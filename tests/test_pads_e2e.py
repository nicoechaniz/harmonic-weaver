"""F5: pads_v1 scene compile + offline HarMoCAP replay + Stage WS pad state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from harmonic_weaver.contract_codec import contract_id_from_manifest
from harmonic_weaver.drivers.harmocap_driver import HarMoCAPDriver
from harmonic_weaver.engine import OBSERVED, WeaverEngine
from harmonic_weaver.server import PROTOCOL_VERSION, STAGE_CONTRACT_ID, create_app
from rehearsal.support import LiveOSCTransport, load_json
from rehearsal.weaver_runtime import (
    _frame_to_wire,
    _handshake_bytes,
    _load_kit_codec,
    _pad_person_features,
    beacon_safety_profile,
    harmocap_manifest,
    shaper_safety_profile,
)


ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT.parent
BEACON_MANIFEST = PROJECTS / "beacon-spatial" / "beacon_spatial.contract.json"
SHAPER_MANIFEST = PROJECTS / "harmonic-shaper" / "contracts" / "shaper.contract.json"
PADS_SCENE = ROOT / "rehearsal" / "scenes" / "pads_v1.scene.json"
SESSION_CANDIDATES = (
    ROOT / "rehearsal" / "artifacts" / "body-instrument-v2" / "harmocap-session.jsonl",
    ROOT / "rehearsal" / "artifacts" / "body-instrument-v1" / "harmocap-session.jsonl",
    ROOT / "rehearsal" / "artifacts" / "body-v5" / "harmocap-session.jsonl",
    PROJECTS
    / "HarMoCAP"
    / "harmocap-nico-kit"
    / "examples"
    / "fixtures"
    / "two_persons.jsonl",
)


def _session_path() -> Path:
    for path in SESSION_CANDIDATES:
        if path.is_file():
            return path
    pytest.skip("no recorded HarMoCAP session available for pads e2e")


def _load_frames(path: Path, *, limit: int | None = None) -> list[dict]:
    frames: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            frames.append(json.loads(line))
            if limit is not None and len(frames) >= limit:
                break
    assert frames, f"session empty: {path}"
    return frames


def _patch_udp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop UDP datagrams without replacing the global socket constructor.

    Patching ``socket.socket`` itself breaks Starlette TestClient / asyncio.
    """
    from harmonic_weaver.contract_codec import encode_message
    from rehearsal.support import append_jsonl

    def _send_no_udp(self, record, address: str, args: list) -> None:  # noqa: ANN001
        endpoint = self._endpoints.get(record.instrument_id)
        if endpoint is None:
            raise RuntimeError(f"no OSC endpoint for {record.instrument_id}")
        encode_message(address, args)  # validate encode path
        payload = {
            "instrument_id": record.instrument_id,
            "kind": record.kind,
            "sent_at_us": record.sent_at_us,
            "reason": record.reason,
            "capability": record.capability,
            "address": record.address,
            "bindings": dict(record.bindings) if record.bindings is not None else None,
            "argument": record.argument,
            "value": record.value,
            "action": record.action,
            "osc_host": endpoint[0],
            "osc_port": endpoint[1],
        }
        with self._lock:
            self._records.append(record)
            append_jsonl(self._audit_path, payload)

    monkeypatch.setattr(LiveOSCTransport, "_send", _send_no_udp)


