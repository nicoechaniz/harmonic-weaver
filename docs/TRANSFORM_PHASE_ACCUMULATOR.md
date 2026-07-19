# Transform: `phase_accumulator`

A stateful route transform that integrates an **angular velocity** (degrees per
second) into a **running phase** wrapped to `[0, wrap_deg)`. It is the first
integrating transform in the weaver; every prior transform
(`scale_range`, `curve`, `gate`, `combine`) is memoryless, and `smoothing` holds
state but does not accumulate.

## What was added

| File | Change |
|---|---|
| `src/harmonic_weaver/engine/compiler.py` | `phase_accumulator` accepted in the transform allow-list; compile-time validation + range propagation; runtime integration in `evaluate_route`; two state maps on `RouteRuntime` (`phase_values`, `phase_at_us`). |
| `tests/test_phase_accumulator.py` | 10 tests: compile/range, wrap, `max_dt`/`max_rate` clamps, invalid-freeze-then-resume, independence. |
| `docs/CORE_DESIGN.md` | Transform table row + a note on stateful transforms and deterministic `dt`. |

No public API, contract, or scene-schema field changed. Existing scenes are
unaffected (57 passing tests unchanged; the one collection error in
`tests/test_harmocap_driver.py` is a pre-existing hardcoded `~/Projects/HarMoCAP`
path, not from this change).

## Why it was needed

The weaver's transforms map a *value* to a *value*. But a natural way to drive a
harmonic's **phase** from a continuous signal is to treat that signal as a
*rate* and integrate it — so motion produces continuous, accumulating phase
change rather than a fixed offset that snaps back. This is exactly the model the
existing EEG→phase mapping in `cymatic-control` uses (`velocity = signal · depth`,
then accumulate `% 360`). Without an integrator the weaver can only set absolute
phase, which cannot express "keep turning while I keep moving."

## How it works

```json
{ "type": "phase_accumulator", "wrap_deg": 360.0, "max_rate": 90.0, "max_dt_ms": 100.0 }
```

- **Input**: the running value of the route chain at this position, interpreted as
  `deg/s`. Put a `scale_range` before it to map a normalized feature to a velocity
  (e.g. `[-1, 1] → [-30, 30]`).
- **Output**: `phase_{t} = (phase_{t-1} + velocity · dt) mod wrap_deg`, always in
  `[0, wrap_deg)`. `wrap_deg` defaults to `360`.
- **`dt`**: `(now_us − last_eval_us) / 1e6`, taken from the engine tick clock —
  never wall-clock — so replay and resume are deterministic (same rule
  `smoothing` follows).
- **`max_dt_ms`** (default `100`): clamps `dt` per evaluation. If a route goes
  unusable (invalid input, gated off) and later resumes, the elapsed gap is large;
  the clamp prevents a big phase jump on the first evaluation back.
- **`max_rate`** (optional): clamps `|velocity|` before integrating.

### Validity is handled upstream, so freezing is automatic

`evaluate_route` applies the route's `validity` policy **before** the transform
chain runs. When the input is `invalid` (or `held` and rejected), the route
suppresses/resets/holds and the transform is never evaluated — so the phase is
simply not advanced (it freezes) until the source recovers. The transform itself
only ever runs on usable input; it does not need its own invalid handling.

### State and scope

State lives in `RouteRuntime`, keyed by transform position, so two phase routes —
or two positions in one chain — never share an accumulator. On scene hot-swap the
engine preserves `RouteRuntime` for routes whose id and definition are unchanged,
so a `held` transition keeps the phase continuous instead of snapping to zero.

### Compile-time guarantees

The compiler propagates the output range as `[0, wrap_deg]` and checks it against
the destination argument's range, so a `wrap_deg` that exceeds the instrument's
phase range (e.g. `harmonic_phase` is `[0, 360]`) is rejected before activation,
not at runtime. `wrap_deg` must be positive; `max_rate` positive; `max_dt_ms`
non-negative.

## Relation to Latido

`phase_accumulator` is the one engine primitive the **Latido** performance needs
(`latido/` show repo). Latido drives a laser-cymatics figure: a sealed
tube → balloon membrane → mirror → laser → wall, fed by `harmonic-shaper`'s stereo
out. On that membrane, **per-harmonic phase controls the figure's shape** (it
accumulates → the figure morphs continuously), while amplitude only changes its
size/brightness. So Latido maps a dancer's HarMoCAP **postural features** to
**phase velocities**:

```
harmocap.slot_focus_expansion   → scale_range → phase_accumulator → shaper harmonic_phase N=2
harmocap.slot_focus_verticality → scale_range → phase_accumulator → shaper harmonic_phase N=3
harmocap.slot_focus_symmetry    → scale_range → phase_accumulator → shaper harmonic_phase N=4
```

with harmonic 1 left unrouted as the phase anchor the others move against. The
heartbeat half of the piece (ECG beat → amplitude pulse) reuses the existing
`gate` rising-edge pattern already shown in `rehearsal/scenes/event_demo.scene.json`.

The Latido scene itself lives in the show repo, not here — this PR is only the
reusable engine primitive it depends on.
