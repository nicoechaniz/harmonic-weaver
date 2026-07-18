"""Deterministic raw ECG OSC source for the hardware-free rehearsal."""

from __future__ import annotations

import argparse
import socket
import time

from harmonic_weaver.contract_codec import encode_message
from harmonic_weaver.drivers.ecg_driver import make_synthetic_ecg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--bpm", type=float, default=72.0)
    parser.add_argument("--duration-s", type=float, default=180.0)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")

    sample_rate = 250
    signal, _peaks = make_synthetic_ecg(
        bpm=args.bpm,
        duration_s=12.0,
        sample_rate=sample_rate,
        noise_level=5.0,
        seed=20260718,
    )
    endpoint = (args.host, args.port)
    deadline = time.monotonic() + args.duration_s
    batches = 0
    samples = 0
    print(
        f"[ecg-simulator] synthetic {args.bpm:.1f} BPM raw ECG -> "
        f"osc://{args.host}:{args.port} for {args.duration_s:.1f}s",
        flush=True,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(encode_message("/ecg/leads_off", [0]), endpoint)
        index = 0
        next_send = time.monotonic()
        while time.monotonic() < deadline:
            end = min(index + args.batch_size, len(signal))
            batch = [float(value) for value in signal[index:end]]
            if len(batch) < args.batch_size:
                batch.extend(float(value) for value in signal[: args.batch_size - len(batch)])
                end = args.batch_size - len(batch)
            sock.sendto(encode_message("/ecg/raw", batch), endpoint)
            batches += 1
            samples += len(batch)
            index = (index + args.batch_size) % len(signal)
            if batches % 250 == 0:
                print(
                    f"[ecg-simulator] batches={batches} samples={samples}",
                    flush=True,
                )
            next_send += len(batch) / sample_rate
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_send = time.monotonic()
    print(f"[ecg-simulator] complete batches={batches} samples={samples}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

