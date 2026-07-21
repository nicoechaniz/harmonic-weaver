# Bitácora — harmonic-weaver

- 2026-07-21: add `slew_limiter` transform (rate-limited chase of a continuous target; `max_rate` + `max_dt_ms` gap clamp; cold-start snaps to target). Core convergence primitive for cuerpo-como-instrumento. 8 tests in `tests/test_transforms_slew.py`; CORE_DESIGN updated. Branch `main`.
- 2026-07-18: repo scaffolded.
- 2026-07-19: add `phase_accumulator` transform (velocity→wrapped phase integrator) for the Latido laser-cymatics piece; compiler validation + range propagation + stateful runtime, 10 tests, docs. Branch `feat/phase-accumulator-transform`.
- 2026-07-19: Event-demo and sparse scenes now route HarMoCAP gains through Shaper `harmonic_envelope` rather than the passive `harmonic_gain` parameter. The safety profile resets those envelopes to zero. The rehearsal assertion now follows the declared capability rather than a historical capability name.
- 2026-07-19: The full hardware-free rehearsal passed as `t45-20260719T092417Z`: focused HarMoCAP partials `1..5`, scene hot-swap, panic release/rearm, and non-silent finite audio all passed. The live-stack launcher accepts `--harmocap-checkpoint <path>` for an explicit local model override without altering HarMoCAP's promoted model configuration.
- 2026-07-19: Live camera → HarMoCAP → Weaver → Shaper/R24 control was audibly confirmed by the user using explicit `yolo26m-pose.pt`. The camera process later hit an Ultralytics CUDA/ReID illegal-memory-access failure; it is recorded as a HarMoCAP GPU stability issue, not as a successful soak run.
