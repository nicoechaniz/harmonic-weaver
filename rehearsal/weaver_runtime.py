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


def harmocap_manifest() -> dict[str, Any]:
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
        specs.append((name, bounds))
    return source_manifest("harmocap", specs, lease_ms=2500.0, rate_hz=60.0)


def ecg_manifest() -> dict[str, Any]:
    return source_manifest(
        "ecg",
        (
            ("beat", (0.0, 1.0)),
            ("bpm", (20.0, 300.0)),
            ("signal_quality", (0.0, 1.0)),
        ),
        lease_ms=2500.0,
        rate_hz=31.25,
    )


def midi_manifest() -> dict[str, Any]:
    return source_manifest(
        "midi",
        (("cc_1", (0.0, 1.0)), ("modwheel", (0.0, 1.0))),
        lease_ms=2500.0,
        rate_hz=2.0,
    )


def shaper_safety_profile(contract_id: str) -> dict[str, Any]:
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
        "reset_defaults": [
            {
                "capability": "harmonic_gain",
                "bindings": {"N": harmonic},
                "argument": "gain",
                "value": 0.0,
            }
            for harmonic in range(1, 6)
        ],
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harmonic Weaver live rehearsal runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--report-root", type=Path, default=REPO_ROOT / "reports")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--beacon-manifest", type=Path, required=True)
    parser.add_argument("--shaper-manifest", type=Path, required=True)
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
    parser.add_argument("--log-level", default="info")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        "harmocap": harmocap_manifest(),
        "ecg": ecg_manifest(),
        "midi": midi_manifest(),
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

