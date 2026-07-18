"""One-command, fail-loudly end-to-end integration rehearsal."""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harmonic_weaver.contract_codec import contract_id_from_manifest
from harmonic_weaver.server import PROTOCOL_VERSION, STAGE_CONTRACT_ID

from rehearsal.support import (
    analyze_wav,
    atomic_json,
    beacon_snapshot,
    flatten_diff,
    http_json,
    load_json,
    send_osc,
    wait_http_json,
)


ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT.parent
BEACON = PROJECTS / "beacon-spatial"
SHAPER = PROJECTS / "harmonic-shaper"
HARMOCAP = PROJECTS / "HarMoCAP"
CYMATIC = PROJECTS / "cymatic-control"

SCRIPTED_ASSERTIONS = [
    "preflight.inventory",
    "preflight.file_source_size",
    "preflight.demo_runtime",
    *(f"preflight.executable.{name}" for name in ("pw-jack", "scsynth", "sclang")),
    *(f"preflight.port.{port}" for port in (8765, 8080, 57120, 9002, 9001, 9100, 5001)),
    "contract.beacon.golden",
    "contract.shaper.golden",
    "contract.stage.golden",
    "process.beacon.ready",
    "process.shaper.ready",
    "process.weaver.ready",
    "gate.instrument.beacon-spatial",
    "gate.instrument.shaper",
    "beacon.nature.loaded",
    "beacon.nature.gain_bounded",
    "shaper.five_voices.primed",
    "scene.demo.routes_active",
    "source.midi.hardware_absent_invalid",
    "source.harmocap.replay_flowing",
    "source.ecg.raw_flowing",
    "scene.hot_swap.to_sparse",
    "scene.hot_swap.return_demo",
    "timeline.demo_runtime_ge_90s",
    "route.focused_subject.five_harmonics",
    "route.ecg.rhythmic_pulses",
    "panic.stage.latched_safe",
    "panic.shaper.voices_released",
    "panic.beacon.silence_profile",
    "panic.routes.gated",
    "panic.clear.routes_recovered",
    "panic.clear.shaper_rearmed",
    "panic.clear.beacon_recovered",
    "audio.wav.created",
    "audio.duration",
    "audio.finite",
    "audio.signal_flow",
    "weaver.behavior_reports.present",
    "process.shutdown.all_managed_processes",
]


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[str]
    log_path: Path
    log_handle: Any


class Evidence:
    def __init__(self, run_id: str, artifact_root: Path) -> None:
        self.run_id = run_id
        self.artifact_root = artifact_root
        self.started_monotonic = time.monotonic()
        self.started_at_us = time.time_ns() // 1000
        self.assertions: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.status = "running"
        self.error: str | None = None
        self.audio_stats: dict[str, Any] | None = None

    def event(self, name: str, **details: Any) -> None:
        item = {
            "elapsed_s": round(time.monotonic() - self.started_monotonic, 6),
            "at_us": time.time_ns() // 1000,
            "event": name,
            **details,
        }
        self.timeline.append(item)
        self.flush()

    def check(
        self,
        name: str,
        condition: bool,
        detail: str,
        *artifacts: str | Path,
    ) -> None:
        item = {
            "name": name,
            "status": "PASS" if condition else "FAIL",
            "detail": detail,
            "artifacts": [str(path) for path in artifacts],
            "at_us": time.time_ns() // 1000,
        }
        self.assertions.append(item)
        self.flush()
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    def flush(self) -> None:
        atomic_json(
            self.artifact_root / "results.json",
            {
                "run_id": self.run_id,
                "status": self.status,
                "error": self.error,
                "started_at_us": self.started_at_us,
                "elapsed_s": time.monotonic() - self.started_monotonic,
                "assertions": self.assertions,
                "timeline": self.timeline,
                "audio_stats": self.audio_stats,
            },
        )
        atomic_json(self.artifact_root / "timeline.json", self.timeline)


