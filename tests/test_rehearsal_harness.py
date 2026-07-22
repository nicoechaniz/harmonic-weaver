from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from harmonic_weaver.contract_codec import (
    contract_id_from_manifest,
    decode_message,
)
from harmonic_weaver.engine import OBSERVED, OutputRecord, WeaverEngine
from engine_fixtures import ready_engine, route, scene
from rehearsal.support import LiveOSCTransport, load_json
from rehearsal.support import analyze_wav
from rehearsal.weaver_runtime import (
    beacon_safety_profile,
    ecg_manifest,
    harmocap_manifest,
    midi_manifest,
    shaper_safety_profile,
)


ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT.parent
BEACON_MANIFEST = PROJECTS / "beacon-spatial" / "beacon_spatial.contract.json"
SHAPER_MANIFEST = (
    PROJECTS / "harmonic-shaper" / "contracts" / "shaper.contract.json"
)


@pytest.mark.skipif(
    not BEACON_MANIFEST.exists() or not SHAPER_MANIFEST.exists(),
    reason="T4.5 sibling instrument manifests are not checked out",
)
def test_real_manifests_safety_profiles_and_both_scenes_compile() -> None:
    engine = WeaverEngine()
    for manifest in (harmocap_manifest(), ecg_manifest(), midi_manifest()):
        contract_id = engine.install_source(manifest)
        assert engine.source_hello(
            manifest["source"]["source_id"], "0000000000000001", contract_id
        )

    beacon = load_json(BEACON_MANIFEST)
    shaper = load_json(SHAPER_MANIFEST)
    for manifest, safety, stream_id in (
        (beacon, beacon_safety_profile, "0000000000000002"),
        (shaper, shaper_safety_profile, "0000000000000003"),
    ):
        contract_id = contract_id_from_manifest(manifest)
        instrument_id = manifest["instrument"]["instrument_id"]
        assert engine.install_instrument(manifest, safety(contract_id)) == contract_id
        assert engine.instrument_hello(instrument_id, stream_id, contract_id)
        assert engine.instrument_sync_complete(instrument_id, stream_id, contract_id)

    demo = load_json(ROOT / "rehearsal" / "scenes" / "event_demo.scene.json")
    sparse = load_json(ROOT / "rehearsal" / "scenes" / "sparse.scene.json")
    instrumento = load_json(ROOT / "rehearsal" / "scenes" / "instrumento_v1_mvp.scene.json")
    pads = load_json(ROOT / "rehearsal" / "scenes" / "pads_v1.scene.json")
    engine.upsert_scene(demo, engine.stage_revision)
    engine.upsert_scene(sparse, engine.stage_revision)
    engine.upsert_scene(instrumento, engine.stage_revision)
    engine.upsert_scene(pads, engine.stage_revision)
    engine.switch_scene("event-demo", 1, engine.stage_revision)

    snapshot = engine.snapshot(["routes", "instruments"])
    assert len(snapshot["routes"]) == 7
    assert {route["destination"]["bindings"].get("N") for route in snapshot["routes"][:5]} == {
        1,
        2,
        3,
        4,
        5,
    }
    assert all(route["runtime"]["active"] for route in snapshot["routes"])

    engine.switch_scene("instrumento-v1-mvp", 1, engine.stage_revision)
    instrument_snapshot = engine.snapshot(["routes"])
    # Multi-body: body0 H=0+H=1, body1 H=2+H=3, shared ceiling/clock/static setup.
    assert len(instrument_snapshot["routes"]) == 36
    caps = {route["destination"]["capability"] for route in instrument_snapshot["routes"]}
    assert {
        "partial_ceiling",
        "arp_rate",
        "arp_direction",
        "arp_density",
        "clock_bpm",
        "generator_enable",
        "arp_enable",
    } <= caps
    arp_hands = {
        route["destination"]["bindings"].get("H")
        for route in instrument_snapshot["routes"]
        if route["destination"]["capability"].startswith("arp_")
    }
    assert {0, 1, 2, 3} <= arp_hands
    assert all(route["runtime"]["active"] for route in instrument_snapshot["routes"])

    engine.switch_scene("pads-v1", 1, engine.stage_revision)
    pads_snapshot = engine.snapshot(["routes", "sources"])
    assert len(pads_snapshot["routes"]) == 64
    pad_caps = {route["destination"]["capability"] for route in pads_snapshot["routes"]}
    assert pad_caps == {"harmonic_envelope", "harmonic_gain"}
    source_ids = {item["source_id"] for item in pads_snapshot["sources"]}
    assert {"hand_r_pad", "hand_l_pad"} <= source_ids
    assert all(route["runtime"]["active"] for route in pads_snapshot["routes"])


