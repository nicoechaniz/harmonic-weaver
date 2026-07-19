"""Declarative scene compiler and stateful route/aggregator evaluators."""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from harmonic_weaver.contract_codec import canonical_json_dumps, canonical_json_hash

from .errors import WeaverError, validation
from .model import HELD, INVALID, OBSERVED, ValueEnvelope


_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_STATES = {OBSERVED, HELD, INVALID}


def validate_json_finite(value: Any, path: str = "value") -> None:
    """Reject non-finite numbers anywhere in declarative client data."""

    if isinstance(value, float) and not math.isfinite(value):
        raise validation(f"{path} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            validate_json_finite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_json_finite(item, f"{path}[{index}]")


def finite(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise validation(f"{path} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise validation(f"{path} must be a finite number")
    return number


def nonnegative(value: Any, path: str) -> float:
    number = finite(value, path)
    if number < 0:
        raise validation(f"{path} must be non-negative")
    return number


def positive(value: Any, path: str) -> float:
    number = finite(value, path)
    if number <= 0:
        raise validation(f"{path} must be positive")
    return number


def integer(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise validation(f"{path} must be an integer")
    if minimum is not None and value < minimum:
        raise validation(f"{path} must be at least {minimum}")
    return value


def identifier(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise validation(f"{path} must match [a-z][a-z0-9_-]*")
    return value


def required(mapping: Mapping[str, Any], names: Sequence[str], path: str) -> None:
    missing = [name for name in names if name not in mapping]
    if missing:
        raise validation(f"{path} missing required field(s): {', '.join(missing)}")


def range_pair(value: Any, path: str, *, allow_descending: bool = True) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise validation(f"{path} must contain two finite endpoints")
    first, second = finite(value[0], f"{path}[0]"), finite(value[1], f"{path}[1]")
    if first == second:
        raise validation(f"{path} endpoints must differ")
    if not allow_descending and first > second:
        raise validation(f"{path} must be increasing")
    return first, second


def split_channel(address: Any, path: str) -> tuple[str, str]:
    if not isinstance(address, str) or "." not in address:
        raise validation(f"{path} must be source_id.channel")
    source_id, channel = address.split(".", 1)
    identifier(source_id, f"{path}.source_id")
    identifier(channel, f"{path}.channel")
    return source_id, channel


def destination_key(destination: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        destination["instrument_id"],
        destination["capability"],
        tuple(sorted(destination["bindings"].items())),
        destination["argument"],
    )


def _combine_range(
    operator: str, ranges: list[tuple[float, float]], weights: list[float] | None
) -> tuple[float, float]:
    if operator == "mean":
        return sum(item[0] for item in ranges) / len(ranges), sum(item[1] for item in ranges) / len(ranges)
    if operator == "sum":
        return sum(item[0] for item in ranges), sum(item[1] for item in ranges)
    if operator in {"min", "max"}:
        return min(item[0] for item in ranges), max(item[1] for item in ranges)
    if operator == "difference":
        return ranges[0][0] - ranges[1][1], ranges[0][1] - ranges[1][0]
    assert weights is not None
    lows: list[float] = []
    highs: list[float] = []
    for (low, high), weight in zip(ranges, weights):
        endpoints = (low * weight, high * weight)
        lows.append(min(endpoints))
        highs.append(max(endpoints))
    return sum(lows), sum(highs)


def _apply_combine(operator: str, values: list[float], weights: list[float] | None) -> float:
    if operator == "mean":
        return sum(values) / len(values)
    if operator == "sum":
        return sum(values)
    if operator == "min":
        return min(values)
    if operator == "max":
        return max(values)
    if operator == "difference":
        return values[0] - values[1]
    assert weights is not None
    return sum(value * weight for value, weight in zip(values, weights))


def _validate_combine(transform: Mapping[str, Any], arity: int, path: str) -> tuple[str, list[float] | None]:
    operator = transform.get("operator")
    if operator not in {"mean", "sum", "min", "max", "weighted_sum", "difference"}:
        raise validation(f"{path}.operator is invalid")
    if operator == "difference" and arity != 2:
        raise validation(f"{path} difference requires exactly two inputs")
    weights: list[float] | None = None
    if operator == "weighted_sum":
        raw = transform.get("weights")
        if not isinstance(raw, list) or len(raw) != arity:
            raise validation(f"{path}.weights must match input arity")
        weights = [finite(item, f"{path}.weights[{index}]") for index, item in enumerate(raw)]
    elif "weights" in transform:
        raise validation(f"{path}.weights is only valid for weighted_sum")
    return str(operator), weights


@dataclass(frozen=True)
class DestinationSpec:
    definition: dict[str, Any]
    key: tuple[Any, ...]
    argument_type: str
    value_range: tuple[float, float]
    address: str


@dataclass(frozen=True)
class CompiledRoute:
    definition: dict[str, Any]
    inputs: tuple[str, ...]
    destination: DestinationSpec
    static_range: tuple[float, float]
    has_edge_gate: bool

    @property
    def route_id(self) -> str:
        return self.definition["route_id"]

    @property
    def canonical(self) -> str:
        return canonical_json_dumps(self.definition)


@dataclass
class RouteRuntime:
    smooth_values: dict[int, float] = field(default_factory=dict)
    smooth_at_us: dict[int, int] = field(default_factory=dict)
    gate_states: dict[int, bool] = field(default_factory=dict)
    phase_values: dict[int, float] = field(default_factory=dict)
    phase_at_us: dict[int, int] = field(default_factory=dict)
    last_usable_output: float | None = None
    last_usable_at_us: int | None = None
    invalid_reset_sent: bool = False


@dataclass(frozen=True)
class CompiledAggregator:
    definition: dict[str, Any]
    inputs: tuple[dict[str, Any], ...]
    output_address: str
    output_range: tuple[float, float]
    contract_id: str
    operator: str
    weights: tuple[float, ...] | None


@dataclass
class AggregatorRuntime:
    next_due_us: int = 0
    last_compute_us: int | None = None
    cached_value: float | None = None
    cached_confidence: float = 0.0
    cached_at_us: int | None = None
    last_output_confidence: float = 1.0
    pending_input: bool = False


@dataclass(frozen=True)
class CompiledScene:
    definition: dict[str, Any]
    routes: tuple[CompiledRoute, ...]
    aggregators: tuple[CompiledAggregator, ...]
    channel_ranges: Mapping[str, tuple[float, float]]


def _capability(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    for capability in manifest.get("capabilities", ()):
        if capability.get("name") == name:
            return capability
    return None


def compile_destination(
    raw: Any,
    instrument_manifests: Mapping[str, Mapping[str, Any]],
    safety_defaults: Mapping[tuple[Any, ...], float | int],
    path: str,
) -> DestinationSpec:
    if not isinstance(raw, Mapping):
        raise validation(f"{path} must be an object")
    required(raw, ("instrument_id", "capability", "bindings", "argument"), path)
    instrument_id = identifier(raw["instrument_id"], f"{path}.instrument_id")
    manifest = instrument_manifests.get(instrument_id)
    if manifest is None:
        raise WeaverError("capability_missing", f"unknown instrument {instrument_id!r}")
    capability_name = identifier(raw["capability"], f"{path}.capability")
    capability = _capability(manifest, capability_name)
    if capability is None or capability.get("write") is not True:
        raise WeaverError("capability_missing", f"{instrument_id}.{capability_name} is not writable")
    bindings = raw["bindings"]
    if not isinstance(bindings, Mapping):
        raise validation(f"{path}.bindings must be an object")
    parameters = capability.get("parameters", {})
    if set(bindings) != set(parameters):
        raise validation(f"{path}.bindings must bind every capability placeholder")
    clean_bindings: dict[str, int] = {}
    for name, spec in parameters.items():
        value = integer(bindings[name], f"{path}.bindings.{name}")
        low, high = spec["bounds"]
        if not low <= value <= high:
            raise validation(f"{path}.bindings.{name} is outside [{low}, {high}]")
        clean_bindings[name] = value
    argument_name = identifier(raw["argument"], f"{path}.argument")
    argument = next((item for item in capability.get("arguments", ()) if item.get("name") == argument_name), None)
    if argument is None or "range" not in argument:
        raise WeaverError("capability_missing", f"unknown numeric argument {argument_name!r}")
    low, high = range_pair(argument["range"], f"{path}.argument.range", allow_descending=False)
    definition = {
        "instrument_id": instrument_id,
        "capability": capability_name,
        "bindings": clean_bindings,
        "argument": argument_name,
    }
    key = destination_key(definition)
    if key not in safety_defaults:
        raise WeaverError("unsafe_instrument", f"destination {key!r} has no safety-profile default")
    address = capability["address_pattern"]
    for name, value in clean_bindings.items():
        address = address.replace("{" + name + "}", str(value))
    return DestinationSpec(definition, key, argument["type"], (low, high), address)


def _validate_curve(transform: Mapping[str, Any], path: str) -> tuple[str, Any]:
    kind = transform.get("kind")
    if kind not in {"linear", "power", "exponential", "smoothstep", "piecewise"}:
        raise validation(f"{path}.kind is invalid")
    if kind == "power":
        return str(kind), positive(transform.get("gamma"), f"{path}.gamma")
    if kind == "exponential":
        amount = finite(transform.get("amount", transform.get("k", 1.0)), f"{path}.amount")
        return str(kind), amount
    if kind == "piecewise":
        raw_points = transform.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            raise validation(f"{path}.points requires at least two points")
        points: list[tuple[float, float]] = []
        for index, raw in enumerate(raw_points):
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise validation(f"{path}.points[{index}] must be [x, y]")
            points.append((finite(raw[0], f"{path}.points[{index}][0]"), finite(raw[1], f"{path}.points[{index}][1]")))
        if any(points[index][0] >= points[index + 1][0] for index in range(len(points) - 1)):
            raise validation(f"{path}.points x values must be strictly increasing")
        deltas = [points[index + 1][1] - points[index][1] for index in range(len(points) - 1)]
        if not (all(delta >= 0 for delta in deltas) or all(delta <= 0 for delta in deltas)):
            raise validation(f"{path}.points y values must be monotonic")
        return str(kind), points
    return str(kind), None


def _curve(value: float, kind: str, parameter: Any) -> float:
    if kind == "linear":
        return value
    if kind == "power":
        return math.copysign(abs(value) ** parameter, value)
    if kind == "exponential":
        if parameter == 0:
            return value
        return math.expm1(parameter * value) / math.expm1(parameter)
    if kind == "smoothstep":
        x = min(1.0, max(0.0, value))
        return x * x * (3.0 - 2.0 * x)
    points: list[tuple[float, float]] = parameter
    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= value <= x1:
            ratio = (value - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    raise AssertionError("piecewise interval not found")


def validate_validity(raw: Any, path: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise validation(f"{path} must be an object")
    required(raw, ("held", "min_confidence", "invalid"), path)
    if raw["held"] not in {"accept", "reject"}:
        raise validation(f"{path}.held must be accept or reject")
    confidence = finite(raw["min_confidence"], f"{path}.min_confidence")
    if not 0 <= confidence <= 1:
        raise validation(f"{path}.min_confidence must be in [0,1]")
    invalid_policy = raw["invalid"]
    if invalid_policy not in {"suppress", "release", "reset", "hold_then_reset"}:
        raise validation(f"{path}.invalid is invalid")
    clean = dict(raw)
    if invalid_policy == "hold_then_reset":
        if "hold_ms" not in raw:
            raise validation(f"{path}.hold_ms is required")
        clean["hold_ms"] = nonnegative(raw["hold_ms"], f"{path}.hold_ms")
    elif "hold_ms" in raw:
        raise validation(f"{path}.hold_ms is only valid for hold_then_reset")
    return clean


def compile_route(
    raw: Any,
    channel_ranges: Mapping[str, tuple[float, float]],
    instrument_manifests: Mapping[str, Mapping[str, Any]],
    safety_defaults: Mapping[tuple[Any, ...], float | int],
    path: str,
) -> CompiledRoute:
    if not isinstance(raw, Mapping):
        raise validation(f"{path} must be an object")
    validate_json_finite(raw, path)
    required(raw, ("route_id", "route_version", "enabled", "inputs", "transforms", "destination", "validity"), path)
    identifier(raw["route_id"], f"{path}.route_id")
    integer(raw["route_version"], f"{path}.route_version", minimum=1)
    if not isinstance(raw["enabled"], bool):
        raise validation(f"{path}.enabled must be boolean")
    for optional_text in ("label", "notes"):
        if optional_text in raw and not isinstance(raw[optional_text], str):
            raise validation(f"{path}.{optional_text} must be a string")
    inputs_raw = raw["inputs"]
    if not isinstance(inputs_raw, list) or not inputs_raw:
        raise validation(f"{path}.inputs must be non-empty")
    inputs: list[str] = []
    ranges: list[tuple[float, float]] = []
    for index, item in enumerate(inputs_raw):
        if not isinstance(item, Mapping) or "channel" not in item:
            raise validation(f"{path}.inputs[{index}] requires channel")
        address = item["channel"]
        split_channel(address, f"{path}.inputs[{index}].channel")
        if address not in channel_ranges:
            raise validation(f"{path}.inputs[{index}] references unknown channel {address!r}")
        if "include_when" in item:
            raise validation(f"{path}.inputs[{index}].include_when is only valid for aggregators")
        inputs.append(address)
        ranges.append(channel_ranges[address])
    transforms = raw["transforms"]
    if not isinstance(transforms, list):
        raise validation(f"{path}.transforms must be an array")
    if len(inputs) > 1 and (
        not transforms
        or not isinstance(transforms[0], Mapping)
        or transforms[0].get("type") != "combine"
    ):
        raise validation(f"{path} multiple inputs require combine as the first transform")
    if len(inputs) == 1:
        current_range = ranges[0]
    else:
        operator, weights = _validate_combine(transforms[0], len(inputs), f"{path}.transforms[0]")
        current_range = _combine_range(operator, ranges, weights)
    has_edge = False
    for index, transform in enumerate(transforms):
        tpath = f"{path}.transforms[{index}]"
        if not isinstance(transform, Mapping):
            raise validation(f"{tpath} must be an object")
        kind = transform.get("type")
        if kind not in {"scale_range", "curve", "smoothing", "gate", "combine", "phase_accumulator"}:
            raise validation(f"{tpath}.type is invalid")
        if kind == "combine":
            if index != 0 or len(inputs) == 1:
                raise validation(f"{tpath} combine is only first for multi-input routes")
            continue
        if kind == "scale_range":
            in_first, in_second = range_pair(transform.get("in"), f"{tpath}.in")
            out_first, out_second = range_pair(transform.get("out"), f"{tpath}.out")
            if not isinstance(transform.get("clamp"), bool):
                raise validation(f"{tpath}.clamp must be boolean")
            if transform["clamp"]:
                current_range = min(out_first, out_second), max(out_first, out_second)
            else:
                mapped = [out_first + ((point - in_first) / (in_second - in_first)) * (out_second - out_first) for point in current_range]
                current_range = min(mapped), max(mapped)
        elif kind == "curve":
            curve_kind, parameter = _validate_curve(transform, tpath)
            values = [_curve(current_range[0], curve_kind, parameter), _curve(current_range[1], curve_kind, parameter)]
            if curve_kind == "piecewise":
                values.extend(point[1] for point in parameter)
            current_range = min(values), max(values)
        elif kind == "smoothing":
            if transform.get("kind") not in {"one_pole", "ramp"}:
                raise validation(f"{tpath}.kind is invalid")
            nonnegative(transform.get("time_ms"), f"{tpath}.time_ms")
        elif kind == "gate":
            finite(transform.get("threshold"), f"{tpath}.threshold")
            hysteresis = nonnegative(transform.get("hysteresis"), f"{tpath}.hysteresis")
            mode = transform.get("mode")
            if mode not in {"level", "rising_edge", "falling_edge"}:
                raise validation(f"{tpath}.mode is invalid")
            has_edge = has_edge or mode in {"rising_edge", "falling_edge"}
            closed = transform.get("closed")
            if closed != "suppress":
                if not isinstance(closed, Mapping) or set(closed) != {"value"}:
                    raise validation(f"{tpath}.closed must be suppress or {{value}}")
                closed_value = finite(closed["value"], f"{tpath}.closed.value")
                current_range = min(current_range[0], closed_value), max(current_range[1], closed_value)
            if mode in {"rising_edge", "falling_edge"}:
                current_range = (0.0, 1.0) if closed == "suppress" else (min(1.0, closed_value), max(1.0, closed_value))
        elif kind == "phase_accumulator":
            # Integrator: input is an angular velocity (deg/s), output is the
            # running phase wrapped to [0, wrap_deg). Output range is bounded by
            # the modulus regardless of the incoming velocity range.
            wrap_deg = positive(transform.get("wrap_deg", 360.0), f"{tpath}.wrap_deg")
            if "max_rate" in transform:
                positive(transform["max_rate"], f"{tpath}.max_rate")
            if "max_dt_ms" in transform:
                nonnegative(transform["max_dt_ms"], f"{tpath}.max_dt_ms")
            current_range = (0.0, wrap_deg)
    validity_policy = validate_validity(raw["validity"], f"{path}.validity")
    definition = copy.deepcopy(dict(raw))
    definition["validity"] = validity_policy
    destination = compile_destination(raw["destination"], instrument_manifests, safety_defaults, f"{path}.destination")
    if current_range[0] < destination.value_range[0] - 1e-12 or current_range[1] > destination.value_range[1] + 1e-12:
        raise validation(
            f"{path} output range {current_range} exceeds destination range {destination.value_range}"
        )
    return CompiledRoute(definition, tuple(inputs), destination, current_range, has_edge)


def evaluate_route(
    route: CompiledRoute,
    runtime: RouteRuntime,
    values: Mapping[str, ValueEnvelope],
    now_us: int,
) -> tuple[float | None, str]:
    """Evaluate one route. The returned reason is usable, suppress, or reset."""

    envelopes = [values.get(address, ValueEnvelope.invalid(now_us)) for address in route.inputs]
    policy = route.definition["validity"]
    usable = all(
        envelope.state in _STATES
        and envelope.state != INVALID
        and envelope.confidence >= policy["min_confidence"]
        and (envelope.state != HELD or policy["held"] == "accept")
        for envelope in envelopes
    )
    if route.has_edge_gate and any(envelope.state != OBSERVED for envelope in envelopes):
        usable = False
    if not usable:
        invalid_policy = policy["invalid"]
        if invalid_policy == "suppress":
            return None, "suppress"
        if invalid_policy in {"reset", "release"}:
            if runtime.invalid_reset_sent:
                return None, "suppress"
            runtime.invalid_reset_sent = True
            return None, "reset"
        if runtime.last_usable_output is not None and runtime.last_usable_at_us is not None:
            if now_us - runtime.last_usable_at_us < int(policy["hold_ms"] * 1000):
                return runtime.last_usable_output, "usable"
        if runtime.invalid_reset_sent:
            return None, "suppress"
        runtime.invalid_reset_sent = True
        return None, "reset"
    runtime.invalid_reset_sent = False
    current: float | list[float]
    if len(envelopes) == 1:
        current = envelopes[0].value
    else:
        current = [envelope.value for envelope in envelopes]
    for transform_index, transform in enumerate(route.definition["transforms"]):
        kind = transform["type"]
        if kind == "combine":
            operator, weights = _validate_combine(transform, len(envelopes), "runtime.combine")
            current = _apply_combine(operator, list(current), weights)  # type: ignore[arg-type]
        elif kind == "scale_range":
            assert isinstance(current, float)
            in_first, in_second = transform["in"]
            if transform["clamp"]:
                current = min(max(current, min(in_first, in_second)), max(in_first, in_second))
            out_first, out_second = transform["out"]
            current = out_first + ((current - in_first) / (in_second - in_first)) * (out_second - out_first)
        elif kind == "curve":
            assert isinstance(current, float)
            curve_kind, parameter = _validate_curve(transform, "runtime.curve")
            current = _curve(current, curve_kind, parameter)
        elif kind == "smoothing":
            assert isinstance(current, float)
            time_ms = float(transform["time_ms"])
            previous_value = runtime.smooth_values.get(transform_index)
            previous_at_us = runtime.smooth_at_us.get(transform_index)
            if previous_value is not None and previous_at_us is not None and time_ms > 0:
                dt_ms = max(0.0, (now_us - previous_at_us) / 1000.0)
                if transform["kind"] == "one_pole":
                    alpha = 1.0 - math.exp(-dt_ms / time_ms)
                else:
                    alpha = min(1.0, dt_ms / time_ms)
                current = previous_value + alpha * (current - previous_value)
            runtime.smooth_values[transform_index] = current
            runtime.smooth_at_us[transform_index] = now_us
        elif kind == "gate":
            assert isinstance(current, float)
            threshold = float(transform["threshold"])
            half = float(transform["hysteresis"]) / 2.0
            was_open = runtime.gate_states.get(transform_index, False)
            gate_open = was_open
            if gate_open:
                if current <= threshold - half:
                    gate_open = False
            elif current >= threshold + half:
                gate_open = True
            runtime.gate_states[transform_index] = gate_open
            mode = transform["mode"]
            passes = gate_open if mode == "level" else (
                gate_open and not was_open if mode == "rising_edge" else was_open and not gate_open
            )
            if passes:
                if mode != "level":
                    current = 1.0
            else:
                closed = transform["closed"]
                if closed == "suppress":
                    return None, "suppress"
                current = float(closed["value"])
        elif kind == "phase_accumulator":
            assert isinstance(current, float)
            wrap_deg = float(transform.get("wrap_deg", 360.0))
            velocity = current
            max_rate = transform.get("max_rate")
            if max_rate is not None:
                limit = abs(float(max_rate))
                velocity = min(max(velocity, -limit), limit)
            previous_phase = runtime.phase_values.get(transform_index, 0.0)
            previous_at_us = runtime.phase_at_us.get(transform_index)
            if previous_at_us is None:
                # First evaluation for this transform: establish the epoch, do
                # not integrate an undefined dt.
                phase = previous_phase % wrap_deg
            else:
                dt_s = max(0.0, (now_us - previous_at_us) / 1_000_000.0)
                # Clamp dt so a gap (route unusable, then usable again) cannot
                # produce a large phase jump on resume.
                max_dt_s = float(transform.get("max_dt_ms", 100.0)) / 1000.0
                if dt_s > max_dt_s:
                    dt_s = max_dt_s
                phase = (previous_phase + velocity * dt_s) % wrap_deg
            runtime.phase_values[transform_index] = phase
            runtime.phase_at_us[transform_index] = now_us
            current = phase
    if isinstance(current, list) or not math.isfinite(current):
        return None, "suppress"
    runtime.last_usable_output = current
    runtime.last_usable_at_us = now_us
    return current, "usable"


def _predicate_matches(predicate: Mapping[str, Any], values: Mapping[str, ValueEnvelope], now_us: int) -> bool:
    required(predicate, ("channel", "op", "value"), "include_when")
    split_channel(predicate["channel"], "include_when.channel")
    envelope = values.get(predicate["channel"], ValueEnvelope.invalid(now_us))
    if envelope.state == INVALID:
        return False
    target = finite(predicate["value"], "include_when.value")
    op = predicate["op"]
    operations = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "lt": lambda a, b: a < b,
        "lte": lambda a, b: a <= b,
        "gt": lambda a, b: a > b,
        "gte": lambda a, b: a >= b,
    }
    if op not in operations:
        raise validation("include_when.op is invalid")
    return operations[op](envelope.value, target)


def compile_aggregators(
    raw_aggregators: Any,
    base_ranges: Mapping[str, tuple[float, float]],
) -> tuple[tuple[CompiledAggregator, ...], dict[str, tuple[float, float]]]:
    if not isinstance(raw_aggregators, list):
        raise validation("scene.aggregators must be an array")
    pending = [copy.deepcopy(item) for item in raw_aggregators]
    ids: set[str] = set()
    outputs: set[str] = set()
    for index, raw in enumerate(pending):
        if not isinstance(raw, Mapping):
            raise validation(f"scene.aggregators[{index}] must be an object")
        validate_json_finite(raw, f"scene.aggregators[{index}]")
        required(raw, ("aggregator_id", "aggregator_version", "derived_source_id", "output_channel", "inputs", "operator", "cadence", "validity"), f"scene.aggregators[{index}]")
        aggregator_id = identifier(raw["aggregator_id"], f"scene.aggregators[{index}].aggregator_id")
        if aggregator_id in ids:
            raise validation(f"duplicate aggregator_id {aggregator_id!r}")
        ids.add(aggregator_id)
        integer(raw["aggregator_version"], f"scene.aggregators[{index}].aggregator_version", minimum=1)
        source_id = identifier(raw["derived_source_id"], f"scene.aggregators[{index}].derived_source_id")
        output = f"{source_id}.{identifier(raw['output_channel'], f'scene.aggregators[{index}].output_channel')}"
        if output in base_ranges:
            raise validation(f"derived output {output!r} collides with an installed source channel")
        if output in outputs or source_id in {item.split('.', 1)[0] for item in outputs}:
            raise validation(f"derived_source_id {source_id!r} must identify exactly one aggregator")
        outputs.add(output)
    available = dict(base_ranges)
    compiled: list[CompiledAggregator] = []
    while pending:
        made_progress = False
        for raw in list(pending):
            inputs_raw = raw["inputs"]
            if not isinstance(inputs_raw, list) or not inputs_raw:
                raise validation(f"aggregator {raw['aggregator_id']!r} inputs must be non-empty")
            addresses = [item.get("channel") if isinstance(item, Mapping) else None for item in inputs_raw]
            if any(address not in available for address in addresses):
                unresolved = [address for address in addresses if address not in available]
                if all(isinstance(address, str) and address in outputs for address in unresolved):
                    continue
                raise validation(f"aggregator {raw['aggregator_id']!r} references unknown input(s): {unresolved}")
            ranges = [available[address] for address in addresses]
            operator, weights = _validate_combine(raw, len(ranges), f"aggregator {raw['aggregator_id']}")
            output_range = _combine_range(operator, ranges, weights)
            cadence = raw["cadence"]
            if not isinstance(cadence, Mapping) or cadence.get("mode") not in {"fixed_hz", "on_input"}:
                raise validation(f"aggregator {raw['aggregator_id']!r} cadence is invalid")
            rate_key = "rate_hz" if cadence["mode"] == "fixed_hz" else "max_rate_hz"
            positive(cadence.get(rate_key), f"aggregator {raw['aggregator_id']}.{rate_key}")
            validity_raw = raw["validity"]
            if not isinstance(validity_raw, Mapping):
                raise validation(f"aggregator {raw['aggregator_id']!r} validity must be an object")
            required(validity_raw, ("min_valid_count", "min_observed_count", "max_age_ms", "include_held", "held_max_ms", "confidence"), f"aggregator {raw['aggregator_id']}.validity")
            min_valid = integer(validity_raw["min_valid_count"], "min_valid_count", minimum=1)
            min_observed = integer(validity_raw["min_observed_count"], "min_observed_count", minimum=0)
            if min_valid > len(inputs_raw) or min_observed > len(inputs_raw):
                raise validation(f"aggregator {raw['aggregator_id']!r} validity count exceeds arity")
            nonnegative(validity_raw["max_age_ms"], "max_age_ms")
            nonnegative(validity_raw["held_max_ms"], "held_max_ms")
            if not isinstance(validity_raw["include_held"], bool):
                raise validation("include_held must be boolean")
            if validity_raw["confidence"] not in {"minimum", "mean", "product"}:
                raise validation("aggregator confidence reducer is invalid")
            for item in inputs_raw:
                if "include_when" in item:
                    predicate = item["include_when"]
                    if not isinstance(predicate, Mapping):
                        raise validation("include_when must be an object")
                    split_channel(predicate.get("channel"), "include_when.channel")
                    if predicate["channel"] not in available:
                        raise validation(f"include_when references unknown channel {predicate['channel']!r}")
                    if predicate.get("op") not in {"eq", "ne", "lt", "lte", "gt", "gte"}:
                        raise validation("include_when.op is invalid")
                    finite(predicate.get("value"), "include_when.value")
            output_address = f"{raw['derived_source_id']}.{raw['output_channel']}"
            compiled.append(CompiledAggregator(raw, tuple(inputs_raw), output_address, output_range, canonical_json_hash(raw), operator, tuple(weights) if weights is not None else None))
            available[output_address] = output_range
            pending.remove(raw)
            made_progress = True
        if not made_progress:
            raise validation("derived-source graph contains a cycle")
    return tuple(compiled), available


def evaluate_aggregator(
    aggregator: CompiledAggregator,
    runtime: AggregatorRuntime,
    values: Mapping[str, ValueEnvelope],
    now_us: int,
) -> ValueEnvelope:
    validity = aggregator.definition["validity"]
    usable: list[ValueEnvelope] = []
    for item in aggregator.inputs:
        if "include_when" in item and not _predicate_matches(item["include_when"], values, now_us):
            continue
        envelope = values.get(item["channel"], ValueEnvelope.invalid(now_us))
        if envelope.state == INVALID:
            continue
        if now_us - envelope.received_at_us > int(float(validity["max_age_ms"]) * 1000):
            continue
        if envelope.state == HELD and not validity["include_held"]:
            continue
        usable.append(envelope)
    observed_count = sum(item.state == OBSERVED for item in usable)
    if len(usable) >= validity["min_valid_count"] and observed_count >= validity["min_observed_count"]:
        value = _apply_combine(aggregator.operator, [item.value for item in usable], list(aggregator.weights) if aggregator.weights is not None else None)
        confidences = [item.confidence for item in usable]
        reducer = validity["confidence"]
        if reducer == "minimum":
            confidence = min(confidences)
        elif reducer == "mean":
            confidence = sum(confidences) / len(confidences)
        else:
            confidence = math.prod(confidences)
        state = OBSERVED if all(item.state == OBSERVED for item in usable) else HELD
        if state == HELD:
            confidence = min(confidence, runtime.last_output_confidence)
        else:
            runtime.cached_value = value
            runtime.cached_confidence = confidence
            runtime.cached_at_us = now_us
        runtime.last_output_confidence = confidence
        return ValueEnvelope(value, state, confidence, now_us, now_us)
    held_max_us = int(float(validity["held_max_ms"]) * 1000)
    if runtime.cached_value is not None and runtime.cached_at_us is not None and held_max_us > 0:
        age = now_us - runtime.cached_at_us
        if age <= held_max_us:
            confidence = runtime.cached_confidence * max(0.0, 1.0 - age / held_max_us)
            confidence = min(confidence, runtime.last_output_confidence)
            runtime.last_output_confidence = confidence
            return ValueEnvelope(runtime.cached_value, HELD, confidence, now_us, runtime.cached_at_us)
    runtime.last_output_confidence = 0.0
    return ValueEnvelope.invalid(now_us)


def validate_transition(
    raw: Any,
    path: str = "scene.transition",
    *,
    allow_overrides: bool = True,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("policy") not in {"crossfade", "held", "reset"}:
        raise validation(f"{path}.policy is invalid")
    clean = copy.deepcopy(dict(raw))
    policy = clean["policy"]
    if policy == "crossfade":
        clean["duration_ms"] = nonnegative(clean.get("duration_ms", 100.0), f"{path}.duration_ms")
        clean["await_valid_ms"] = nonnegative(clean.get("await_valid_ms", 250.0), f"{path}.await_valid_ms")
    elif policy == "held":
        clean["hold_ms"] = nonnegative(clean.get("hold_ms", 250.0), f"{path}.hold_ms")
    if "destination_overrides" in clean:
        if not allow_overrides:
            raise validation(f"{path}.destination_overrides cannot be nested")
        overrides = clean["destination_overrides"]
        if not isinstance(overrides, list):
            raise validation(f"{path}.destination_overrides must be an array")
        cleaned_overrides: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for index, override in enumerate(overrides):
            override_path = f"{path}.destination_overrides[{index}]"
            if not isinstance(override, Mapping):
                raise validation(f"{override_path} must be an object")
            required(override, ("destination", "transition"), override_path)
            destination = override["destination"]
            if not isinstance(destination, Mapping):
                raise validation(f"{override_path}.destination must be an object")
            required(destination, ("instrument_id", "capability", "bindings", "argument"), f"{override_path}.destination")
            identifier(destination["instrument_id"], f"{override_path}.destination.instrument_id")
            identifier(destination["capability"], f"{override_path}.destination.capability")
            identifier(destination["argument"], f"{override_path}.destination.argument")
            if not isinstance(destination["bindings"], Mapping):
                raise validation(f"{override_path}.destination.bindings must be an object")
            for binding_name, binding_value in destination["bindings"].items():
                if not isinstance(binding_name, str) or not binding_name:
                    raise validation(f"{override_path}.destination.bindings keys must be non-empty strings")
                integer(binding_value, f"{override_path}.destination.bindings.{binding_name}")
            key = destination_key(destination)
            if key in seen:
                raise validation(f"{override_path} duplicates a destination override")
            seen.add(key)
            cleaned_overrides.append(
                {
                    "destination": copy.deepcopy(dict(destination)),
                    "transition": validate_transition(
                        override["transition"],
                        f"{override_path}.transition",
                        allow_overrides=False,
                    ),
                }
            )
        clean["destination_overrides"] = cleaned_overrides
    return clean


def compile_scene(
    raw: Any,
    base_channel_ranges: Mapping[str, tuple[float, float]],
    instrument_manifests: Mapping[str, Mapping[str, Any]],
    safety_defaults: Mapping[tuple[Any, ...], float | int],
) -> CompiledScene:
    if not isinstance(raw, Mapping):
        raise validation("scene must be an object")
    validate_json_finite(raw, "scene")
    required(raw, ("scene_id", "scene_version", "name", "description", "tags", "updated_at_us", "aggregators", "routes", "transition"), "scene")
    identifier(raw["scene_id"], "scene.scene_id")
    integer(raw["scene_version"], "scene.scene_version", minimum=1)
    if not isinstance(raw["name"], str) or not raw["name"].strip():
        raise validation("scene.name must be non-empty")
    if not isinstance(raw["description"], str) or not isinstance(raw["tags"], list):
        raise validation("scene.description and scene.tags have invalid types")
    if any(not isinstance(tag, str) or not tag for tag in raw["tags"]):
        raise validation("scene.tags must contain non-empty strings")
    if "created_at_us" in raw:
        integer(raw["created_at_us"], "scene.created_at_us", minimum=0)
    integer(raw["updated_at_us"], "scene.updated_at_us", minimum=0)
    transition = validate_transition(raw["transition"])
    aggregators, ranges = compile_aggregators(raw["aggregators"], base_channel_ranges)
    routes_raw = raw["routes"]
    if not isinstance(routes_raw, list):
        raise validation("scene.routes must be an array")
    routes: list[CompiledRoute] = []
    route_ids: set[str] = set()
    destinations: dict[tuple[Any, ...], str] = {}
    for index, route_raw in enumerate(routes_raw):
        route = compile_route(route_raw, ranges, instrument_manifests, safety_defaults, f"scene.routes[{index}]")
        if route.route_id in route_ids:
            raise validation(f"duplicate route_id {route.route_id!r}")
        route_ids.add(route.route_id)
        if route.definition["enabled"]:
            previous = destinations.get(route.destination.key)
            if previous is not None:
                raise WeaverError("destination_collision", f"routes {previous!r} and {route.route_id!r} target the same destination")
            destinations[route.destination.key] = route.route_id
        routes.append(route)
    route_destinations = {route.destination.key for route in routes}
    for override in transition.get("destination_overrides", ()):
        if destination_key(override["destination"]) not in route_destinations:
            raise validation("transition destination override does not match a scene route")
    definition = copy.deepcopy(dict(raw))
    definition["transition"] = transition
    return CompiledScene(definition, tuple(routes), aggregators, ranges)


__all__ = [
    "AggregatorRuntime",
    "CompiledAggregator",
    "CompiledRoute",
    "CompiledScene",
    "DestinationSpec",
    "RouteRuntime",
    "compile_scene",
    "destination_key",
    "evaluate_aggregator",
    "evaluate_route",
    "finite",
    "identifier",
    "split_channel",
    "validate_transition",
    "validate_json_finite",
]
