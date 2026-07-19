"""phase_accumulator transform: velocity (deg/s) -> wrapped phase (deg).

Exercises the compiler validation / range propagation and the stateful runtime
integration in evaluate_route, using the template's voice_phase capability
(argument phase_degrees, range [0, 360]).
"""

from __future__ import annotations

import pytest

from harmonic_weaver.engine.compiler import (
    RouteRuntime,
    compile_route,
    destination_key,
    evaluate_route,
)
from harmonic_weaver.engine.model import INVALID, OBSERVED, ValueEnvelope

from engine_fixtures import instrument_manifest

INSTRUMENT = instrument_manifest()
MANIFESTS = {"synth": INSTRUMENT}
CHANNELS = {"sensor.vel": (-1.0, 1.0)}
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
        "route_id": "vel-to-phase",
        "route_version": 1,
        "enabled": True,
        "inputs": [{"channel": "sensor.vel"}],
        "transforms": transforms,
        "destination": _destination(),
        "validity": validity or {"held": "accept", "min_confidence": 0.0, "invalid": "suppress"},
    }
    return compile_route(route, CHANNELS, MANIFESTS, _safety(), "scene.routes[0]")


def _values(velocity_channel_value: float, now_us: int, state: str = OBSERVED) -> dict:
    return {"sensor.vel": ValueEnvelope(velocity_channel_value, state, 1.0, now_us, now_us)}


def _chain(out=(-30.0, 30.0), **accumulator):
    accumulator.setdefault("wrap_deg", 360.0)
    accumulator.setdefault("max_dt_ms", 1000.0)
    return [
        {"type": "scale_range", "in": [-1.0, 1.0], "out": list(out), "clamp": True},
        {"type": "phase_accumulator", **accumulator},
    ]


def test_compiles_with_bounded_output_range():
    compiled = _compile(_chain())
    assert compiled.static_range == (0.0, 360.0)


def test_output_range_must_fit_destination():
    # wrap 400 exceeds the phase_degrees [0, 360] destination.
    with pytest.raises(Exception):
        _compile(_chain(wrap_deg=400.0))


def test_rejects_non_positive_wrap():
    with pytest.raises(Exception):
        _compile(_chain(wrap_deg=0.0))


def test_constant_velocity_integrates():
    compiled = _compile(_chain())          # channel 1.0 -> 30 deg/s
    rt = RouteRuntime()
    v0, r0 = evaluate_route(compiled, rt, _values(1.0, 0), 0)
    assert r0 == "usable" and abs(v0 - 0.0) < TOL          # epoch, no dt yet
    v1, _ = evaluate_route(compiled, rt, _values(1.0, 100_000), 100_000)   # +0.1 s
    assert abs(v1 - 3.0) < TOL
    v2, _ = evaluate_route(compiled, rt, _values(1.0, 200_000), 200_000)
    assert abs(v2 - 6.0) < TOL


def test_zero_velocity_holds_phase():
    compiled = _compile(_chain())
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _values(1.0, 0), 0)
    v1, _ = evaluate_route(compiled, rt, _values(1.0, 100_000), 100_000)
    v2, _ = evaluate_route(compiled, rt, _values(0.0, 200_000), 200_000)   # stop
    assert abs(v1 - v2) < TOL


def test_wraps_continuously_past_modulus():
    compiled = _compile(_chain(out=(-100.0, 100.0), max_dt_ms=10_000.0))   # 100 deg/s
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _values(1.0, 0), 0)
    value, _ = evaluate_route(compiled, rt, _values(1.0, 4_000_000), 4_000_000)  # 400 deg
    assert abs(value - 40.0) < TOL
    assert 0.0 <= value < 360.0


def test_max_dt_clamps_resume_jump():
    compiled = _compile(_chain(max_dt_ms=100.0))          # 30 deg/s, clamp 0.1 s
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _values(1.0, 0), 0)
    value, _ = evaluate_route(compiled, rt, _values(1.0, 10_000_000), 10_000_000)  # 10 s gap
    assert abs(value - 3.0) < TOL                          # 30 * 0.1, not 30 * 10


def test_max_rate_clamps_velocity():
    compiled = _compile(_chain(out=(-1000.0, 1000.0), max_rate=20.0))     # would be 1000
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _values(1.0, 0), 0)
    value, _ = evaluate_route(compiled, rt, _values(1.0, 100_000), 100_000)   # 0.1 s
    assert abs(value - 2.0) < TOL                          # clamped 20 * 0.1


def test_invalid_input_freezes_then_resumes_clamped():
    compiled = _compile(
        _chain(max_dt_ms=100.0),
        validity={"held": "accept", "min_confidence": 0.0, "invalid": "suppress"},
    )
    rt = RouteRuntime()
    evaluate_route(compiled, rt, _values(1.0, 0), 0)
    v1, _ = evaluate_route(compiled, rt, _values(1.0, 100_000), 100_000)      # 3.0
    assert abs(v1 - 3.0) < TOL
    # invalid -> route suppresses upstream of transforms; phase must not advance
    frozen, reason = evaluate_route(compiled, rt, _values(0.0, 200_000, INVALID), 200_000)
    assert reason == "suppress" and frozen is None
    # resume after a long gap: dt from the last usable eval, clamped to 0.1 s
    resumed, _ = evaluate_route(compiled, rt, _values(1.0, 10_000_000), 10_000_000)
    assert abs(resumed - 6.0) < TOL                        # 3.0 + 30 * 0.1


def test_independent_route_runtimes():
    fast = _compile(_chain(out=(-90.0, 90.0)))            # 90 deg/s
    slow = _compile(_chain(out=(-30.0, 30.0)))            # 30 deg/s
    rt_fast, rt_slow = RouteRuntime(), RouteRuntime()
    evaluate_route(fast, rt_fast, _values(1.0, 0), 0)
    evaluate_route(slow, rt_slow, _values(1.0, 0), 0)
    vf, _ = evaluate_route(fast, rt_fast, _values(1.0, 100_000), 100_000)
    vs, _ = evaluate_route(slow, rt_slow, _values(1.0, 100_000), 100_000)
    assert abs(vf - 9.0) < TOL
    assert abs(vs - 3.0) < TOL