class StageClient:
    def __init__(self, uri: str) -> None:
        from websockets.sync.client import connect

        self._socket = connect(uri, open_timeout=10.0, close_timeout=3.0)
        self._request_seq = 0
        initial = self._receive()
        if initial.get("type") != "server.hello":
            raise RuntimeError(f"Stage did not begin with server.hello: {initial}")
        self._send(
            "client.hello",
            {
                "client_id": "t4.5-rehearsal",
                "expected_contract_id": STAGE_CONTRACT_ID,
                "supported_protocol_versions": [PROTOCOL_VERSION],
            },
        )
        ready = self._receive()
        if (
            ready.get("type") != "server.hello"
            or ready.get("payload", {}).get("gate_state") != "ready"
            or ready.get("payload", {}).get("contract_id") != STAGE_CONTRACT_ID
        ):
            raise RuntimeError(f"Stage contract gate did not become ready: {ready}")

    def _receive(self) -> dict[str, Any]:
        raw = self._socket.recv(timeout=10.0)
        if not isinstance(raw, str):
            raise RuntimeError("Stage sent a non-text WebSocket message")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError("Stage sent a non-object JSON message")
        return value

    def _send(self, message_type: str, payload: Mapping[str, Any]) -> str:
        self._request_seq += 1
        request_id = f"rehearsal-{self._request_seq}"
        self._socket.send(
            json.dumps(
                {
                    "type": message_type,
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "payload": dict(payload),
                },
                separators=(",", ":"),
            )
        )
        return request_id

    def _response(self, request_id: str, expected_type: str) -> dict[str, Any]:
        while True:
            message = self._receive()
            if message.get("request_id") != request_id:
                continue
            if message.get("type") == "command.error":
                raise RuntimeError(f"Stage command failed: {message['payload']}")
            if message.get("type") != expected_type:
                raise RuntimeError(
                    f"Stage response type {message.get('type')!r}, expected {expected_type!r}"
                )
            return message

    def snapshot(self) -> dict[str, Any]:
        request_id = self._send(
            "state.subscribe",
            {"topics": ["stage", "routes", "scenes", "sources", "instruments", "metrics"]},
        )
        return self._response(request_id, "state.snapshot")

    def command(self, message_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._send(message_type, payload)
        return self._response(request_id, "command.ack")

    def revision(self) -> int:
        return int(self.snapshot()["stage_revision"])

    def upsert_scene(self, scene: Mapping[str, Any]) -> dict[str, Any]:
        return self.command(
            "scene.upsert",
            {"scene": dict(scene), "expected_stage_revision": self.revision()},
        )

    def switch_scene(self, scene_id: str, scene_version: int = 1) -> dict[str, Any]:
        return self.command(
            "scene.switch",
            {
                "scene_id": scene_id,
                "expected_scene_version": scene_version,
                "expected_stage_revision": self.revision(),
            },
        )

    def close(self) -> None:
        self._socket.close()


def _pythonpath(*paths: Path) -> str:
    existing = os.environ.get("PYTHONPATH")
    values = [str(path) for path in paths]
    if existing:
        values.append(existing)
    return os.pathsep.join(values)


def _port_probe(host: str, port: int, socktype: int) -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_INET, socktype) as sock:
            sock.bind((host, port))
    except PermissionError as exc:
        return False, f"loopback socket creation denied by execution environment: {exc}"
    except OSError as exc:
        return False, f"port is unavailable: {exc}"
    return True, "loopback socket created and port available before launch"


def _start_process(
    processes: list[ManagedProcess],
    *,
    name: str,
    command: list[str],
    cwd: Path,
    log_dir: Path,
    env: Mapping[str, str] | None = None,
) -> ManagedProcess:
    log_path = log_dir / f"{name}.log"
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    managed = ManagedProcess(name, process, log_path, handle)
    processes.append(managed)
    return managed


def _assert_processes_alive(evidence: Evidence, processes: list[ManagedProcess]) -> None:
    for managed in processes:
        code = managed.process.poll()
        if code is not None:
            evidence.check(
                f"process.{managed.name}.alive",
                False,
                f"process exited unexpectedly with code {code}",
                managed.log_path,
            )