def test_live_osc_transport_audits_engine_frozen_route_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def sendto(self, packet: bytes, endpoint: tuple[str, int]) -> None:
            sent.append((packet, endpoint))

    monkeypatch.setattr("rehearsal.support.socket.socket", lambda *args: FakeSocket())
    audit = tmp_path / "outputs.jsonl"
    transport = LiveOSCTransport({"synth": ("127.0.0.1", 9002)}, audit)
    engine, _recorder, source, _instrument = ready_engine()
    engine.transport = transport
    engine.upsert_scene(
        scene(routes=[route(voice=4)]),
        engine.stage_revision,
    )
    engine.switch_scene("main", 1, engine.stage_revision)

    assert engine.ingest_source_frame(
        "sensor",
        "0000000000000001",
        source["contract_id"],
        0,
        {"modulation": (0.625, OBSERVED, 1.0)},
        now_us=1,
    )

    assert sent[-1][1] == ("127.0.0.1", 9002)
    address, args = decode_message(sent[-1][0])
    assert address == "/instrument/gain/4"
    assert args == pytest.approx([0.625])
    audited = [
        json.loads(line)
        for line in audit.read_text(encoding="utf-8").splitlines()
    ][-1]
    assert audited["reason"] == "route"
    assert audited["bindings"] == {"N": 4}
    assert isinstance(transport.records[-1].bindings, MappingProxyType)
    assert engine.metrics["instrument_writes"] > 0
    assert engine.metrics["transport_errors"] == 0


def test_live_osc_transport_audits_action_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def sendto(self, packet: bytes, endpoint: tuple[str, int]) -> None:
            sent.append((packet, endpoint))

    monkeypatch.setattr("rehearsal.support.socket.socket", lambda *args: FakeSocket())
    audit = tmp_path / "actions.jsonl"
    transport = LiveOSCTransport(
        {"synth": ("127.0.0.1", 9002)},
        audit,
        action_addresses={("synth", "panic"): "/instrument/panic"},
    )
    transport.invoke_action(
        OutputRecord("synth", "action", 123, "panic", action="panic")
    )

    address, args = decode_message(sent[0][0])
    assert address == "/instrument/panic"
    assert args == []
    audited = json.loads(audit.read_text(encoding="utf-8"))
    assert audited["kind"] == "action"
    assert audited["action"] == "panic"
    assert audited["bindings"] is None


def test_rehearsal_scenes_express_required_event_sources() -> None:
    demo = load_json(ROOT / "rehearsal" / "scenes" / "event_demo.scene.json")
    aggregate_inputs = {
        item["channel"]
        for aggregator in demo["aggregators"]
        for item in aggregator["inputs"]
    }
    required = {
        "harmocap.slot_0_keypoint_left_wrist_y",
        "harmocap.slot_0_keypoint_right_wrist_y",
        "harmocap.slot_0_keypoint_left_ankle_y",
        "harmocap.slot_0_keypoint_right_ankle_y",
        "harmocap.slot_0_keypoint_nose_y",
        "harmocap.slot_1_kinetic_energy",
    }
    assert required <= aggregate_inputs
    assert any(
        route["inputs"] == [{"channel": "ecg.beat"}]
        and route["destination"]["capability"] == "nature_gain"
        for route in demo["routes"]
    )
    assert all(
        route["validity"]["min_confidence"] >= 0.8
        for route in demo["routes"]
    )


def test_harmocap_manifest_matches_producer_feature_ranges() -> None:
    """S13 regression: HarMoCAP schema.FEATURE_RANGES declares verticality as
    signed (-1..1); the runtime manifest must not clamp it to (0,1), or live
    frames raise engine range-validation errors. tempo_bpm is producer BPM
    (0..240), not unit-normalized."""
    manifest = harmocap_manifest()
    ranges = {spec["name"]: tuple(spec["range"]) for spec in manifest["channels"]}
    for slot in range(8):
        assert ranges[f"slot_{slot}_verticality"] == (-1.0, 1.0)
        assert ranges[f"slot_{slot}_tempo_bpm"] == (0.0, 240.0)
    # All other features stay normalized (0..1).
    for name, bounds in ranges.items():
        if "_keypoint_" in name or name.endswith(("_present", "_focused")):
            continue
        if name.endswith("_verticality") or name.endswith("_tempo_bpm"):
            continue
        assert bounds == (0.0, 1.0), name


def test_audio_analyzer_reports_finite_non_silent_signal(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    sample_rate = 8_000
    time_axis = np.arange(sample_rate, dtype=np.float64) / sample_rate
    mono = 0.25 * np.sin(2.0 * np.pi * 220.0 * time_axis)
    stereo = np.column_stack((mono, mono))
    path = tmp_path / "probe.wav"
    sf.write(path, stereo, sample_rate, subtype="FLOAT")

    stats = analyze_wav(path)

    assert stats["duration_s"] == pytest.approx(1.0)
    assert stats["channels"] == 2
    assert stats["all_finite"] is True
    assert stats["nan_count"] == stats["inf_count"] == 0
    assert stats["non_silence_ratio"] > 0.99
    assert stats["peak_abs"] == pytest.approx(0.25, rel=1e-4)
    assert stats["rms"] > 0.1
