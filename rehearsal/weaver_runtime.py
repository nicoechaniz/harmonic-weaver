"""Live Weaver runtime used by the end-to-end rehearsal.

This is intentionally separate from the minimal production CLI: it installs the
three rehearsal source adapters, exact sibling instrument manifests, safety
profiles, real UDP output endpoints, and then serves the normal Stage API.
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from harmonic_weaver.contract_codec import contract_id_from_manifest
from harmonic_weaver.drivers.ecg_driver import ECGDriver
from harmonic_weaver.drivers.harmocap_driver import (
    FEATURE_NAMES,
    KEYPOINT_NAMES,
    HarMoCAPDriver,
    channel_names,
)
from harmonic_weaver.drivers.midi_driver import MIDIDriver
from harmonic_weaver.engine import ReportWriter, WeaverEngine
from harmonic_weaver.server import create_app

from rehearsal.support import (
    LiveOSCTransport,
    atomic_json,
    beacon_snapshot,
    http_json,
    load_json,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _channel_spec(name: str, bounds: tuple[float, float], rate_hz: float) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Rehearsal adapter channel {name}.",
        "range": list(bounds),
        "polarity": "Declared adapter-domain minimum to maximum.",
        "rate_hz_nominal": rate_hz,
        "smoothing_hints": "The scene owns any additional smoothing.",
    }


def source_manifest(
    source_id: str,
    channels: Iterable[tuple[str, tuple[float, float]]],
    *,
    lease_ms: float,
    rate_hz: float,
) -> dict[str, Any]:
    manifest = load_json(REPO_ROOT / "contracts" / "source_frame.template.json")
    old_namespace = manifest["namespace"]
    namespace = f"/src/{source_id}"
    manifest["name"] = f"{source_id}-rehearsal-source-frame"
    manifest["namespace"] = namespace
    manifest["source"]["source_id"] = source_id
    manifest["source"]["description"] = f"Rehearsal adapter for the {source_id} driver."
    manifest["presence"]["lease_ms"] = lease_ms
    manifest["handshake"]["hello_address"] = f"{namespace}/hello"
    manifest["handshake"]["hello_request_address"] = f"{namespace}/hello/request"
    manifest["addresses"] = {
        address.replace(old_namespace, namespace): value
        for address, value in manifest["addresses"].items()
    }
    manifest["channels"] = [
        _channel_spec(name, bounds, rate_hz) for name, bounds in channels
    ]
    manifest["contract_id"] = contract_id_from_manifest(manifest)
    return manifest


def harmocap_manifest(lease_ms: float = 2500.0) -> dict[str, Any]:
    boolean_names = {
        f"slot_{slot}_{field}"
        for slot in range(8)
        for field in ("present", "focused")
    }
    confidence_names = {
        f"slot_{slot}_keypoint_{keypoint}_conf"
        for slot in range(8)
        for keypoint in KEYPOINT_NAMES
    }
    coordinate_names = {
        f"slot_{slot}_keypoint_{keypoint}_{axis}"
        for slot in range(8)
        for keypoint in KEYPOINT_NAMES
        for axis in ("x", "y")
    }
    feature_names = {
        f"slot_{slot}_{feature}"
        for slot in range(8)
        for feature in (*FEATURE_NAMES, "kinetic_energy")
    }
    specs: list[tuple[str, tuple[float, float]]] = []
    for name in channel_names():
        if name in boolean_names or name in confidence_names or name in feature_names:
            bounds = (0.0, 1.0)
        elif name in coordinate_names:
            bounds = (0.0, 4.0)
        else:  # defensive: channel_names should be exhausted by the sets above
            bounds = (-1_000_000.0, 1_000_000.0)
        # HarMoCAP INTERFACE_SPEC / schema.FEATURE_RANGES: verticality is a
        # signed feature (-1..1). Live data exercises the negative range even
        # though the two_persons fixture never did; the wrong bound made the
        # engine raise on valid live frames (S13 live test).
        if name.endswith("_verticality"):
            bounds = (-1.0, 1.0)
        # tempo_bpm is producer BPM, not unit-normalized (schema 0..240).
        if name.endswith("_tempo_bpm"):
            bounds = (0.0, 240.0)
        specs.append((name, bounds))
    return source_manifest("harmocap", specs, lease_ms=lease_ms, rate_hz=60.0)


def ecg_manifest(lease_ms: float = 2500.0) -> dict[str, Any]:
    return source_manifest(
        "ecg",
        (
            ("beat", (0.0, 1.0)),
            ("bpm", (20.0, 300.0)),
            ("signal_quality", (0.0, 1.0)),
        ),
        lease_ms=lease_ms,
        rate_hz=31.25,
    )


def midi_manifest(lease_ms: float = 2500.0) -> dict[str, Any]:
    return source_manifest(
        "midi",
        (("cc_1", (0.0, 1.0)), ("modwheel", (0.0, 1.0))),
        lease_ms=lease_ms,
        rate_hz=2.0,
    )


def shaper_safety_profile(contract_id: str) -> dict[str, Any]:
    """Safety defaults for every Shaper destination a rehearsal scene may target.

    Includes legacy harmonic envelopes (event_demo / sparse) plus the cuerpo-
    instrumento MVP surface: partial_ceiling, clock, settle, generator, arp/*
    for hands H=0..3 (multi-body: body0→H0/H1, body1→H2/H3). Every route
    destination must appear here or scene compile raises unsafe_instrument.
    """
    reset_defaults: list[dict[str, Any]] = [
        {
            "capability": "harmonic_envelope",
            "bindings": {"N": harmonic},
            "argument": "gain",
            "value": 0.0,
        }
        for harmonic in range(1, 6)
    ]
    reset_defaults.extend(
        [
            {
                "capability": "partial_ceiling",
                "bindings": {},
                "argument": "level",
                "value": 1.0,
            },
            {
                "capability": "clock_bpm",
                "bindings": {},
                "argument": "bpm",
                "value": 120.0,
            },
            {
                "capability": "settle_beats",
                "bindings": {},
                "argument": "beats",
                "value": 1.0,
            },
            {
                "capability": "generator_enable",
                "bindings": {},
                "argument": "enable",
                "value": 0,
            },
        ]
    )
    # Multi-body scene uses H=0..3 (body 0 → H=0,1; body 1 → H=2,3).
    for hand in range(0, 4):
        reset_defaults.extend(
            [
                {
                    "capability": "arp_enable",
                    "bindings": {"H": hand},
                    "argument": "enable",
                    "value": 0,
                },
                {
                    "capability": "arp_rate",
                    "bindings": {"H": hand},
                    "argument": "steps_per_beat",
                    "value": 0.0,
                },
                {
                    "capability": "arp_direction",
                    "bindings": {"H": hand},
                    "argument": "dir",
                    "value": 1.0,
                },
                {
                    "capability": "arp_density",
                    "bindings": {"H": hand},
                    "argument": "fill",
                    "value": 0.0,
                },
                {
                    "capability": "arp_register_lo",
                    "bindings": {"H": hand},
                    "argument": "n",
                    "value": 1.0,
                },
                {
                    "capability": "arp_register_hi",
                    "bindings": {"H": hand},
                    "argument": "n",
                    "value": 16.0,
                },
                {
                    "capability": "arp_gate",
                    "bindings": {"H": hand},
                    "argument": "frac",
                    "value": 0.5,
                },
                {
                    "capability": "arp_gain",
                    "bindings": {"H": hand},
                    "argument": "g",
                    "value": 0.0,
                },
            ]
        )
    return {
        "instrument_id": "shaper",
        "instrument_contract_id": contract_id,
        "instrument_class": "sustained_processor",
        "silence_actions": [
            {
                "capability": "panic",
                "bindings": {},
                "argument": "trigger",
                "value": 1,
                "ramp_ms": 0.0,
            }
        ],
        "reset_defaults": reset_defaults,
        "rearm_fade_ms": 300.0,
    }


def beacon_safety_profile(contract_id: str) -> dict[str, Any]:
    return {
        "instrument_id": "beacon-spatial",
        "instrument_contract_id": contract_id,
        "instrument_class": "sustained_processor",
        "silence_actions": [
            {
                "capability": "master_gain",
                "bindings": {},
                "argument": "master_gain",
                "value": 0.0,
                "ramp_ms": 10.0,
            },
            {
                "capability": "nature_gain",
                "bindings": {},
                "argument": "gain",
                "value": 0.0,
                "ramp_ms": 10.0,
            },
        ],
        "reset_defaults": [
            {
                "capability": "master_gain",
                "bindings": {},
                "argument": "master_gain",
                "value": 0.55,
            },
            {
                "capability": "nature_gain",
                "bindings": {},
                "argument": "gain",
                "value": 0.08,
            },
            {
                "capability": "band_gain",
                "bindings": {"N": 3},
                "argument": "gain",
                "value": 0.0,
            },
        ],
        "rearm_fade_ms": 300.0,
    }


def _stream_id() -> str:
    return secrets.token_hex(8)


def _default_sibling(name: str) -> Path:
    return REPO_ROOT.parent / name


def resolve_scene_path(scene: str | Path) -> Path:
    """Resolve a scene name or path under rehearsal/scenes/."""
    candidate = Path(scene)
    if candidate.is_file():
        return candidate
    name = str(scene)
    if name.endswith(".scene.json"):
        path = REPO_ROOT / "rehearsal" / "scenes" / name
    else:
        path = REPO_ROOT / "rehearsal" / "scenes" / f"{name}.scene.json"
    if not path.is_file():
        raise FileNotFoundError(f"scene not found: {scene} (looked for {path})")
    return path


def _pad_person_features(person: dict[str, Any], target_n: int = 24) -> dict[str, Any]:
    """Pad pre-tempo (21-feature) session persons so kit pack expects 24."""
    out = dict(person)
    features = list(out.get("features") or [])
    feat_state = list(out.get("feat_state") or [])
    if len(features) < target_n:
        features = features + [0.0] * (target_n - len(features))
    if len(feat_state) < target_n:
        # STATE_INVALID = 2 for unknown tempo fields on older sessions.
        feat_state = feat_state + [2] * (target_n - len(feat_state))
    out["features"] = features[:target_n]
    out["feat_state"] = feat_state[:target_n]
    return out


def _load_kit_codec():
    """Import HarMoCAP kit osc_codec for session→wire encoding (offline only)."""
    import importlib.util

    kit_codec = (
        REPO_ROOT.parent
        / "HarMoCAP"
        / "harmocap-nico-kit"
        / "osc_codec.py"
    )
    if not kit_codec.is_file():
        raise FileNotFoundError(
            f"HarMoCAP kit osc_codec not found at {kit_codec}; needed for --replay"
        )
    spec = importlib.util.spec_from_file_location("harmocap_kit_osc_codec", kit_codec)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _handshake_bytes(codec: Any, frame: Mapping[str, Any]) -> list[bytes]:
    params = frame.get("calibration_params")
    params_blob = b""
    calib_hash = "0" * 32
    if params:
        order = [
            "torso_height_norm",
            "vmax_hand",
            "vmax_center",
            "jerk_ref",
            "energy_ref",
            "accel_ref",
        ]
        params_blob = codec.pack_calibration_params([float(params[k]) for k in order])
        calib_hash = codec.calibration_hash(params_blob)
    packets = [
        codec.build_hello(
            stream_id=frame["stream_id"],
            schema_version=frame["schema_version"],
            feature_set_version=frame["feature_set_version"],
            producer_version=str(frame.get("producer_version", "0")) + "+replay",
            model_id=frame.get("model_id", "replay"),
            config_hash=frame.get("config_hash", "0" * 32),
            contract_id=frame["contract_id"],
            calibration_generation=int(frame.get("calibration_generation", 0)),
            calibration_state=str(frame.get("calibration_state", "valid")),
            calib_hash=calib_hash,
            effective_from_frame_id=int(frame.get("calibration_effective_from", 0)),
            frame_w=int(frame.get("frame_w", 0)),
            frame_h=int(frame.get("frame_h", 0)),
        )
    ]
    if params:
        packets.append(
            codec.build_calibration(
                stream_id=frame["stream_id"],
                generation=int(frame["calibration_generation"]),
                calib_hash=calib_hash,
                effective_from_frame_id=int(frame.get("calibration_effective_from", 0)),
                params_blob=params_blob,
            )
        )
    return packets


def _frame_to_wire(codec: Any, frame: Mapping[str, Any], first_seq: int) -> list[bytes]:
    bundles: list[bytes] = []
    seq = first_seq
    persons = frame.get("persons") or []
    for person in persons:
        person = _pad_person_features(dict(person))
        if not person.get("present"):
            payload = {"slot_id": int(person["slot_id"]), "present": False}
        else:
            kps = [(float(k[0]), float(k[1]), float(k[2])) for k in person["keypoints"]]
            kst = [(int(s[0]), int(s[1]), int(s[2])) for s in person["kp_state"]]
            payload = {
                "slot_id": int(person["slot_id"]),
                "present": True,
                "focused": bool(person.get("focused")),
                "keypoints_blob": codec.pack_keypoints(kps),
                "kp_state_blob": codec.pack_kp_state(kst),
                "bbox": person.get("bbox_xywhn") or [0.0, 0.0, 0.0, 0.0],
                "features_blob": codec.pack_features(
                    [0.0 if v is None else float(v) for v in person["features"]]
                ),
                "feat_state_blob": codec.pack_feat_state(
                    [int(v) for v in person["feat_state"]]
                ),
            }
        bundles.append(
            codec.build_person_bundle(
                stream_id=frame["stream_id"],
                captured_frame_id=int(frame.get("captured_frame_id", seq)),
                bundle_seq=seq,
                n_persons=int(frame.get("n_persons", len(persons))),
                fps=float(frame.get("fps", 30.0)),
                contract_id=str(frame.get("contract_id", "")),
                calibration_generation=int(frame.get("calibration_generation", 0)),
                calibration_state=str(frame.get("calibration_state", "valid")),
                captured_at_us=int(frame.get("captured_at_us", 0)),
                processed_at_us=int(frame.get("processed_at_us", 0)),
                queued_for_send_at_us=0,
                person=payload,
            )
        )
        seq += 1
    return bundles


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harmonic Weaver live/offline rehearsal runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--report-root", type=Path, default=REPO_ROOT / "reports")
    parser.add_argument("--run-id", default=None, help="required for live mode; default offline-<ts> for --replay")
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument(
        "--beacon-manifest",
        type=Path,
        default=_default_sibling("beacon-spatial") / "beacon_spatial.contract.json",
    )
    parser.add_argument(
        "--shaper-manifest",
        type=Path,
        default=_default_sibling("harmonic-shaper") / "contracts" / "shaper.contract.json",
    )
    parser.add_argument("--beacon-host", default="127.0.0.1")
    parser.add_argument("--beacon-port", type=int, default=57120)
    parser.add_argument("--shaper-host", default="127.0.0.1")
    parser.add_argument("--shaper-port", type=int, default=9002)
    parser.add_argument("--shaper-api", default="http://127.0.0.1:8080/api/state")
    parser.add_argument("--harmocap-host", default="127.0.0.1")
    parser.add_argument("--harmocap-port", type=int, default=9100)
    parser.add_argument("--ecg-host", default="127.0.0.1")
    parser.add_argument("--ecg-port", type=int, default=5001)
    parser.add_argument("--max-runtime-s", type=float, default=300.0)
    parser.add_argument(
        "--lease-ms",
        type=float,
        default=2500.0,
        help="source presence lease in ms; raise it for live sessions so a "
        "temporary dropout (camera stall, person out of frame) does not "
        "latch the source gate as permanently absent",
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--scene",
        default=None,
        help="scene name (under rehearsal/scenes/) or path; required with --replay",
    )
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="hardware-free mode: replay a HarMoCAP session .jsonl through the engine",
    )
    return parser


def run_offline_replay(args: argparse.Namespace) -> int:
    """Hardware-free scene validation: install instruments, load scene, replay jsonl."""
    if args.scene is None:
        raise SystemExit("--scene is required with --replay")
    if not args.shaper_manifest.is_file():
        raise SystemExit(f"shaper manifest not found: {args.shaper_manifest}")
    if not args.beacon_manifest.is_file():
        raise SystemExit(f"beacon manifest not found: {args.beacon_manifest}")
    if not args.replay.is_file():
        raise SystemExit(f"replay jsonl not found: {args.replay}")

    scene_path = resolve_scene_path(args.scene)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = args.run_id or f"offline-{stamp}"
    artifact_root = args.artifact_root or (REPO_ROOT / "rehearsal" / "artifacts" / run_id)
    artifact_root.mkdir(parents=True, exist_ok=True)
    report_root = args.report_root
    report_root.mkdir(parents=True, exist_ok=True)

    output_audit = artifact_root / "instrument_outputs.jsonl"
    if output_audit.exists():
        output_audit.unlink()

    report = ReportWriter(
        report_root,
        run_id=run_id,
        run_config={
            "mode": "offline_replay",
            "scene": str(scene_path),
            "replay": str(args.replay),
            "drivers": ["harmocap"],
            "instrument_transport": "live OSC/UDP (audit only; no hardware required)",
        },
    )
    transport = LiveOSCTransport(
        {
            "beacon-spatial": (args.beacon_host, args.beacon_port),
            "shaper": (args.shaper_host, args.shaper_port),
        },
        output_audit,
    )
    engine = WeaverEngine(transport=transport, report_writer=report)

    beacon_manifest = load_json(args.beacon_manifest)
    shaper_manifest = load_json(args.shaper_manifest)
    beacon_contract_id = contract_id_from_manifest(beacon_manifest)
    shaper_contract_id = contract_id_from_manifest(shaper_manifest)
    engine.install_instrument(beacon_manifest, beacon_safety_profile(beacon_contract_id))
    engine.install_instrument(shaper_manifest, shaper_safety_profile(shaper_contract_id))

    # Offline: skip live Beacon OSC hello and Shaper HTTP state — gate with synthetic streams.
    beacon_stream = _stream_id()
    shaper_stream = _stream_id()
    if not engine.instrument_hello("beacon-spatial", beacon_stream, beacon_contract_id):
        raise RuntimeError("Beacon offline hello rejected")
    if not engine.instrument_sync_complete("beacon-spatial", beacon_stream, beacon_contract_id):
        raise RuntimeError("Beacon offline sync rejected")
    if not engine.instrument_hello("shaper", shaper_stream, shaper_contract_id):
        raise RuntimeError("Shaper offline hello rejected")
    if not engine.instrument_sync_complete("shaper", shaper_stream, shaper_contract_id):
        raise RuntimeError("Shaper offline sync rejected")

    harmocap_src = harmocap_manifest(lease_ms=max(args.lease_ms, 60_000.0))
    harmocap_contract = engine.install_source(harmocap_src)
    harmocap_stream = _stream_id()
    if not engine.source_hello("harmocap", harmocap_stream, harmocap_contract):
        raise RuntimeError("harmocap source hello rejected")

    scene = load_json(scene_path)
    engine.upsert_scene(scene, engine.stage_revision)
    engine.switch_scene(scene["scene_id"], int(scene["scene_version"]), engine.stage_revision)

    codec = _load_kit_codec()
    frames: list[dict[str, Any]] = []
    with args.replay.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                frames.append(json.loads(line))
    if not frames:
        raise SystemExit(f"replay file is empty: {args.replay}")

    driver = HarMoCAPDriver(on_frame=engine.driver_callback, lease_ms=max(args.lease_ms, 60_000.0))
    # Monotonic driver clock so leases and dt-based transforms stay well-defined.
    now_ms = 1_000_000.0
    seq = 0
    handshake_sent = False
    last_stream: str | None = None

    for frame_index, frame in enumerate(frames):
        stream = str(frame.get("stream_id", "replay"))
        if not handshake_sent or stream != last_stream or frame_index % 30 == 0:
            for packet in _handshake_bytes(codec, frame):
                driver.handle_datagram(packet, now_ms=now_ms)
            handshake_sent = True
            last_stream = stream
        for packet in _frame_to_wire(codec, frame, first_seq=seq + 1):
            seq += 1
            driver.handle_datagram(packet, now_ms=now_ms)
        # ~30 Hz frame spacing for slew/derivative dt.
        now_ms += 33.0

    records = transport.records
    caps: dict[str, int] = {}
    for record in records:
        if record.capability:
            caps[record.capability] = caps.get(record.capability, 0) + 1

    summary = {
        "run_id": run_id,
        "scene_id": scene["scene_id"],
        "scene_path": str(scene_path),
        "replay": str(args.replay),
        "frames": len(frames),
        "instrument_writes": int(engine.metrics.get("instrument_writes", 0)),
        "transport_errors": int(engine.metrics.get("transport_errors", 0)),
        "frames_accepted": int(engine.metrics.get("frames_accepted", 0)),
        "capability_counts": caps,
        "audit_path": str(output_audit),
        "arp_entries": sum(1 for name, count in caps.items() if name.startswith("arp_") for _ in range(count)),
    }
    atomic_json(artifact_root / "offline_replay_summary.json", summary)
    engine.close()

    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["instrument_writes"] <= 0:
        raise SystemExit("offline replay produced no instrument writes")
    if not any(name.startswith("arp_") for name in caps):
        raise SystemExit("offline replay produced no arp_* capability writes")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.replay is not None:
        return run_offline_replay(args)
    if args.run_id is None or args.artifact_root is None:
        raise SystemExit("live mode requires --run-id and --artifact-root (or use --replay)")
    args.artifact_root.mkdir(parents=True, exist_ok=True)
    output_audit = args.artifact_root / "instrument_outputs.jsonl"
    report = ReportWriter(
        args.report_root,
        run_id=args.run_id,
        run_config={
            "bind_host": args.host,
            "bind_port": args.port,
            "drivers": ["harmocap", "ecg", "midi"],
            "instrument_transport": "live OSC/UDP",
            "shaper_audio": "disabled (--no-audio); state API is the evidence plane",
            "midi_hardware": False,
            "r24_hardware": False,
        },
    )
    transport = LiveOSCTransport(
        {
            "beacon-spatial": (args.beacon_host, args.beacon_port),
            "shaper": (args.shaper_host, args.shaper_port),
        },
        output_audit,
    )
    engine = WeaverEngine(transport=transport, report_writer=report)

    beacon_manifest = load_json(args.beacon_manifest)
    shaper_manifest = load_json(args.shaper_manifest)
    beacon_contract_id = contract_id_from_manifest(beacon_manifest)
    shaper_contract_id = contract_id_from_manifest(shaper_manifest)
    engine.install_instrument(
        beacon_manifest,
        beacon_safety_profile(beacon_contract_id),
    )
    engine.install_instrument(
        shaper_manifest,
        shaper_safety_profile(shaper_contract_id),
    )

    # Beacon implements the complete OSC hello/dump protocol. Its live stream
    # tuple is installed into the engine only after both checks pass.
    beacon_initial = beacon_snapshot(
        host=args.beacon_host,
        port=args.beacon_port,
        expected_contract_id=beacon_contract_id,
        timeout=10.0,
    )
    atomic_json(args.artifact_root / "beacon_runtime_sync.json", beacon_initial)
    beacon_stream = beacon_initial["hello"]["stream_id"]
    if not engine.instrument_hello("beacon-spatial", beacon_stream, beacon_contract_id):
        raise RuntimeError("Beacon engine hello gate rejected the live contract tuple")
    if not engine.instrument_sync_complete(
        "beacon-spatial", beacon_stream, beacon_contract_id
    ):
        raise RuntimeError("Beacon engine state synchronization did not complete")

    # The exact Shaper v1 manifest declares that OSC hello is not implemented.
    # The adapter therefore gates its local engine tuple on the real HTTP state
    # snapshot documented by state_sync.current_mechanism.
    shaper_initial = http_json(args.shaper_api, timeout=5.0)
    shaper_stream = _stream_id()
    atomic_json(
        args.artifact_root / "shaper_runtime_sync.json",
        {
            "captured_at_us": time.time_ns() // 1000,
            "method": "manifest-declared HTTP state API fallback",
            "runtime_has_no_osc_hello": True,
            "adapter_stream_id": shaper_stream,
            "contract_id": shaper_contract_id,
            "state": shaper_initial,
        },
    )
    if not engine.instrument_hello("shaper", shaper_stream, shaper_contract_id):
        raise RuntimeError("Shaper adapter hello gate rejected the exact manifest ID")
    if not engine.instrument_sync_complete("shaper", shaper_stream, shaper_contract_id):
        raise RuntimeError("Shaper adapter state synchronization did not complete")

    source_manifests = {
        "harmocap": harmocap_manifest(lease_ms=args.lease_ms),
        "ecg": ecg_manifest(lease_ms=args.lease_ms),
        "midi": midi_manifest(lease_ms=args.lease_ms),
    }
    source_streams: dict[str, str] = {}
    for source_id, manifest in source_manifests.items():
        contract_id = engine.install_source(manifest)
        stream_id = _stream_id()
        source_streams[source_id] = stream_id
        if not engine.source_hello(source_id, stream_id, contract_id):
            raise RuntimeError(f"source hello rejected for {source_id}")

    harmocap = HarMoCAPDriver(on_frame=engine.driver_callback)
    harmocap_thread = threading.Thread(
        target=harmocap.serve_udp,
        kwargs={
            "host": args.harmocap_host,
            "port": args.harmocap_port,
            "seconds": args.max_runtime_s,
        },
        name="harmocap-rehearsal-udp",
        daemon=True,
    )
    harmocap_thread.start()

    ecg = ECGDriver(
        on_frame=engine.driver_callback,
        listen_host=args.ecg_host,
        listen_port=args.ecg_port,
    )
    ecg.start()

    midi = MIDIDriver(on_frame=engine.driver_callback, declared_ccs=(1,))
    midi_stop = threading.Event()

    def poll_midi() -> None:
        while not midi_stop.is_set():
            emitted = midi.poll_once()
            if not emitted:
                # An absent device still produces a live adapter snapshot. This
                # keeps the source present while both channels remain invalid.
                engine.driver_callback("midi", midi.snapshot())
            midi_stop.wait(0.5)

    midi_thread = threading.Thread(target=poll_midi, name="midi-rehearsal", daemon=True)
    midi_thread.start()

    status_stop = threading.Event()

    def write_status() -> None:
        while not status_stop.is_set():
            records = transport.records
            reasons: dict[str, int] = {}
            for record in records:
                reasons[record.reason] = reasons.get(record.reason, 0) + 1
            atomic_json(
                args.artifact_root / "runtime_status.json",
                {
                    "captured_at_us": time.time_ns() // 1000,
                    "engine": engine.snapshot(),
                    "drivers": {
                        "harmocap": {
                            "listener_alive": harmocap_thread.is_alive(),
                            "upstream_stream_id": harmocap.stream_id,
                            "upstream_contract_id": (
                                harmocap.hello.contract_id if harmocap.hello else None
                            ),
                            "stats": vars(harmocap.stats),
                        },
                        "ecg": {
                            "stream_alive": ecg.stream_alive(),
                            "frame_count": ecg._frame_count,
                        },
                        "midi": {
                            "connected": midi.connected,
                            "port_name": midi.port_name,
                            "available_ports": midi.available_ports(),
                            "channels": midi.snapshot(),
                            "last_error": repr(midi.last_error) if midi.last_error else None,
                        },
                    },
                    "transport": {"record_count": len(records), "reasons": reasons},
                },
            )
            status_stop.wait(0.5)

    status_thread = threading.Thread(
        target=write_status, name="rehearsal-status", daemon=True
    )
    status_thread.start()

    atomic_json(
        args.artifact_root / "runtime_ready.json",
        {
            "ready_at_us": time.time_ns() // 1000,
            "run_id": args.run_id,
            "instrument_contract_ids": {
                "beacon-spatial": beacon_contract_id,
                "shaper": shaper_contract_id,
            },
            "source_contract_ids": {
                source_id: manifest["contract_id"]
                for source_id, manifest in source_manifests.items()
            },
            "source_stream_ids": source_streams,
        },
    )

    try:
        import uvicorn

        uvicorn.run(
            create_app(engine),
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            access_log=False,
        )
    finally:
        status_stop.set()
        midi_stop.set()
        status_thread.join(timeout=2.0)
        midi_thread.join(timeout=2.0)
        midi.stop()
        ecg.stop()
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