def _ready_pads_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[WeaverEngine, LiveOSCTransport, dict]:
    if not BEACON_MANIFEST.is_file() or not SHAPER_MANIFEST.is_file():
        pytest.skip("sibling instrument manifests are not checked out")
    if not PADS_SCENE.is_file():
        pytest.skip("pads_v1 scene missing")

    _patch_udp(monkeypatch)
    audit = tmp_path / "instrument_outputs.jsonl"
    transport = LiveOSCTransport(
        {
            "beacon-spatial": ("127.0.0.1", 57120),
            "shaper": ("127.0.0.1", 9002),
        },
        audit,
    )
    engine = WeaverEngine(transport=transport)

    beacon = load_json(BEACON_MANIFEST)
    shaper = load_json(SHAPER_MANIFEST)
    beacon_cid = contract_id_from_manifest(beacon)
    shaper_cid = contract_id_from_manifest(shaper)
    engine.install_instrument(beacon, beacon_safety_profile(beacon_cid))
    engine.install_instrument(shaper, shaper_safety_profile(shaper_cid))
    assert engine.instrument_hello("beacon-spatial", "00000000000000b1", beacon_cid)
    assert engine.instrument_sync_complete("beacon-spatial", "00000000000000b1", beacon_cid)
    assert engine.instrument_hello("shaper", "00000000000000c1", shaper_cid)
    assert engine.instrument_sync_complete("shaper", "00000000000000c1", shaper_cid)

    harmocap = harmocap_manifest(lease_ms=60_000.0)
    harmocap_cid = engine.install_source(harmocap)
    assert engine.source_hello("harmocap", "00000000000000a1", harmocap_cid)

    scene = load_json(PADS_SCENE)
    engine.upsert_scene(scene, engine.stage_revision)
    engine.switch_scene(scene["scene_id"], int(scene["scene_version"]), engine.stage_revision)
    return engine, transport, scene


def _replay_session(
    engine: WeaverEngine,
    frames: list[dict],
    *,
    pad_history: list[tuple[float | None, float | None]] | None = None,
    event_listener=None,
) -> HarMoCAPDriver:
    codec = _load_kit_codec()
    remove = None
    if event_listener is not None:
        remove = engine.add_event_listener(event_listener)
    driver = HarMoCAPDriver(on_frame=engine.driver_callback, lease_ms=60_000.0)
    now_ms = 1_000_000.0
    seq = 0
    handshake_sent = False
    last_stream: str | None = None
    try:
        for frame_index, frame in enumerate(frames):
            # Older sessions may carry 21-feature vectors; pad to kit expectation.
            if frame.get("persons"):
                frame = dict(frame)
                frame["persons"] = [
                    _pad_person_features(dict(person)) for person in frame["persons"]
                ]
            stream = str(frame.get("stream_id", "replay"))
            if not handshake_sent or stream != last_stream or frame_index % 30 == 0:
                for packet in _handshake_bytes(codec, frame):
                    driver.handle_datagram(packet, now_ms=now_ms)
                handshake_sent = True
                last_stream = stream
            for packet in _frame_to_wire(codec, frame, first_seq=seq + 1):
                seq += 1
                driver.handle_datagram(packet, now_ms=now_ms)
            if pad_history is not None:
                right = engine.source_value("hand_r_pad.pad")
                left = engine.source_value("hand_l_pad.pad")
                pad_history.append(
                    (
                        None if right is None or right.state != OBSERVED else float(right.value),
                        None if left is None or left.state != OBSERVED else float(left.value),
                    )
                )
            now_ms += 33.0
    finally:
        if remove is not None:
            remove()
    return driver


def test_pads_v1_scene_structure_and_metadata() -> None:
    scene = load_json(PADS_SCENE)
    assert scene["scene_id"] == "pads-v1"
    assert len(scene["aggregators"]) == 6
    assert len(scene["routes"]) == 64
    derived = {item["derived_source_id"] for item in scene["aggregators"]}
    assert {
        "hand_r_x",
        "hand_r_y",
        "hand_l_x",
        "hand_l_y",
        "hand_r_pad",
        "hand_l_pad",
    } <= derived
    pad_aggs = [
        item
        for item in scene["aggregators"]
        if item["derived_source_id"] in {"hand_r_pad", "hand_l_pad"}
    ]
    assert all(item["operator"] == "bin_2d" for item in pad_aggs)
    assert all(item["cols"] == 4 and item["rows"] == 8 for item in pad_aggs)
    assert all(item["serpentine"] is True for item in pad_aggs)
    assert all(item["output_channel"] == "pad" for item in pad_aggs)

    right = [r for r in scene["routes"] if r["route_id"].startswith("pad-r-")]
    left = [r for r in scene["routes"] if r["route_id"].startswith("pad-l-")]
    assert len(right) == 32 and len(left) == 32
    assert all(r["destination"]["capability"] == "harmonic_envelope" for r in right)
    assert all(r["destination"]["capability"] == "harmonic_gain" for r in left)
    assert {r["destination"]["bindings"]["N"] for r in right} == set(range(1, 33))
    assert {r["destination"]["bindings"]["N"] for r in left} == set(range(1, 33))
    assert all(r["inputs"] == [{"channel": "hand_r_pad.pad"}] for r in right)
    assert all(r["inputs"] == [{"channel": "hand_l_pad.pad"}] for r in left)

    metadata = scene.get("metadata") or {}
    assert metadata.get("pad_sources", {}).get("right") == "hand_r_pad.pad"
    assert metadata.get("pad_sources", {}).get("left") == "hand_l_pad.pad"
    pad_grid = metadata.get("pad_grid") or {}
    assert pad_grid.get("cols") == 4 and pad_grid.get("rows") == 8
    assert pad_grid.get("serpentine") is True and pad_grid.get("pad_count") == 32
    dest_map = metadata.get("destination_map") or {}
    assert dest_map.get("right_hand") == "harmonic_envelope"
    assert dest_map.get("left_hand") == "harmonic_gain"
    assert dest_map.get("binding") == "N"
    assert dest_map.get("harmonic_range") == [1, 32]


