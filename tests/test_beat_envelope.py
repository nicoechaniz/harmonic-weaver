"""beat_envelope transform: rising-edge trigger -> decaying gain envelope.

On each rising edge the output snaps to `peak` and relaxes toward `floor` with
a time constant (fixed `tau_ms`, or auto-scaled from the measured inter-beat
interval by `tau_ratio`). Turns the ECG beat trigger into a pulse that breathes
instead of a one-frame flash.
"""

from __future__ import annotations

import math

import pytest

from harmonic_weaver.engine.compiler import (
    RouteRuntime,
    compile_route,
    destination_key,
    evaluate_route,
)
from harmonic_weaver.engine.model import OBSERVED, ValueEnvelope

from engine_fixtures import instrument_manifest

INSTRUMENT = instrument_manifest()
MANIFESTS = {"synth": INSTRUMENT}
CHANNELS = {"sensor.beat": (0.0, 1.0)}
TOL = 1e-6


def _destination() -> dict:
    return {"instrument_id": "synth", "capability": "voice_gain",
            "bindings": {"N": 0}, "argument": "gain"}


def _compile(transform: dict):
    route = {
        "route_id": "beat-to-gain",
        "route_version": 1,
        "enabled": True,
        "inputs": [{"channel": "sensor.beat"}],
        "transforms": [transform],
        "destination": _destination(),
        "validity": {"held": "accept", "min_confidence": 0.0, "invalid": "suppress"},
    }
    return compile_route(route, CHANNELS, MANIFESTS, {destination_key(_destination()): 0.0},
                         "scene.routes[0]")


def _beat(value: float, now_us: int) -> dict:
    return {"sensor.beat": ValueEnvelope(value, OBSERVED, 1.0, now_us, now_us)}


def test_compiles_with_floor_peak_range():
    compiled = _compile({"type": "beat_envelope", "peak": 1.0, "floor": 0.2})
    assert compiled.static_range == (0.2, 1.0)


def test_rejects_floor_ge_peak():
    with pytest.raises(Exception):
        _compile({"type": "beat_envelope", "peak": 0.5, "floor": 0.5})


def test_rising_edge_fires_to_peak():
    compiled = _compile({"type": "beat_envelope", "peak": 1.0, "floor": 0.2, "tau_ms": 100.0})
    rt = RouteRuntime()
    v0, _ = evaluate_route(compiled, rt, _beat(0.0, 0), 0)          # rest at floor
    assert abs(v0 - 0.2) < TOL
    v1, _ = evaluate_route(compiled, rt, _beat(1.0, 10_000), 10_000)  # edge -> peak
    assert abs(v1 - 1.0) < TOL


def test_decays_toward_floor_by_tau():
    compiled = _compile({"type": "beat_envelope", "peak": 1.0, "floor": 0.2, "tau_ms": 100.0})
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _beat(1.0, 0), 0)                  # fire at t=0 -> 1.0
    v, _ = evaluate_route(compiled, rt, _beat(0.0, 100_000), 100_000)  # +1 tau (0.1 s)
    expected = 0.2 + (1.0 - 0.2) * math.exp(-1.0)                   # ~0.494
    assert abs(v - expected) < 1e-4


def test_no_refire_while_held_high():
    compiled = _compile({"type": "beat_envelope", "peak": 1.0, "floor": 0.0, "tau_ms": 50.0})
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _beat(1.0, 0), 0)                  # fire
    a, _ = evaluate_route(compiled, rt, _beat(1.0, 50_000), 50_000)  # still high -> no refire, decays
    b, _ = evaluate_route(compiled, rt, _beat(1.0, 100_000), 100_000)
    assert a < 1.0 and b < a                                        # monotonic decay, never re-peaks


def test_refractory_ignores_too_close_beats():
    compiled = _compile({"type": "beat_envelope", "peak": 1.0, "floor": 0.0,
                         "tau_ms": 100.0, "min_interval_ms": 300.0})
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _beat(1.0, 0), 0)                  # beat 1 -> peak
    evaluate_route(compiled, rt, _beat(0.0, 50_000), 50_000)        # drop low
    v, _ = evaluate_route(compiled, rt, _beat(1.0, 100_000), 100_000)  # beat 2 at +100ms < 300ms
    assert v < 1.0                                                  # refused -> not re-peaked


def test_tau_autoscales_from_interval():
    # No fixed tau_ms: tau = tau_ratio * measured interval. Two beats 1 s apart
    # -> tau = 0.3 * 1000 ms = 300 ms. One tau after the 2nd beat -> ~1/e above floor.
    compiled = _compile({"type": "beat_envelope", "peak": 1.0, "floor": 0.0, "tau_ratio": 0.3})
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _beat(1.0, 0), 0)                  # beat 1
    evaluate_route(compiled, rt, _beat(0.0, 500_000), 500_000)
    evaluate_route(compiled, rt, _beat(1.0, 1_000_000), 1_000_000)  # beat 2, interval 1 s -> tau 300 ms
    v, _ = evaluate_route(compiled, rt, _beat(0.0, 1_300_000), 1_300_000)  # +300 ms = 1 tau
    assert abs(v - math.exp(-1.0)) < 1e-3                           # peak 1, floor 0 -> ~0.368


def test_output_stays_within_floor_peak():
    compiled = _compile({"type": "beat_envelope", "peak": 0.9, "floor": 0.3, "tau_ms": 40.0})
    rt = RouteRuntime()
    vals = []
    seq = [(1.0, 0), (0.0, 20_000), (0.0, 60_000), (1.0, 200_000), (0.0, 260_000)]
    for x, t in seq:
        v, _ = evaluate_route(compiled, rt, _beat(x, t), t)
        vals.append(v)
    assert all(0.3 - TOL <= v <= 0.9 + TOL for v in vals)
