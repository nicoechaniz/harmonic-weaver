# End-to-end rehearsal

Run the full hardware-free integration gate from the `harmonic-weaver` root:

```console
uv sync --extra rehearsal --extra test
./rehearsal/run_rehearsal.sh
```

The command requires the sibling checkouts at `~/Projects/beacon-spatial`,
`~/Projects/harmonic-shaper`, `~/Projects/HarMoCAP`, and
`~/Projects/cymatic-control`, plus `pw-jack`, `scsynth`, `sclang`, SuperCollider
ATK kernels, and the existing `beacon-spatial/venv`. It uses the Weaver
`.venv/bin/python` for Weaver, Shaper, the HarMoCAP kit replay, and the
deterministic ECG sender. No R24, MIDI device, camera, person, or other live
sensor is used.

Shaper is intentionally launched with `--no-midi --no-audio`. Its OSC state
is exercised and its HTTP API supplies state evidence; Beacon/SuperCollider is
the rehearsal audio plane. This is declared in every run report.

The default sequence spends 45 seconds in `event-demo`, 10 seconds in
`sparse`, and another 45 seconds in `event-demo`, for 90 seconds of cumulative
demo-scene runtime. It then performs panic, a three-second route-gating check,
clear/recovery, and shutdown. Override only for harness development with
`REHEARSAL_DEMO_SEGMENT_S` and `REHEARSAL_SPARSE_S`; a run shorter than 90 demo
seconds cannot pass the live gate.

Weaver behavior reports are written to `reports/<run_id>/`. Process logs,
state dumps, diffs, assertion results, and the ignored rehearsal WAV are under
`rehearsal/artifacts/<run_id>/`. The most recent full result is rendered into
`rehearsal/REPORT.md`. A failed assertion exits non-zero and leaves all partial
evidence in place.