def test_pads_v1_compiles_with_real_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _transport, scene = _ready_pads_engine(tmp_path, monkeypatch)
    snapshot = engine.snapshot(["routes", "sources"])
    assert len(snapshot["routes"]) == 64
    assert all(route["runtime"]["active"] for route in snapshot["routes"])
    source_ids = {item["source_id"] for item in snapshot["sources"]}
    assert {"harmocap", "hand_r_pad", "hand_l_pad", "hand_r_x", "hand_l_x"} <= source_ids
    assert scene["scene_id"] == engine.active_scene_id
    engine.close()


def test_pads_e2e_replay_pad_indices_and_harmonic_gain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session_path()
    # Cap frames for runtime; body-instrument sessions are long enough that a
    # few hundred frames exercise multiple wrist positions.
    frames = _load_frames(session, limit=220)
    engine, transport, _scene = _ready_pads_engine(tmp_path, monkeypatch)

    channel_events: list[dict] = []

    def on_event(event) -> None:
        if event.type != "state.event":
            return
        payload = event.as_dict()["payload"]
        if payload.get("topic") != "sources":
            return
        if payload.get("action") != "source.channels_updated":
            return
        entity = payload.get("entity") or {}
        if entity.get("source_id") in {"hand_r_pad", "hand_l_pad"}:
            channel_events.append(payload)

    pad_history: list[tuple[float | None, float | None]] = []
    _replay_session(
        engine,
        frames,
        pad_history=pad_history,
        event_listener=on_event,
    )

    observed_right = {value for value, _ in pad_history if value is not None}
    observed_left = {value for _, value in pad_history if value is not None}
    observed_any = observed_right | observed_left
    assert observed_any, "expected at least one observed hand_*_pad.pad during replay"
    assert len(observed_any) >= 2, (
        f"pad indices must change with hand movement; saw {sorted(observed_any)}"
    )
    for value in observed_any:
        assert 0.0 <= value <= 31.0

    gain_writes = [
        record
        for record in transport.records
        if record.capability == "harmonic_gain" and record.reason == "route"
    ]
    assert gain_writes, "expected harmonic_gain route writes from left-hand pads"
    for record in gain_writes:
        assert record.argument == "gain"
        assert isinstance(record.bindings, Mapping)
        harmonic = record.bindings.get("N")
        assert isinstance(harmonic, int) and 1 <= harmonic <= 32
        assert 0.0 <= float(record.value) <= 1.0

    envelope_writes = [
        record
        for record in transport.records
        if record.capability == "harmonic_envelope" and record.reason == "route"
    ]
    assert envelope_writes, "expected harmonic_envelope route writes from right-hand pads"
    for record in envelope_writes:
        assert record.argument == "gain"
        assert isinstance(record.bindings, Mapping)
        harmonic = record.bindings.get("N")
        assert isinstance(harmonic, int) and 1 <= harmonic <= 32
        assert 0.0 <= float(record.value) <= 1.0

    assert engine.metrics["transport_errors"] == 0
    assert engine.metrics["instrument_writes"] > 0
    assert channel_events, "expected source.channels_updated events for pad sources"
    pad_sources = {event["entity"]["source_id"] for event in channel_events}
    assert pad_sources & {"hand_r_pad", "hand_l_pad"}

    # Sanity: written N values correspond to observed pad indices (+1).
    if observed_left:
        written_n = {int(record.bindings["N"]) for record in gain_writes}
        expected_n = {int(round(v)) + 1 for v in observed_left}
        assert written_n & expected_n, (
            f"harmonic_gain N {sorted(written_n)} did not intersect "
            f"observed left pads+1 {sorted(expected_n)}"
        )
    if observed_right:
        written_n = {int(record.bindings["N"]) for record in envelope_writes}
        expected_n = {int(round(v)) + 1 for v in observed_right}
        assert written_n & expected_n, (
            f"harmonic_envelope N {sorted(written_n)} did not intersect "
            f"observed right pads+1 {sorted(expected_n)}"
        )

    engine.close()


