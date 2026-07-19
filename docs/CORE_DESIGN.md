# Harmonic Weaver core design

Status: implementation design for T4.2. The stage contract is
`contracts/stage.contract.draft.json` (`contract_version: 0.1-draft`). Source
Frame v1 and Instrument Control v1 remain authoritative at the OSC edges.

## 1. Scope and invariants

Harmonic Weaver is a headless modulation router. Sources enter through Source
Frame v1, instruments expose native capabilities through Instrument Control
v1, and every UI is a thin WebSocket client. The authoritative state lives in
the server, never in a UI.

The MVP is exactly three sources (HarMoCAP, MIDI, ECG as a rhythmic trigger)
and two instruments (beacon-spatial and harmonic-shaper). The model is general;
EEG/HR, audio analysis, phone sensors, surge-bridge, mobile and Quest clients
are F5+, not T4.2 scope.

Four invariants govern the engine:

1. Mappings are declarative, versioned data. No route contains executable code.
2. A value crosses an edge only after the peer's runtime `contract_id` matches
   an installed manifest. Only declared, writable instrument capabilities bind.
3. A mutation is validated and compiled before one atomic state-generation
   swap. Failure leaves the previous generation running.
4. Panic has priority over every mutation and output; it is global and latched.

Patching is a performance action: edits do not restart the engine, single edits
activate on the next engine tick, and `route.batch` provides an atomic multi-edit
audition. Optimistic revisions prevent two clients from silently overwriting one
another. The Baka-derived criterion is that the operator weaves relationships
among focus, crowd and instruments; the system does not turn them into a
hierarchical conductor or a single master-control gesture.

## 2. Registries and addresses

An accepted manifest is installed before its producer connects. Runtime hello
messages prove identity; they do not install or replace contracts.

For a source, the gate is `(source_id, stream_id, contract_id)`. A mismatch
rejects frames. A new `stream_id` clears counters and cached values. Lease expiry
marks the source absent and every channel invalid. Each declared channel is
addressable as `source_id.channel`, with the first dot as the separator. For
example, an adapter can expose one HarMoCAP coordinate as
`harmocap.slot_3_keypoint_left_wrist_x` and one feature as
`harmocap.slot_3_kinetic_energy`; both channel names obey Source Frame v1.

For an instrument, the gate is `(instrument_id, stream_id, contract_id)` plus
completed state synchronization. A mismatch, restart, or incomplete dump makes
the instrument non-writable. A route destination names `instrument_id`, a
manifest capability, placeholder bindings such as `N=4`, and one argument.
Bounds, type, range, lag, read/write status and native OSC address come only
from that manifest.

Registry state is one of `installed`, `connecting`, `gated`, `ready`,
`incompatible` or `absent`. Derived sources use the same registry and value
envelope, but are marked `kind: derived` and have no external lease.

## 3. Route and preset model

A scene is the persisted, versioned preset. It embeds its routes and derived
source declarations so the complete mapping can be reviewed, diffed, hashed
and reproduced. Deployment host/port data is outside scenes.

```json
{
  "route_id": "left-wrist-to-h4-gain",
  "route_version": 3,
  "enabled": true,
  "inputs": [{"channel": "harmocap.slot_0_keypoint_left_wrist_y"}],
  "transforms": [
    {"type": "scale_range", "in": [0.0, 1.0], "out": [0.0, 0.8], "clamp": true},
    {"type": "curve", "kind": "power", "gamma": 1.6},
    {"type": "smoothing", "kind": "one_pole", "time_ms": 35.0}
  ],
  "destination": {
    "instrument_id": "harmonic-shaper", "capability": "voice_gain",
    "bindings": {"N": 4}, "argument": "gain"
  },
  "validity": {"held": "accept", "min_confidence": 0.35,
               "invalid": "hold_then_reset", "hold_ms": 120}
}
```

`inputs` contains one or more exact registry channel references. Multiple inputs
require a `combine` transform first. A reusable N-to-one computation belongs in
a derived source instead. A scene may have only one active writer for a fully
bound destination argument; preflight rejects write races.

Transform execution is ordered and uses finite numbers only:

