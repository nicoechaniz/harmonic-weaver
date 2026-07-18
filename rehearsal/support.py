"""Shared, dependency-light primitives for the live rehearsal."""

from __future__ import annotations

import json
import math
import os
import socket
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from harmonic_weaver.contract_codec import (
    canonical_json_dumps,
    decode_bundle,
    encode_message,
)
from harmonic_weaver.engine.transport import OutputRecord


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def atomic_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(canonical_json_dumps(value) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def append_jsonl(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json_dumps(dict(value)) + "\n")


def http_json(url: str, timeout: float = 3.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"GET {url} returned HTTP {response.status}")
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"GET {url} did not return a JSON object")
    return value


def wait_http_json(url: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return http_json(url)
        except Exception as exc:  # service startup is expected to race this probe
            last_error = exc
            time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {url}: {last_error}")


def send_osc(host: str, port: int, address: str, args: list[Any]) -> None:
    packet = encode_message(address, args)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(packet, (host, port))


def _receive_matching(
    sock: socket.socket,
    predicate: Any,
    deadline: float,
) -> list[tuple[str, list[Any]]]:
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, min(0.5, deadline - time.monotonic())))
        try:
            packet, _peer = sock.recvfrom(65535)
        except socket.timeout:
            continue
        messages = decode_bundle(packet)
        if predicate(messages):
            return messages
    raise TimeoutError("timed out waiting for matching OSC response")


def beacon_snapshot(
    *,
    host: str,
    port: int,
    expected_contract_id: str,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Perform the real Beacon hello gate and one contract-gated state dump."""

    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        hello: list[Any] | None = None
        while hello is None and time.monotonic() < deadline:
            sock.sendto(encode_message("/beacon/hello/request", []), (host, port))
            try:
                messages = _receive_matching(
                    sock,
                    lambda items: any(address == "/beacon/hello" for address, _ in items),
                    min(deadline, time.monotonic() + 1.0),
                )
            except TimeoutError:
                continue
            hello = next(args for address, args in messages if address == "/beacon/hello")
        if hello is None:
            raise TimeoutError("Beacon did not answer /beacon/hello/request")
        if len(hello) != 6:
            raise RuntimeError(f"Beacon hello has {len(hello)} args, expected 6")
        instrument_id, stream_id, schema_version, producer_version, contract_id, state_seq = hello
        if instrument_id != "beacon-spatial":
            raise RuntimeError(f"unexpected Beacon instrument_id {instrument_id!r}")
        if contract_id != expected_contract_id:
            raise RuntimeError(
                f"Beacon contract mismatch: runtime={contract_id}, expected={expected_contract_id}"
            )
        sock.sendto(
            encode_message("/beacon/state", [expected_contract_id]),
            (host, port),
        )

        def complete_dump(items: list[tuple[str, list[Any]]]) -> bool:
            addresses = [address for address, _args in items]
            return "/beacon/state/begin" in addresses and "/beacon/state/end" in addresses

        messages = _receive_matching(sock, complete_dump, deadline)
        begin = next(args for address, args in messages if address == "/beacon/state/begin")
        end = next(args for address, args in messages if address == "/beacon/state/end")
        if len(begin) != 3 or len(end) != 2:
            raise RuntimeError("Beacon state dump boundary has an invalid shape")
        if begin[0] != stream_id or end[0] != stream_id or begin[1] != end[1]:
            raise RuntimeError("Beacon state dump is not stream/sequence atomic")
        if begin[2] != expected_contract_id:
            raise RuntimeError("Beacon state dump contract_id mismatch")
        values: dict[str, Any] = {}
        for address, args in messages:
            if address.startswith("/beacon/state/"):
                continue
            if len(args) == 1:
                values[address] = args[0]
        return {
            "captured_at_us": time.time_ns() // 1000,
            "hello": {
                "instrument_id": instrument_id,
                "stream_id": stream_id,
                "schema_version": schema_version,
                "producer_version": producer_version,
                "contract_id": contract_id,
                "state_seq": state_seq,
            },
            "dump": {
                "stream_id": begin[0],
                "state_seq": begin[1],
                "contract_id": begin[2],
                "value_count": len(values),
                "values": values,
            },
        }


class LiveOSCTransport:
    """Engine transport that sends declared native addresses and audits each send."""

    def __init__(
        self,
        endpoints: Mapping[str, tuple[str, int]],
        audit_path: str | Path,
        *,
        action_addresses: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self._endpoints = dict(endpoints)
        self._audit_path = Path(audit_path)
        self._action_addresses = dict(action_addresses or {})
        self._lock = threading.Lock()
        self._records: list[OutputRecord] = []

    @property
    def records(self) -> list[OutputRecord]:
        with self._lock:
            return list(self._records)

    def _send(self, record: OutputRecord, address: str, args: list[Any]) -> None:
        endpoint = self._endpoints.get(record.instrument_id)
        if endpoint is None:
            raise RuntimeError(f"no OSC endpoint for {record.instrument_id}")
        packet = encode_message(address, args)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(packet, endpoint)
        # OutputRecord.bindings comes from the engine's recursively frozen
        # scene definition and is therefore a MappingProxyType.  asdict()
        # deep-copies dataclass fields and cannot copy that type, so serialize
        # this transport boundary explicitly instead.
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

    def send_capability(self, record: OutputRecord) -> None:
        if record.address is None or record.value is None:
            raise RuntimeError("capability output is missing an address or value")
        if isinstance(record.value, float) and not math.isfinite(record.value):
            raise RuntimeError("refusing to send a non-finite OSC value")
        self._send(record, record.address, [record.value])

    def invoke_action(self, record: OutputRecord) -> None:
        if record.action is None:
            raise RuntimeError("action output is missing its action name")
        address = self._action_addresses.get((record.instrument_id, record.action))
        if address is None:
            raise RuntimeError(
                f"no OSC address for action {record.instrument_id}.{record.action}"
            )
        self._send(record, address, [])


def analyze_wav(path: str | Path) -> dict[str, Any]:
    """Measure a rehearsal WAV without making an audibility claim."""

    import numpy as np
    import soundfile as sf

    wav_path = Path(path)
    data, sample_rate = sf.read(wav_path, dtype="float64", always_2d=True)
    finite = np.isfinite(data)
    nan_count = int(np.isnan(data).sum())
    inf_count = int(np.isinf(data).sum())
    if data.size:
        safe = np.where(finite, data, 0.0)
        peak = float(np.max(np.abs(safe)))
        rms = float(np.sqrt(np.mean(np.square(safe))))
        non_silence_ratio = float(np.mean(np.max(np.abs(safe), axis=1) > 1e-4))
    else:
        peak = rms = non_silence_ratio = 0.0
    frames = int(data.shape[0])
    return {
        "path": str(wav_path),
        "bytes": wav_path.stat().st_size,
        "sample_rate_hz": int(sample_rate),
        "channels": int(data.shape[1]),
        "frames": frames,
        "duration_s": frames / float(sample_rate),
        "non_silence_threshold": 1e-4,
        "non_silence_ratio": non_silence_ratio,
        "peak_abs": peak,
        "rms": rms,
        "nan_count": nan_count,
        "inf_count": inf_count,
        "all_finite": bool(finite.all()),
    }


def flatten_diff(before: Any, after: Any, prefix: str = "") -> list[dict[str, Any]]:
    """Return concise leaf-level state changes for report artifacts."""

    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before:
                changes.append({"path": child, "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"path": child, "before": before[key], "after": None})
            else:
                changes.extend(flatten_diff(before[key], after[key], child))
        return changes
    if before != after:
        return [{"path": prefix, "before": before, "after": after}]
    return []
