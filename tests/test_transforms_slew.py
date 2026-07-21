"""slew_limiter transform: rate-limited chase of a continuous target.

Exercises compiler validation (max_rate > 0, max_dt_ms >= 0), static range
preservation, and the stateful runtime in evaluate_route. Destination is
voice_phase so values in [0, 10] fit the instrument argument range [0, 360].
"""

from __future__ import annotations

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
CHANNELS = {"sensor.target": (0.0, 10.0)}
TOL = 1e-6


def _destination() -> dict:
    return {
        "instrument_id": "synth",
        "capability": "voice_phase",
        "bindings": {"N": 0},
        "argument": "phase_degrees",
    }


def _safety() -> dict:
    return {destination_key(_destination()): 0.0}


def _compile(transforms: list[dict], validity: dict | None = None):
    route = {
        "route_id": "target-slew",
        "route_version": 1,
        "enabled": True,
        "inputs": [{"channel": "sensor.target"}],
        "transforms": transforms,
        "destination": _destination(),
        "validity": validity
        or {"held": "accept", "min_confidence": 0.0, "invalid": "suppress"},
    }
    return compile_route(route, CHANNELS, MANIFESTS, _safety(), "scene.routes[0]")


def _values(target: float, now_us: int, state: str = OBSERVED) -> dict:
    return {
        "sensor.target": ValueEnvelope(target, state, 1.0, now_us, now_us),
    }


def _slew(**params) -> list[dict]:
    params.setdefault("max_rate", 2.0)
    params.setdefault("max_dt_ms", 1000.0)
    return [{"type": "slew_limiter", **params}]


def test_preserves_input_static_range():
    compiled = _compile(_slew())
    assert compiled.static_range == (0.0, 10.0)


def test_rejects_max_rate_zero():
    with pytest.raises(Exception):
        _compile(_slew(max_rate=0.0))


def test_rejects_negative_max_rate():
    with pytest.raises(Exception):
        _compile(_slew(max_rate=-1.0))


def test_rejects_negative_max_dt_ms():
    with pytest.raises(Exception):
        _compile(_slew(max_dt_ms=-1.0))


def test_cold_start_snaps_to_target():
    compiled = _compile(_slew(max_rate=2.0, max_dt_ms=1000.0))
    rt = RouteRuntime()
    value, reason = evaluate_route(compiled, rt, _values(7.5, 0), 0)
    assert reason == "usable"
    assert abs(value - 7.5) < TOL


def test_ramp_bounded_by_max_rate():
    # Start at 0 (cold snap), then hold target=10 for 1 s with max_rate=2.
    # After 1 s the slew may only have moved by 2.0, not jumped to 10.
    compiled = _compile(_slew(max_rate=2.0, max_dt_ms=10_000.0))
    rt = RouteRuntime()
    v0, _ = evaluate_route(compiled, rt, _values(0.0, 0), 0)
    assert abs(v0 - 0.0) < TOL
    v1, reason = evaluate_route(compiled, rt, _values(10.0, 1_000_000), 1_000_000)
    assert reason == "usable"
    assert abs(v1 - 2.0) < TOL
    assert abs(v1 - 10.0) > 1.0


def test_gap_clamped_by_max_dt_ms():
    # max_rate=2, max_dt_ms=100 → at most 0.2 of movement across a 500 ms gap.
    compiled = _compile(_slew(max_rate=2.0, max_dt_ms=100.0))
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _values(0.0, 0), 0)
    value, _ = evaluate_route(compiled, rt, _values(10.0, 500_000), 500_000)
    max_step = 2.0 * 0.1
    assert abs(value - max_step) < TOL
    assert value < 1.0  # must not jump toward 10 over the full 0.5 s


def test_output_stays_within_range_when_target_oscillates():
    compiled = _compile(_slew(max_rate=50.0, max_dt_ms=10_000.0))
    rt = RouteRuntime()
    targets = [0.0, 10.0, 0.0, 10.0, 5.0, 0.0, 10.0]
    step_us = 100_000  # 0.1 s; max_rate*dt = 5.0 so each step is rate-limited
    now = 0
    for target in targets:
        value, reason = evaluate_route(compiled, rt, _values(target, now), now)
        assert reason == "usable"
        assert 0.0 - TOL <= value <= 10.0 + TOL
        now += step_us