def test_pads_stage_ws_exposes_hand_pad_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session_path()
    frames = _load_frames(session, limit=120)
    engine, _transport, _scene = _ready_pads_engine(tmp_path, monkeypatch)
    app = create_app(engine)

    def client_message(message_type: str, request_id: str, payload: dict) -> dict:
        return {
            "type": message_type,
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "payload": payload,
        }

    with TestClient(app) as client:
        http_pads = client.get("/api/pads")
        assert http_pads.status_code == 200
        body = http_pads.json()
        assert "hand_r_pad" in body and "hand_l_pad" in body

        with client.websocket_connect("/ws") as socket:
            first = socket.receive_json()
            assert first["type"] == "server.hello"
            assert first["payload"]["gate_state"] == "awaiting_client"
            socket.send_json(
                client_message(
                    "client.hello",
                    "pads-hello",
                    {
                        "client_id": "pads-e2e",
                        "expected_contract_id": STAGE_CONTRACT_ID,
                        "supported_protocol_versions": [PROTOCOL_VERSION],
                    },
                )
            )
            ready = socket.receive_json()
            assert ready["type"] == "server.hello"
            assert ready["payload"]["gate_state"] == "ready"

            socket.send_json(
                client_message("state.subscribe", "pads-sub", {"topics": ["sources"]})
            )
            snapshot = socket.receive_json()
            assert snapshot["type"] == "state.snapshot"
            source_ids = {item["source_id"] for item in snapshot["payload"]["sources"]}
            assert {"hand_r_pad", "hand_l_pad"} <= source_ids

            # Replay while the socket is subscribed; collect channel updates.
            seen_pad_events: list[dict] = []

            def on_event(event) -> None:
                if event.type != "state.event":
                    return
                payload = event.as_dict()["payload"]
                if (
                    payload.get("topic") == "sources"
                    and payload.get("action") == "source.channels_updated"
                    and (payload.get("entity") or {}).get("source_id")
                    in {"hand_r_pad", "hand_l_pad"}
                ):
                    seen_pad_events.append(payload)

            remove = engine.add_event_listener(on_event)
            try:
                _replay_session(engine, frames)
            finally:
                remove()

            # Drain any queued WS frames without hanging the test forever.
            import time

            deadline = time.time() + 2.0
            ws_pad_messages = 0
            while time.time() < deadline:
                try:
                    message = socket.receive_json()
                except Exception:
                    break
                if message.get("type") != "state.event":
                    continue
                payload = message.get("payload") or {}
                if (
                    payload.get("topic") == "sources"
                    and payload.get("action") == "source.channels_updated"
                    and (payload.get("entity") or {}).get("source_id")
                    in {"hand_r_pad", "hand_l_pad"}
                ):
                    ws_pad_messages += 1
                    channels = (payload.get("entity") or {}).get("channels") or {}
                    assert "pad" in channels

            assert seen_pad_events or ws_pad_messages, (
                "Stage sources topic must carry hand_*_pad channel updates"
            )

            # After replay, HTTP endpoint should expose observed pad values when present.
            after = client.get("/api/pads").json()
            for key in ("hand_r_pad", "hand_l_pad"):
                assert key in after
                assert "value" in after[key]

    engine.close()