| Type | Required semantics |
|---|---|
| `scale_range` | Affine input/output ranges; clamp behavior is explicit. |
| `curve` | `linear`, `power`, `exponential`, `smoothstep` or monotonic piecewise points. |
| `smoothing` | One-pole or bounded ramp with declared `time_ms`; it is control-rate smoothing, not audio interpolation. |
| `gate` | Threshold, hysteresis, `level`/`rising_edge`/`falling_edge`, and closed behavior (`suppress` or a finite value). |
| `combine` | `mean`, `sum`, `min`, `max`, `weighted_sum` or `difference`, with arity and weights validated. |
| `phase_accumulator` | Integrate an angular velocity (deg/s) into a running phase wrapped to `[0, wrap_deg)` (default `360`). Optional `max_rate` clamps `|velocity|`; `max_dt_ms` (default `100`) clamps the per-evaluation step so a gap on resume cannot jump. Stateful. |

`scale_range`, `curve`, `combine` and `gate` are memoryless; `smoothing` and
`phase_accumulator` are **stateful** — they hold per-route, per-position state in
`RouteRuntime` and derive their time step from the engine's `now_us` deltas (the
same monotonic clock used everywhere, so replay/resume stays deterministic — never
wall-clock). See `docs/TRANSFORM_PHASE_ACCUMULATOR.md`.

Every route declares a validity policy. `invalid` never enters the numerical
chain. `held` may be accepted or rejected, but its state and decaying confidence
are preserved; no transform may promote it to `observed`. On loss, a route must
explicitly suppress/release, reset, or hold for a bounded interval then reset.
Rising-edge ECG routes discard invalid/held triggers and never queue events.

Route create/update/delete operates on a named scene. Updates replace the full
route (no ambiguous merge patch), require the current route/stage revision, and
append an audit event. Validation covers manifests, types, ranges, graph cycles,
destination collisions and safety defaults before activation.

## 4. Aggregated and derived sources

An aggregator declaration has `derived_source_id`, one output channel, exact
inputs, an operator, cadence, membership/validity rules and confidence reducer.
Inputs may include per-input predicates, which makes the eight HarMoCAP slots
explicit and auditable. For example, each slot's kinetic-energy input can be
included only while that slot's sibling `focused` channel is `0`; the output
`crowd.mean_kinetic_energy` can then drive the harmonic bed.

The compiler rejects derived-source cycles and materializes each aggregator as
a first-class source registry entry. Its synthetic contract ID is the canonical
hash of its declaration. Routes consume its `source_id.channel` exactly like an
external source, and derived sources may depend on earlier acyclic derived
sources.

Cadence is mandatory:

- `fixed_hz` samples the coherent latest input set at the declared rate. The MVP
  crowd aggregators use 30 Hz (one declared 33.3 ms control period).
- `on_input` recomputes after a usable input change, coalesced to at most the
  declared `max_rate_hz` and once per engine tick.

At each update, invalid/expired inputs are excluded. The declaration specifies
`min_valid_count`, `min_observed_count`, `max_age_ms`, held inclusion, and a
confidence reducer (`minimum`, `mean` or `product`). The output is `invalid` if
too few usable inputs remain; `held` if any included value is held or a cached
aggregate is emitted within its bounded hold window; and `observed` only when
the observed minimum is met and every included value is observed. Held output
confidence must decay monotonically. Empty sets are always invalid.

## 5. Scenes and hot switching

A scene contains `scene_id`, integer `scene_version`, name, description, tags,
creation/update metadata, derived sources, routes and a transition policy.
Switch preflight resolves all installed contract IDs and capabilities, then the
engine flips the active scene generation at a tick boundary. Disconnected but
known instruments leave routes inactive; unknown/incompatible contracts reject
the switch. A later matching connection activates those routes safely.

Routes with the same ID and identical canonical definition retain transform
state. Changed/new routes start clean. Removed routes stop evaluating. For each
affected destination, the selected policy is:

- `crossfade` (default, 100 ms): freeze the old emitted value, start the new
  chain clean, and interpolate to its live value. If no valid new value arrives
  within `await_valid_ms` (default 250 ms), reset to the declared safe default.
- `held`: freeze the old value for at most `hold_ms` (default 250 ms), hand over
  when the new chain becomes usable, otherwise reset. The old route is not run.
- `reset`: at the swap, apply the declared destination default/release action,
  clear route state, and wait for a usable new value.

A reset/default must exist in the instrument's accepted safety profile. Scene
switch and `route.batch` are all-or-nothing. Successful acknowledgements include
the new `stage_revision` and activation generation so clients can correlate what
they heard with what they edited.