def _stop_processes(processes: list[ManagedProcess], evidence: Evidence) -> list[str]:
    forced: list[str] = []
    for managed in reversed(processes):
        if managed.process.poll() is None:
            try:
                os.killpg(managed.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 10.0
    for managed in reversed(processes):
        if managed.process.poll() is None:
            try:
                managed.process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                forced.append(managed.name)
                try:
                    os.killpg(managed.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                managed.process.wait(timeout=3.0)
        managed.log_handle.close()
        evidence.event(
            "process_stopped",
            process=managed.name,
            exit_code=managed.process.returncode,
            log=str(managed.log_path),
        )
    return forced


def _wait_with_health(
    seconds: float,
    *,
    evidence: Evidence,
    processes: list[ManagedProcess],
    stage_health: str,
    shaper_health: str,
    beacon_contract_id: str,
) -> float:
    started = time.monotonic()
    deadline = started + seconds
    next_health = started
    while time.monotonic() < deadline:
        _assert_processes_alive(evidence, processes)
        now = time.monotonic()
        if now >= next_health:
            stage = http_json(stage_health)
            shaper = http_json(shaper_health)
            beacon = beacon_snapshot(
                host="127.0.0.1",
                port=57120,
                expected_contract_id=beacon_contract_id,
                timeout=3.0,
            )
            if stage.get("status") != "ok" or "voices" not in shaper:
                raise RuntimeError("periodic Stage/Shaper health assertion failed")
            if beacon["dump"]["value_count"] != 68:
                raise RuntimeError("periodic Beacon state-dump health assertion failed")
            next_health = now + 5.0
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return time.monotonic() - started


def _stage_source(snapshot: Mapping[str, Any], source_id: str) -> dict[str, Any]:
    return next(
        source
        for source in snapshot["payload"]["sources"]
        if source["source_id"] == source_id
    )


def _stage_instrument(snapshot: Mapping[str, Any], instrument_id: str) -> dict[str, Any]:
    return next(
        instrument
        for instrument in snapshot["payload"]["instruments"]
        if instrument["instrument_id"] == instrument_id
    )


def _active_voice_count(shaper_state: Mapping[str, Any], harmonics: range = range(1, 6)) -> int:
    voices = shaper_state.get("voices", {})
    return sum(bool(voices.get(str(harmonic), {}).get("active")) for harmonic in harmonics)


def _route_output_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                if json.loads(line).get("reason") == "route":
                    count += 1
            except json.JSONDecodeError:
                continue
    return count


def _route_outputs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("reason") == "route":
                records.append(item)
    return records


def _activate_shaper_voices(port: int) -> None:
    frequencies = (110.0, 165.0, 220.0, 330.0, 440.0)
    for harmonic, frequency in enumerate(frequencies, 1):
        send_osc(
            "127.0.0.1",
            port,
            "/beacon/voice/on",
            [100 + harmonic, frequency, 0.5, harmonic, harmonic],
        )


def _capture(
    label: str,
    *,
    stage: StageClient,
    beacon_contract_id: str,
    artifact_root: Path,
) -> dict[str, Any]:
    state_root = artifact_root / "states"
    state_root.mkdir(parents=True, exist_ok=True)
    captured = {
        "stage": stage.snapshot(),
        "beacon": beacon_snapshot(
            host="127.0.0.1",
            port=57120,
            expected_contract_id=beacon_contract_id,
            timeout=5.0,
        ),
        "shaper": http_json("http://127.0.0.1:8080/api/state"),
    }
    for component, value in captured.items():
        atomic_json(state_root / f"{label}.{component}.json", value)
    return captured


def _critical_diffs(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "stage": {
            "before": before["stage"]["payload"]["stage"],
            "after": after["stage"]["payload"]["stage"],
        },
        "beacon": flatten_diff(
            before["beacon"]["dump"]["values"],
            after["beacon"]["dump"]["values"],
        ),
        "shaper": flatten_diff(before["shaper"], after["shaper"]),
    }


def _artifact_tree(root: Path) -> list[tuple[str, int]]:
    items: list[tuple[str, int]] = []
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                items.append((str(path.relative_to(ROOT)), path.stat().st_size))
    return items


def _record_not_run_assertions(evidence: Evidence) -> None:
    recorded = {item["name"] for item in evidence.assertions}
    for name in SCRIPTED_ASSERTIONS:
        if name not in recorded:
            evidence.assertions.append(
                {
                    "name": name,
                    "status": "NOT RUN",
                    "detail": "live sequence aborted before this assertion",
                    "artifacts": [],
                    "at_us": time.time_ns() // 1000,
                }
            )


def _render_report(evidence: Evidence, weaver_report: Path) -> None:
    report_path = ROOT / "rehearsal" / "REPORT.md"
    assertion_lines = [
        f"| {item['status']} | `{item['name']}` | {item['detail'].replace('|', '/')} |"
        for item in evidence.assertions
    ]
    timeline_lines = [
        f"| {item['elapsed_s']:.3f} | `{item['event']}` | "
        + ", ".join(
            f"{key}={value}"
            for key, value in item.items()
            if key not in {"elapsed_s", "at_us", "event"}
        ).replace("|", "/")
        + " |"
        for item in evidence.timeline
    ]
    audio = evidence.audio_stats or {}
    audio_lines = (
        f"- Duration: `{audio.get('duration_s', 'not measured')}` seconds\n"
        f"- Non-silence ratio: `{audio.get('non_silence_ratio', 'not measured')}` "
        f"at absolute threshold `{audio.get('non_silence_threshold', 'not measured')}`\n"
        f"- Peak absolute sample: `{audio.get('peak_abs', 'not measured')}`\n"
        f"- RMS: `{audio.get('rms', 'not measured')}`\n"
        f"- NaN / Inf: `{audio.get('nan_count', 'not measured')}` / "
        f"`{audio.get('inf_count', 'not measured')}`"
    )
    artifacts = _artifact_tree(evidence.artifact_root) + _artifact_tree(weaver_report)
    artifact_lines = [f"- `{path}` ({size} bytes)" for path, size in artifacts]
    result = evidence.status.upper()
    weaver_report_label = str(weaver_report.relative_to(ROOT))
    if not weaver_report.exists():
        weaver_report_label += " (not created before abort)"
    runtime_heading = "Runtime declaration" if evidence.status == "pass" else "Planned runtime and abort point"
    runtime_intro = (
        "The full runtime completed as declared below."
        if evidence.status == "pass"
        else f"The live sequence did not complete. Abort reason: `{evidence.error}`. The text below describes the configured sequence, not completed evidence."
    )
    audio_intro = (
        "SuperCollider recorded its master output. These numbers prove a finite, non-silent signal was written; they do not claim a person heard it."
        if evidence.audio_stats is not None
        else "No analyzable rehearsal WAV was produced before this run aborted."
    )
    diff_intro = (
        f"Machine-readable pre/post swap, panic, and recovery diffs are in `{(evidence.artifact_root / 'state_diffs.json').relative_to(ROOT)}`."
        if (evidence.artifact_root / "state_diffs.json").exists()
        else "No state-dump diffs were captured before this run aborted."
    )
    text = f"""# T4.5 end-to-end integration rehearsal

Result: **{result}**  
Run ID: `{evidence.run_id}`  
Weaver report: `{weaver_report_label}`  
Rehearsal evidence: `{evidence.artifact_root.relative_to(ROOT)}`

## {runtime_heading}

{runtime_intro}

Beacon is configured through the canonical `start-beacon.sh --file --no-https`
launcher with the 659 MB file-mode source. Shaper is configured headless with
`--no-midi --no-audio --slave`; its state API, not an audio device, is the
evidence plane. Weaver is configured with its normal Stage WebSocket API and
the HarMoCAP, MIDI, and ECG drivers installed. HarMoCAP uses the real
`two_persons.jsonl` kit replay over OSC. ECG uses a deterministic synthetic
raw-ADC stream over `/ecg/raw` into the production ECG driver. MIDI has no
hardware and its invalid channels are an expected assertion.

Repository inspection found that `cymatic-control/test_ecg_stream.py` is a
receiver/terminal diagnostic, despite the supplied inventory calling it an ECG
simulator. It cannot generate `/ecg/raw`. The rehearsal therefore uses
`rehearsal/ecg_simulator.py`, whose deterministic waveform comes from the
production driver's synthetic-ECG helper, and records this inventory mismatch
instead of pretending the diagnostic sends data. `simulate_eeg.py` is not
started because EEG is outside the Weaver driver set.

The configured Beacon gate requires its real OSC hello and atomic
contract-gated state dump. Shaper's exact v1 manifest explicitly declares that
OSC hello is not currently implemented, so the configured Weaver adapter gates
the exact manifest contract ID after the manifest-declared HTTP state snapshot.
This limitation is not hidden.

## Timeline

| Elapsed (s) | Event | Detail |
|---:|---|---|
{chr(10).join(timeline_lines)}

## Scripted assertions

| Result | Assertion | Evidence |
|---|---|---|
{chr(10).join(assertion_lines)}

## Audio statistics

{audio_intro}

{audio_lines}

## State-dump diffs and panic/recovery

{diff_intro} The exact Stage, Beacon, and Shaper snapshots named in the artifact
tree are the primary evidence. Panic assertions require Shaper voices inactive,
Beacon master and nature gain at zero, no route transport writes during a
three-second gated window, and route writes resuming after `panic.clear`.

## Artifact tree

{chr(10).join(artifact_lines)}

## Explicitly unverified

- Audible monitoring or subjective audio quality by a human.
- R24 live input or any other audio-interface input.
- Physical MIDI hardware, camera input, live people, ESP32/AD8232 hardware,
  EEG hardware, or EEG routing.
- Shaper real-time audio output; Shaper is configured with `--no-audio` by design.
- Shaper OSC hello/state dump, because the installed real manifest marks that
  wire handshake as planned rather than implemented.
- The supplied characterization of `cymatic-control/test_ecg_stream.py` as a
  simulator; the file is verified to be a listener, so it is inventory-only.
"""
    report_path.write_text(text, encoding="utf-8")


def run() -> int:
    run_id = time.strftime("t45-%Y%m%dT%H%M%SZ", time.gmtime())
    artifact_root = ROOT / "rehearsal" / "artifacts" / run_id
    log_dir = artifact_root / "logs"
    audio_dir = artifact_root / "audio"
    log_dir.mkdir(parents=True)
    audio_dir.mkdir(parents=True)
    evidence = Evidence(run_id, artifact_root)
    processes: list[ManagedProcess] = []
    stage: StageClient | None = None
    weaver_report = ROOT / "reports" / run_id
    recording_started = False

    beacon_manifest_path = BEACON / "beacon_spatial.contract.json"
    shaper_manifest_path = SHAPER / "contracts" / "shaper.contract.json"
    fixture_path = HARMOCAP / "examples" / "fixtures" / "two_persons.jsonl"
    replay_path = HARMOCAP / "harmocap-nico-kit" / "replay.py"
    nature_path = BEACON / "assets" / "nature-samples" / "dominicalito_frogs_pond.wav"
    file_source_path = BEACON / "harmonic_beacon_2026_05_13_session.wav"
    audio_path = audio_dir / "beacon_master_rehearsal.wav"
    output_audit = artifact_root / "instrument_outputs.jsonl"
    # NOTE: do not resolve() this path — venv interpreters are symlinks, and
    # dereferencing them drops the venv site-packages for child processes.
    python = Path(os.environ.get("REHEARSAL_PYTHON", sys.executable))
    demo_segment_s = float(os.environ.get("REHEARSAL_DEMO_SEGMENT_S", "45"))
    sparse_s = float(os.environ.get("REHEARSAL_SPARSE_S", "10"))

    try:
        required = [
            python,
            BEACON / "start-beacon.sh",
            beacon_manifest_path,
            BEACON / "beacon_spatial.contract_id.golden",
            shaper_manifest_path,
            SHAPER / "contracts" / "shaper.contract_id.golden",
            fixture_path,
            replay_path,
            CYMATIC / "test_ecg_stream.py",
            CYMATIC / "simulate_eeg.py",
            nature_path,
            file_source_path,
        ]
        missing = [str(path) for path in required if not path.exists()]
        evidence.check("preflight.inventory", not missing, f"missing={missing}")
        evidence.check(
            "preflight.file_source_size",
            file_source_path.stat().st_size > 600_000_000,
            f"bytes={file_source_path.stat().st_size}",
            file_source_path,
        )
        evidence.check(
            "preflight.demo_runtime",
            demo_segment_s * 2 >= 90.0,
            f"declared cumulative demo runtime={demo_segment_s * 2:.3f}s",
        )
        for executable in ("pw-jack", "scsynth", "sclang"):
            evidence.check(
                f"preflight.executable.{executable}",
                shutil.which(executable) is not None,
                f"resolved={shutil.which(executable)}",
            )
        for port, socktype in (
            (8765, socket.SOCK_STREAM),
            (8080, socket.SOCK_STREAM),
            (57120, socket.SOCK_DGRAM),
            (9002, socket.SOCK_DGRAM),
            (9001, socket.SOCK_DGRAM),
            (9100, socket.SOCK_DGRAM),
            (5001, socket.SOCK_DGRAM),
        ):
            port_ready, port_detail = _port_probe("127.0.0.1", port, socktype)
            evidence.check(
                f"preflight.port.{port}",
                port_ready,
                port_detail,
            )

        beacon_manifest = load_json(beacon_manifest_path)
        shaper_manifest = load_json(shaper_manifest_path)
        beacon_contract_id = contract_id_from_manifest(beacon_manifest)
        shaper_contract_id = contract_id_from_manifest(shaper_manifest)
        evidence.check(
            "contract.beacon.golden",
            beacon_contract_id
            == (BEACON / "beacon_spatial.contract_id.golden").read_text().strip(),
            f"contract_id={beacon_contract_id}",
            beacon_manifest_path,
        )
        evidence.check(
            "contract.shaper.golden",
            shaper_contract_id
            == (SHAPER / "contracts" / "shaper.contract_id.golden").read_text().strip(),
            f"contract_id={shaper_contract_id}",
            shaper_manifest_path,
        )
        evidence.check(
            "contract.stage.golden",
            STAGE_CONTRACT_ID
            == (ROOT / "contracts" / "stage.contract_id.golden").read_text().strip(),
            f"contract_id={STAGE_CONTRACT_ID}",
        )
        atomic_json(
            artifact_root / "run_manifest.json",
            {
                "run_id": run_id,
                "python": str(python),
                "modes": {
                    "beacon": "--file --no-https",
                    "shaper": "--no-midi --no-audio --slave",
                    "weaver_drivers": ["harmocap", "midi", "ecg"],
                    "hardware": False,
                },
                "paths": {str(path.name): str(path) for path in required},
                "contract_ids": {
                    "stage": STAGE_CONTRACT_ID,
                    "beacon-spatial": beacon_contract_id,
                    "shaper": shaper_contract_id,
                },
                "timing": {
                    "demo_segment_s": demo_segment_s,
                    "sparse_s": sparse_s,
                    "declared_demo_total_s": demo_segment_s * 2,
                },
            },
        )

        base_env = dict(os.environ)
        _start_process(
            processes,
            name="beacon",
            command=[str(BEACON / "start-beacon.sh"), "--file", "--no-https"],
            cwd=BEACON,
            log_dir=log_dir,
            env=base_env,
        )
        evidence.event("process_started", process="beacon", mode="--file --no-https")
        beacon_live = None
        deadline = time.monotonic() + 75.0
        while time.monotonic() < deadline:
            _assert_processes_alive(evidence, processes)
            try:
                beacon_live = beacon_snapshot(
                    host="127.0.0.1",
                    port=57120,
                    expected_contract_id=beacon_contract_id,
                    timeout=2.0,
                )
                break
            except Exception:
                time.sleep(0.5)
        evidence.check(
            "process.beacon.ready",
            beacon_live is not None,
            "real hello and atomic state dump completed",
            log_dir / "beacon.log",
        )

        shaper_env = dict(base_env)
        shaper_env["PYTHONPATH"] = _pythonpath(SHAPER / "src")
        _start_process(
            processes,
            name="shaper",
            command=[
                str(python),
                "-m",
                "harmonic_shaper",
                "--no-midi",
                "--no-audio",
                "--slave",
                "--api-host",
                "127.0.0.1",
                "--api-port",
                "8080",
                "--osc-host",
                "127.0.0.1",
            ],
            cwd=SHAPER,
            log_dir=log_dir,
            env=shaper_env,
        )
        evidence.event("process_started", process="shaper", mode="--no-midi --no-audio --slave")
        wait_http_json("http://127.0.0.1:8080/api/state", 15.0)
        _assert_processes_alive(evidence, processes)
        evidence.check(
            "process.shaper.ready",
            True,
            "HTTP state API ready with audio and MIDI disabled",
            log_dir / "shaper.log",
        )

        weaver_env = dict(base_env)
        weaver_env["PYTHONPATH"] = _pythonpath(ROOT / "src", ROOT)
        _start_process(
            processes,
            name="weaver",
            command=[
                str(python),
                "-m",
                "rehearsal.weaver_runtime",
                "--run-id",
                run_id,
                "--artifact-root",
                str(artifact_root),
                "--beacon-manifest",
                str(beacon_manifest_path),
                "--shaper-manifest",
                str(shaper_manifest_path),
                "--max-runtime-s",
                str(demo_segment_s * 2 + sparse_s + 90.0),
            ],
            cwd=ROOT,
            log_dir=log_dir,
            env=weaver_env,
        )
        evidence.event("process_started", process="weaver", drivers="harmocap,midi,ecg")
        health = wait_http_json("http://127.0.0.1:8765/health", 20.0)
        evidence.check(
            "process.weaver.ready",
            health.get("contract_id") == STAGE_CONTRACT_ID,
            f"health={health}",
            log_dir / "weaver.log",
        )

        _start_process(
            processes,
            name="harmocap-replay",
            command=[
                str(python),
                str(replay_path),
                str(fixture_path),
                "--host",
                "127.0.0.1",
                "--port",
                "9100",
                "--loop",
            ],
            cwd=replay_path.parent,
            log_dir=log_dir,
            env=weaver_env,
        )
        _start_process(
            processes,
            name="ecg-simulator",
            command=[
                str(python),
                "-m",
                "rehearsal.ecg_simulator",
                "--host",
                "127.0.0.1",
                "--port",
                "5001",
                "--duration-s",
                str(demo_segment_s * 2 + sparse_s + 90.0),
            ],
            cwd=ROOT,
            log_dir=log_dir,
            env=weaver_env,
        )
        evidence.event("sources_started", harmocap_fixture=str(fixture_path), ecg_bpm=72.0)

        stage = StageClient("ws://127.0.0.1:8765/ws")
        initial_stage = stage.snapshot()
        for instrument_id, contract_id in (
            ("beacon-spatial", beacon_contract_id),
            ("shaper", shaper_contract_id),
        ):
            instrument = _stage_instrument(initial_stage, instrument_id)
            evidence.check(
                f"gate.instrument.{instrument_id}",
                instrument["gate_state"] == "ready"
                and instrument["state_synced"] is True
                and instrument["runtime_contract_id"] == contract_id
                and instrument["expected_contract_id"] == contract_id,
                f"gate_state={instrument['gate_state']} contract_id={contract_id}",
                artifact_root / f"{instrument_id.replace('-', '_')}_runtime_sync.json",
            )

        # Load the independent nature layer through its real string OSC command
        # and wait until the atomic Beacon state dump shows the accepted path.
        send_osc("127.0.0.1", 57120, "/beacon/nature/load", [str(nature_path.resolve())])
        send_osc("127.0.0.1", 57120, "/beacon/nature/gain", [0.12])
        nature_state = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            nature_state = beacon_snapshot(
                host="127.0.0.1",
                port=57120,
                expected_contract_id=beacon_contract_id,
                timeout=3.0,
            )
            values = nature_state["dump"]["values"]
            if values.get("/beacon/nature/load") == str(nature_path.resolve()):
                break
            time.sleep(0.25)
        evidence.check(
            "beacon.nature.loaded",
            nature_state is not None
            and nature_state["dump"]["values"].get("/beacon/nature/load")
            == str(nature_path.resolve()),
            f"path={str(nature_path.resolve())}",
            nature_path,
        )
        nature_gain = float(nature_state["dump"]["values"]["/beacon/nature/gain"])
        evidence.check(
            "beacon.nature.gain_bounded",
            0.0 <= nature_gain <= 1.0 and math.isclose(nature_gain, 0.12, abs_tol=1e-4),
            f"gain={nature_gain}",
        )

        _activate_shaper_voices(9001)
        time.sleep(0.5)
        initial_shaper = http_json("http://127.0.0.1:8080/api/state")
        evidence.check(
            "shaper.five_voices.primed",
            _active_voice_count(initial_shaper) == 5,
            f"active_voices={_active_voice_count(initial_shaper)}",
        )

        demo_scene = load_json(ROOT / "rehearsal" / "scenes" / "event_demo.scene.json")
        sparse_scene = load_json(ROOT / "rehearsal" / "scenes" / "sparse.scene.json")
        stage.upsert_scene(demo_scene)
        stage.upsert_scene(sparse_scene)
        stage.switch_scene("event-demo")
        evidence.event("scene_switched", scene="event-demo", segment=1)

        send_osc("127.0.0.1", 57120, "/beacon/record/start", [str(audio_path)])
        recording_started = True
        evidence.event("recording_started", path=str(audio_path))
        time.sleep(1.0)

        t0 = _capture(
            "t0",
            stage=stage,
            beacon_contract_id=beacon_contract_id,
            artifact_root=artifact_root,
        )
        evidence.event("state_captured", label="t0")
        evidence.check(
            "scene.demo.routes_active",
            len(t0["stage"]["payload"]["routes"]) == 7
            and all(route["runtime"]["active"] for route in t0["stage"]["payload"]["routes"]),
            f"active_scene={t0['stage']['payload']['stage']['active_scene_id']}",
            artifact_root / "states" / "t0.stage.json",
        )
        midi = _stage_source(t0["stage"], "midi")
        evidence.check(
            "source.midi.hardware_absent_invalid",
            midi["channels"]["cc_1"]["state"] == "invalid"
            and midi["channels"]["modwheel"]["state"] == "invalid",
            "cc_1 and modwheel are invalid as expected without MIDI hardware",
            artifact_root / "states" / "t0.stage.json",
        )
        harmocap = _stage_source(t0["stage"], "harmocap")
        ecg = _stage_source(t0["stage"], "ecg")
        evidence.check(
            "source.harmocap.replay_flowing",
            harmocap["last_frame_seq"] > 0
            and harmocap["channels"]["slot_0_present"]["state"] == "observed",
            f"last_frame_seq={harmocap['last_frame_seq']}",
            fixture_path,
        )
        evidence.check(
            "source.ecg.raw_flowing",
            ecg["last_frame_seq"] > 0
            and ecg["channels"]["signal_quality"]["state"] == "observed",
            f"last_frame_seq={ecg['last_frame_seq']}",
            log_dir / "ecg-simulator.log",
        )

        first_demo_elapsed = _wait_with_health(
            demo_segment_s,
            evidence=evidence,
            processes=processes,
            stage_health="http://127.0.0.1:8765/health",
            shaper_health="http://127.0.0.1:8080/api/state",
            beacon_contract_id=beacon_contract_id,
        )
        mid_pre = _capture(
            "mid_pre_swap",
            stage=stage,
            beacon_contract_id=beacon_contract_id,
            artifact_root=artifact_root,
        )
        pre_generation = mid_pre["stage"]["payload"]["stage"]["activation_generation"]
        stage.switch_scene("sparse")
        mid_sparse = _capture(
            "mid_post_swap_sparse",
            stage=stage,
            beacon_contract_id=beacon_contract_id,
            artifact_root=artifact_root,
        )
        evidence.event("scene_switched", scene="sparse", phase="hot_swap")
        evidence.check(
            "scene.hot_swap.to_sparse",
            mid_sparse["stage"]["payload"]["stage"]["active_scene_id"] == "sparse"
            and mid_sparse["stage"]["payload"]["stage"]["activation_generation"]
            == pre_generation + 1,
            "active scene changed atomically and activation generation incremented",
            artifact_root / "states" / "mid_pre_swap.stage.json",
            artifact_root / "states" / "mid_post_swap_sparse.stage.json",
        )
        _wait_with_health(
            sparse_s,
            evidence=evidence,
            processes=processes,
            stage_health="http://127.0.0.1:8765/health",
            shaper_health="http://127.0.0.1:8080/api/state",
            beacon_contract_id=beacon_contract_id,
        )
        stage.switch_scene("event-demo")
        mid_return = _capture(
            "mid_return_demo",
            stage=stage,
            beacon_contract_id=beacon_contract_id,
            artifact_root=artifact_root,
        )
        evidence.event("scene_switched", scene="event-demo", segment=2)
        evidence.check(
            "scene.hot_swap.return_demo",
            mid_return["stage"]["payload"]["stage"]["active_scene_id"] == "event-demo",
            "demo scene restored after sparse interlude",
            artifact_root / "states" / "mid_return_demo.stage.json",
        )
        second_demo_elapsed = _wait_with_health(
            demo_segment_s,
            evidence=evidence,
            processes=processes,
            stage_health="http://127.0.0.1:8765/health",
            shaper_health="http://127.0.0.1:8080/api/state",
            beacon_contract_id=beacon_contract_id,
        )
        demo_elapsed = first_demo_elapsed + second_demo_elapsed
        evidence.check(
            "timeline.demo_runtime_ge_90s",
            demo_elapsed >= 90.0,
            f"measured cumulative demo runtime={demo_elapsed:.6f}s",
        )
        end = _capture(
            "end",
            stage=stage,
            beacon_contract_id=beacon_contract_id,
            artifact_root=artifact_root,
        )
        evidence.event("state_captured", label="end")

        route_records = _route_outputs(output_audit)
        five_harmonics = {
            int(record["bindings"]["N"])
            for record in route_records
            if record.get("instrument_id") == "shaper"
            and record.get("capability") == "harmonic_gain"
        }
        evidence.check(
            "route.focused_subject.five_harmonics",
            five_harmonics == {1, 2, 3, 4, 5},
            f"observed_harmonics={sorted(five_harmonics)}",
            output_audit,
        )
        ecg_pulses = [
            record
            for record in route_records
            if record.get("capability") == "nature_gain"
            and float(record.get("value", 0.0)) > 0.9
        ]
        evidence.check(
            "route.ecg.rhythmic_pulses",
            len(ecg_pulses) >= 3,
            f"full-gain beat pulses={len(ecg_pulses)}",
            output_audit,
        )

        panic_ack = stage.command("panic.trigger", {"reason": "T4.5 scripted panic"})
        panic_generation = int(panic_ack["payload"]["panic_generation"])
        time.sleep(0.5)
        panic = _capture(
            "panic",
            stage=stage,
            beacon_contract_id=beacon_contract_id,
            artifact_root=artifact_root,
        )
        evidence.event("panic_triggered", panic_generation=panic_generation)
        outcomes = panic["stage"]["payload"]["stage"]["panic"]["outcomes"]
        evidence.check(
            "panic.stage.latched_safe",
            panic["stage"]["payload"]["stage"]["panic"]["active"] is True
            and outcomes == {"beacon-spatial": "ok", "shaper": "ok"},
            f"outcomes={outcomes}",
            artifact_root / "states" / "panic.stage.json",
        )
        evidence.check(
            "panic.shaper.voices_released",
            _active_voice_count(panic["shaper"]) == 0,
            f"active_voices={_active_voice_count(panic['shaper'])}",
            artifact_root / "states" / "panic.shaper.json",
        )
        panic_beacon = panic["beacon"]["dump"]["values"]
        evidence.check(
            "panic.beacon.silence_profile",
            math.isclose(float(panic_beacon["/beacon/master"]), 0.0, abs_tol=1e-6)
            and math.isclose(
                float(panic_beacon["/beacon/nature/gain"]), 0.0, abs_tol=1e-6
            ),
            f"master={panic_beacon['/beacon/master']} nature_gain={panic_beacon['/beacon/nature/gain']}",
            artifact_root / "states" / "panic.beacon.json",
        )
        route_count_at_panic = _route_output_count(output_audit)
        _wait_with_health(
            3.0,
            evidence=evidence,
            processes=processes,
            stage_health="http://127.0.0.1:8765/health",
            shaper_health="http://127.0.0.1:8080/api/state",
            beacon_contract_id=beacon_contract_id,
        )
        route_count_after_gate = _route_output_count(output_audit)
        evidence.check(
            "panic.routes.gated",
            route_count_after_gate == route_count_at_panic,
            f"route writes stayed at {route_count_at_panic} for 3 seconds while sources continued",
            output_audit,
        )

        clear_ack = stage.command(
            "panic.clear",
            {
                "panic_generation": panic_generation,
                "scene_id": "event-demo",
                "expected_scene_version": 1,
            },
        )
        evidence.event("panic_cleared", ack=clear_ack["payload"])
        _wait_with_health(
            3.0,
            evidence=evidence,
            processes=processes,
            stage_health="http://127.0.0.1:8765/health",
            shaper_health="http://127.0.0.1:8080/api/state",
            beacon_contract_id=beacon_contract_id,
        )
        route_count_recovered = _route_output_count(output_audit)
        _activate_shaper_voices(9001)
        time.sleep(0.5)
        recovery = _capture(
            "recovery",
            stage=stage,
            beacon_contract_id=beacon_contract_id,
            artifact_root=artifact_root,
        )
        evidence.check(
            "panic.clear.routes_recovered",
            recovery["stage"]["payload"]["stage"]["panic"]["active"] is False
            and route_count_recovered > route_count_after_gate,
            f"route writes before={route_count_after_gate} after={route_count_recovered}",
            output_audit,
            artifact_root / "states" / "recovery.stage.json",
        )
        evidence.check(
            "panic.clear.shaper_rearmed",
            _active_voice_count(recovery["shaper"]) == 5,
            f"active_voices={_active_voice_count(recovery['shaper'])}",
            artifact_root / "states" / "recovery.shaper.json",
        )
        recovered_master = float(
            recovery["beacon"]["dump"]["values"]["/beacon/master"]
        )
        evidence.check(
            "panic.clear.beacon_recovered",
            recovered_master > 0.0,
            f"master={recovered_master}",
            artifact_root / "states" / "recovery.beacon.json",
        )

        atomic_json(
            artifact_root / "state_diffs.json",
            {
                "hot_swap_to_sparse": _critical_diffs(mid_pre, mid_sparse),
                "hot_swap_return_demo": _critical_diffs(mid_sparse, mid_return),
                "panic": _critical_diffs(end, panic),
                "recovery": _critical_diffs(panic, recovery),
            },
        )

        send_osc("127.0.0.1", 57120, "/beacon/record/stop", [])
        recording_started = False
        evidence.event("recording_stopped", path=str(audio_path))
        time.sleep(2.0)
        evidence.check(
            "audio.wav.created",
            audio_path.exists() and audio_path.stat().st_size > 44,
            f"exists={audio_path.exists()} bytes={audio_path.stat().st_size if audio_path.exists() else 0}",
            audio_path,
        )
        evidence.audio_stats = analyze_wav(audio_path)
        atomic_json(artifact_root / "audio_stats.json", evidence.audio_stats)
        stats = evidence.audio_stats
        evidence.check(
            "audio.duration",
            stats["duration_s"] >= demo_elapsed + sparse_s,
            f"duration={stats['duration_s']:.6f}s required>={demo_elapsed + sparse_s:.6f}s",
            artifact_root / "audio_stats.json",
        )
        evidence.check(
            "audio.finite",
            stats["all_finite"] and stats["nan_count"] == 0 and stats["inf_count"] == 0,
            f"nan={stats['nan_count']} inf={stats['inf_count']}",
            artifact_root / "audio_stats.json",
        )
        evidence.check(
            "audio.signal_flow",
            stats["peak_abs"] > 1e-5
            and stats["rms"] > 1e-6
            and stats["non_silence_ratio"] > 0.01,
            f"peak={stats['peak_abs']:.9f} rms={stats['rms']:.9f} non_silence_ratio={stats['non_silence_ratio']:.9f}",
            artifact_root / "audio_stats.json",
        )

        final_runtime = load_json(artifact_root / "runtime_status.json")
        atomic_json(artifact_root / "runtime_status.final.json", final_runtime)
        evidence.check(
            "weaver.behavior_reports.present",
            all(
                (weaver_report / name).exists()
                for name in (
                    "behavior_events.jsonl",
                    "state_timestamps.jsonl",
                    "accepted_contract_ids.json",
                    "scene_snapshot.json",
                    "summary.json",
                )
            ),
            f"report_root={weaver_report}",
            weaver_report,
        )
        evidence.status = "pass"
        evidence.event("rehearsal_complete", result="PASS")
    except Exception as exc:
        evidence.status = "fail"
        evidence.error = f"{type(exc).__name__}: {exc}"
        if not evidence.assertions or evidence.assertions[-1]["status"] != "FAIL":
            evidence.assertions.append(
                {
                    "name": "harness.unexpected_error",
                    "status": "FAIL",
                    "detail": evidence.error,
                    "artifacts": [str(log_dir)],
                    "at_us": time.time_ns() // 1000,
                }
            )
        evidence.event("rehearsal_aborted", error=evidence.error)
    finally:
        if recording_started:
            try:
                send_osc("127.0.0.1", 57120, "/beacon/record/stop", [])
                time.sleep(1.0)
            except Exception:
                pass
        if stage is not None:
            try:
                stage.close()
            except Exception:
                pass
        forced_shutdowns = _stop_processes(processes, evidence)
        clean_shutdown = not forced_shutdowns and all(
            managed.process.poll() is not None for managed in processes
        )
        evidence.assertions.append(
            {
                "name": "process.shutdown.all_managed_processes",
                "status": "PASS" if clean_shutdown else "FAIL",
                "detail": (
                    "all managed process groups stopped after SIGTERM"
                    if clean_shutdown
                    else f"forced SIGKILL required for {forced_shutdowns}"
                ),
                "artifacts": [str(log_dir)],
                "at_us": time.time_ns() // 1000,
            }
        )
        if not clean_shutdown:
            evidence.status = "fail"
            evidence.error = f"forced process shutdowns: {forced_shutdowns}"
        _record_not_run_assertions(evidence)
        for source in (Path("/tmp/scsynth.log"), Path("/tmp/sclang.log"), Path("/tmp/webui.log")):
            if source.exists():
                try:
                    shutil.copy2(source, log_dir / f"beacon-{source.name}")
                except OSError:
                    pass
        evidence.flush()
        _render_report(evidence, weaver_report)

    print(f"T4.5 rehearsal {evidence.status.upper()}: {run_id}")
    print(f"Report: {ROOT / 'rehearsal' / 'REPORT.md'}")
    print(f"Artifacts: {artifact_root}")
    return 0 if evidence.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(run())