## 6. Global panic

Each installed instrument has a declarative `safety_profile` bound to its exact
Instrument Control contract. It classifies the instrument and names only native,
declared actions/defaults. An instrument without a valid profile cannot be armed
for performance.

Panic is idempotent and bypasses the normal command queue. The server first
latches `panic.active=true`, increments `panic_generation`, closes the route
output gate, and discards pending triggers. It then acts on all instruments in
parallel; failure of one never delays another:

| Class | Panic sequence |
|---|---|
| `sustained_processor` (beacon-spatial) | Invoke native panic if declared; ramp master/band gains or enables to their silent values within 20 ms; reset spatial/tone controls to defaults while silence controls remain pinned. |
| `polyphonic_instrument` (harmonic-shaper) | Block note-ons; invoke release-all; ramp master to silence within 20 ms; after the profile's bounded release grace (maximum 500 ms), force all-voices-off; reset voice/global controls while master remains pinned silent. |
| `control_only` (future non-audio target) | Suppress events and reset every safety-profile control to its default; no audio action is implied. |

The latched state retains scenes and edits, keeps source/registry telemetry live,
suppresses all instrument writes, records per-instrument outcomes, and retries
the safe sequence before accepting a reconnected instrument as ready. Nothing
automatically clears panic and no queued event is replayed.

Recovery requires `panic.clear` with the current `panic_generation` and an
explicit scene. The server revalidates every contract and state sync, resets all
route state, restores profile defaults, and fades the chosen scene up from safe
values over at least 250 ms. Any failed preflight leaves panic latched.

## 7. Headless state store and WebSocket API

T4.2 hosts `/ws` with FastAPI, but transport, routing and state-store layers stay
separate and no static UI is required. `digital_beacon/api.py` and its state
store are the structural reference (connection manager, snapshot and change
broadcast), not a codebase to inherit.

The server exposes `/ws` as versioned JSON messages. It sends `server.hello`
with its process `server_stream_id`, stage `contract_id`, event sequence and
revision. The client must reply `client.hello` with that exact contract ID before
commands or subscriptions are accepted. A mismatch is handshake-only and
incompatible.

`state.subscribe` selects `stage`, `routes`, `scenes`, `sources`, `instruments`
and/or `metrics`. The server atomically emits `state.snapshot` at revision R,
then events strictly after R; reconnect normally takes a fresh snapshot.
Server events carry `event_seq`, `stage_revision` and `sent_at_us`.

The command surface is `route.create`, `route.update`, `route.delete`,
`route.batch`, `scene.upsert`, `scene.delete`, `scene.switch`, `panic.trigger`
and `panic.clear`. Responses are `command.ack` or `command.error`. Registry
transitions are `registry.source` and `registry.instrument`; they expose the
runtime and expected contract IDs, stream ID, gate state and reason. Route,
scene, panic and state changes are broadcast as `state.event`/`panic.event`.
The draft contract defines exact envelopes, required payload fields, error codes
and compare-and-swap behavior.

The state store serializes mutations, owns immutable snapshots, and publishes
events only after commit. WebSocket serialization and slow clients are isolated
from the routing hot path; a bounded client queue is disconnected on overflow
rather than delaying instrument output.

## 8. Latency and behavioral evidence

HarMoCAP measured about 7 ms software p50 before Weaver. T4.2's provisional
local receipt-to-instrument-send budget is p50 <= 2 ms, p95 <= 5 ms and p99 <=
10 ms under the full MVP route set. Direct routes are event-driven. A 30 Hz
derived source additionally has 0-33.3 ms intentional scheduling wait. Local WS
mutation receipt-to-generation-swap should be p95 <= 20 ms, excluding declared
crossfades. These are acceptance targets, not measured claims.

Every rehearsal writes `reports/<run_id>/` with the run configuration, accepted
contract IDs, canonical scene snapshot, source/route/instrument timestamps,
`latency_summary.json`, behavior events and a summary of drops/errors. Behavior
tests prove contract gating, monotonic discard, invalid/held propagation, atomic
batch and scene switches, destination collision rejection, panic ordering and
latched recovery. Reports distinguish source capture latency, Weaver overhead,
network/audio latency and deliberate cadence/crossfade time.
