"""Portable codec and validator for Harmonic Weaver OSC contracts.

This module intentionally uses only the Python standard library and has no
package-relative imports.  It may be copied unchanged into a source or
instrument repository.

It provides:

* canonical JSON and SHA-256/128 contract identifiers;
* validation for Source Frame v1 and Instrument Control v1 manifests;
* golden-sidecar verification; and
* small OSC 1.0 message/bundle primitives for integration tests and adapters.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
from typing import Any


CONTRACT_ID_EXCLUDED_KEYS = (
    "contract_id",
    "golden_hash",
    "expected_contract_id",
)

_CONTRACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_IMMEDIATELY = b"\x00\x00\x00\x00\x00\x00\x00\x01"

_COMMON_REQUIRED = (
    "name",
    "contract_type",
    "contract_version",
    "schema_version",
    "namespace",
    "transport",
    "hashes",
    "stream_identity",
    "counters",
    "handshake",
    "receiver_rules",
)
_SOURCE_REQUIRED = (
    "source",
    "presence",
    "states_enum",
    "channels",
    "frame",
    "addresses",
    "producer_rules",
)
_INSTRUMENT_REQUIRED = (
    "instrument",
    "capabilities",
    "state_sync",
)


class ContractValidationError(ValueError):
    """Raised when a contract manifest is structurally invalid."""


class ContractIdMismatch(ContractValidationError):
    """Raised when a manifest does not match an expected contract_id."""


# ---------------------------------------------------------------------------
# Canonical JSON and contract identity
# ---------------------------------------------------------------------------


def canonical_json_bytes(obj: Any, exclude_keys: tuple[str, ...] = ()) -> bytes:
    """Return canonical UTF-8 JSON: sorted keys, no whitespace or NaN/Inf.

    ``exclude_keys`` applies only to top-level dictionary keys.  This matches
    the HarMoCAP mold and prevents self-reference by identifier fields without
    changing nested contract vocabulary.
    """

    if exclude_keys:
        if not isinstance(obj, dict):
            raise TypeError("exclude_keys requires a top-level JSON object")
        obj = {key: value for key, value in obj.items() if key not in exclude_keys}
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_dumps(obj: Any, exclude_keys: tuple[str, ...] = ()) -> str:
    """Return canonical JSON as text."""

    return canonical_json_bytes(obj, exclude_keys).decode("utf-8")


def canonical_json_hash(obj: dict[str, Any],
                        exclude_keys: tuple[str, ...] = ()) -> str:
    """Return SHA-256 truncated to 128 bits as 32 lowercase hex characters."""

    return hashlib.sha256(canonical_json_bytes(obj, exclude_keys)).hexdigest()[:32]


def contract_id_from_manifest(manifest: dict[str, Any]) -> str:
    """Compute the normative contract identifier for a manifest."""

    return canonical_json_hash(manifest, CONTRACT_ID_EXCLUDED_KEYS)


def encode_manifest(manifest: dict[str, Any]) -> bytes:
    """Encode a manifest as canonical JSON bytes without altering id fields."""

    return canonical_json_bytes(manifest)


def decode_manifest(payload: bytes | bytearray | str) -> dict[str, Any]:
    """Decode canonical or ordinary JSON text into a manifest object."""

    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8")
    if not isinstance(payload, str):
        raise TypeError("manifest payload must be str, bytes or bytearray")
    try:
        manifest = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractValidationError(f"invalid manifest JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ContractValidationError("manifest root must be a JSON object")
    return manifest


def load_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a UTF-8 JSON manifest from ``path``."""

    with open(path, "rb") as handle:
        return decode_manifest(handle.read())


def verify_contract_id(manifest: dict[str, Any], expected_contract_id: str) -> str:
    """Return the computed id, or raise if it differs from ``expected``."""

    expected = expected_contract_id.strip()
    if not _CONTRACT_ID_RE.fullmatch(expected):
        raise ContractValidationError(
            "expected contract_id must be 32 lowercase hexadecimal characters"
        )
    actual = contract_id_from_manifest(manifest)
    if actual != expected:
        raise ContractIdMismatch(
            f"contract_id mismatch: expected {expected}, computed {actual}"
        )
    return actual


def check_golden_sidecar(manifest: dict[str, Any],
                         sidecar_path: str | os.PathLike[str]) -> str:
    """Verify a manifest against a sidecar containing one contract_id line."""

    try:
        with open(sidecar_path, "r", encoding="ascii") as handle:
            expected = handle.read().strip()
    except OSError as exc:
        raise ContractValidationError(
            f"cannot read contract_id sidecar {os.fspath(sidecar_path)!r}: {exc}"
        ) from exc
    return verify_contract_id(manifest, expected)


# A descriptive alias for consumers that prefer validator-style naming.
validate_golden_sidecar = check_golden_sidecar


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def _fail(path: str, message: str) -> None:
    raise ContractValidationError(f"{path}: {message}")


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _require_list(value: Any, path: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    if nonempty and not value:
        _fail(path, "must not be empty")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "must be a boolean")
    return value


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _positive_number(value: Any, path: str, *, allow_zero: bool = False) -> float:
    if not _is_finite_number(value):
        _fail(path, "must be a finite number")
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        _fail(path, f"must be {qualifier}")
    return float(value)


def _validate_range(value: Any, path: str, *, integer: bool = False) -> None:
    values = _require_list(value, path)
    if len(values) != 2:
        _fail(path, "must contain exactly [minimum, maximum]")
    low, high = values
    if integer:
        if isinstance(low, bool) or isinstance(high, bool) \
                or not isinstance(low, int) or not isinstance(high, int):
            _fail(path, "bounds must be integers")
    elif not _is_finite_number(low) or not _is_finite_number(high):
        _fail(path, "range endpoints must be finite numbers")
    if low >= high:
        _fail(path, "minimum must be strictly less than maximum")


def _require_keys(mapping: dict[str, Any], keys: tuple[str, ...], path: str) -> None:
    missing = [key for key in keys if key not in mapping]
    if missing:
        _fail(path, "missing required section(s): " + ", ".join(missing))


def _validate_common(manifest: dict[str, Any]) -> None:
    _require_keys(manifest, _COMMON_REQUIRED, "manifest")
    _require_string(manifest["name"], "name")
    _require_string(manifest["contract_version"], "contract_version")
    schema_version = _require_string(manifest["schema_version"], "schema_version")
    if not _SEMVER_RE.fullmatch(schema_version):
        _fail("schema_version", "must use MAJOR.MINOR.PATCH numeric form")
    namespace = _require_string(manifest["namespace"], "namespace")
    if not namespace.startswith("/") or namespace.endswith("/") or "//" in namespace:
        _fail("namespace", "must be an absolute OSC path without a trailing slash")
    _require_mapping(manifest["transport"], "transport")
    hashes = _require_mapping(manifest["hashes"], "hashes")
    _require_keys(hashes, ("algorithm", "contract_id"), "hashes")
    _require_mapping(manifest["stream_identity"], "stream_identity")
    _require_mapping(manifest["counters"], "counters")
    _require_mapping(manifest["handshake"], "handshake")
    _require_mapping(manifest["receiver_rules"], "receiver_rules")

    # This also proves that every value is representable by normative JSON.
    try:
        canonical_json_bytes(manifest)
    except (TypeError, ValueError) as exc:
        _fail("manifest", f"not canonical-JSON serializable: {exc}")

    if "contract_id" in manifest:
        verify_contract_id(manifest, _require_string(manifest["contract_id"],
                                                     "contract_id"))


def _validate_source(manifest: dict[str, Any]) -> None:
    _require_keys(manifest, _SOURCE_REQUIRED, "source manifest")
    namespace = manifest["namespace"]
    if not re.fullmatch(r"/src/[a-z][a-z0-9_-]*", namespace):
        _fail("namespace", "Source Frame v1 requires /src/<source_id>")

    source = _require_mapping(manifest["source"], "source")
    _require_keys(source, ("source_id", "description", "destination_independence"),
                  "source")
    source_id = _require_string(source["source_id"], "source.source_id")
    if not _IDENTIFIER_RE.fullmatch(source_id):
        _fail("source.source_id", "must match [a-z][a-z0-9_-]*")
    if namespace != f"/src/{source_id}":
        _fail("namespace", "must end with the declared source.source_id")

    presence = _require_mapping(manifest["presence"], "presence")
    _require_keys(presence, ("lease_ms", "renewed_by", "expiry"), "presence")
    _positive_number(presence["lease_ms"], "presence.lease_ms")

    states = _require_mapping(manifest["states_enum"], "states_enum")
    if set(states) != {"observed", "held", "invalid"}:
        _fail("states_enum", "must define exactly observed, held and invalid")
    for expected_code, state_name in enumerate(("observed", "held", "invalid")):
        state = _require_mapping(states[state_name], f"states_enum.{state_name}")
        _require_keys(state, ("code", "meaning"), f"states_enum.{state_name}")
        if state["code"] != expected_code:
            _fail(f"states_enum.{state_name}.code",
                  f"must be {expected_code} for wire compatibility")
        _require_string(state["meaning"], f"states_enum.{state_name}.meaning")

    channels = _require_list(manifest["channels"], "channels", nonempty=True)
    names: set[str] = set()
    for index, raw_channel in enumerate(channels):
        path = f"channels[{index}]"
        channel = _require_mapping(raw_channel, path)
        _require_keys(
            channel,
            ("name", "range", "polarity", "rate_hz_nominal", "smoothing_hints"),
            path,
        )
        name = _require_string(channel["name"], f"{path}.name")
        if not _IDENTIFIER_RE.fullmatch(name):
            _fail(f"{path}.name", "must match [a-z][a-z0-9_-]*")
        if name in names:
            _fail(f"{path}.name", f"duplicate channel {name!r}")
        names.add(name)
        _validate_range(channel["range"], f"{path}.range")
        _require_string(channel["polarity"], f"{path}.polarity")
        _positive_number(channel["rate_hz_nominal"], f"{path}.rate_hz_nominal")
        _require_string(channel["smoothing_hints"], f"{path}.smoothing_hints")

    frame = _require_mapping(manifest["frame"], "frame")
    _require_keys(frame, ("atomicity", "channel_payload", "completeness",
                          "invalid_rule"), "frame")
    producer_rules = _require_mapping(manifest["producer_rules"], "producer_rules")
    _require_keys(producer_rules, ("normalized_values", "channel_names",
                                   "destination_names", "state_required"),
                  "producer_rules")

    handshake = manifest["handshake"]
    expected_hello = f"{namespace}/hello"
    expected_request = f"{namespace}/hello/request"
    if handshake.get("hello_address") != expected_hello:
        _fail("handshake.hello_address", f"must be {expected_hello}")
    if handshake.get("hello_request_address") != expected_request:
        _fail("handshake.hello_request_address", f"must be {expected_request}")

    addresses = _require_mapping(manifest["addresses"], "addresses")
    required_addresses = {
        expected_hello,
        expected_request,
        f"{namespace}/heartbeat",
        f"{namespace}/frame",
        f"{namespace}/{{channel}}",
    }
    missing_addresses = required_addresses.difference(addresses)
    if missing_addresses:
        _fail("addresses", "missing " + ", ".join(sorted(missing_addresses)))


def _validate_capability(raw_capability: Any, index: int, namespace: str,
                         seen_names: set[str], patterns: set[str]) -> None:
    path = f"capabilities[{index}]"
    capability = _require_mapping(raw_capability, path)
    _require_keys(
        capability,
        ("name", "address_pattern", "parameters", "arguments", "lag_ms",
         "smoothing", "read", "write"),
        path,
    )
    name = _require_string(capability["name"], f"{path}.name")
    if not _IDENTIFIER_RE.fullmatch(name):
        _fail(f"{path}.name", "must match [a-z][a-z0-9_-]*")
    if name in seen_names:
        _fail(f"{path}.name", f"duplicate capability {name!r}")
    seen_names.add(name)

    pattern = _require_string(capability["address_pattern"],
                              f"{path}.address_pattern")
    if not pattern.startswith(namespace + "/"):
        _fail(f"{path}.address_pattern", "must remain under the native namespace")
    if pattern in patterns:
        _fail(f"{path}.address_pattern", "must be unique")
    patterns.add(pattern)

    placeholders = set(_PLACEHOLDER_RE.findall(pattern))
    parameters = _require_mapping(capability["parameters"], f"{path}.parameters")
    if placeholders != set(parameters):
        _fail(
            f"{path}.parameters",
            "keys must exactly match address placeholders " + repr(sorted(placeholders)),
        )
    for parameter_name, raw_parameter in parameters.items():
        parameter_path = f"{path}.parameters.{parameter_name}"
        parameter = _require_mapping(raw_parameter, parameter_path)
        _require_keys(parameter, ("type", "bounds"), parameter_path)
        if parameter["type"] not in ("int32", "int64"):
            _fail(f"{parameter_path}.type", "path parameters must be int32 or int64")
        _validate_range(parameter["bounds"], f"{parameter_path}.bounds", integer=True)

    arguments = _require_list(capability["arguments"], f"{path}.arguments",
                              nonempty=True)
    argument_names: set[str] = set()
    for argument_index, raw_argument in enumerate(arguments):
        argument_path = f"{path}.arguments[{argument_index}]"
        argument = _require_mapping(raw_argument, argument_path)
        _require_keys(argument, ("name", "type"), argument_path)
        argument_name = _require_string(argument["name"], f"{argument_path}.name")
        if argument_name in argument_names:
            _fail(f"{argument_path}.name", "must be unique within the capability")
        argument_names.add(argument_name)
        argument_type = argument["type"]
        if argument_type not in ("float32", "float64", "int32", "int64",
                                 "string", "blob"):
            _fail(f"{argument_path}.type", "unsupported OSC argument type")
        if argument_type in ("float32", "float64", "int32", "int64"):
            if "range" not in argument:
                _fail(argument_path, "numeric arguments require a range")
            _validate_range(
                argument["range"],
                f"{argument_path}.range",
                integer=argument_type in ("int32", "int64"),
            )

    _positive_number(capability["lag_ms"], f"{path}.lag_ms", allow_zero=True)
    _require_string(capability["smoothing"], f"{path}.smoothing")
    readable = _require_bool(capability["read"], f"{path}.read")
    writable = _require_bool(capability["write"], f"{path}.write")
    if not readable and not writable:
        _fail(path, "at least one of read or write must be true")


def _validate_state_sync(raw_state_sync: Any, namespace: str) -> None:
    state_sync = _require_mapping(raw_state_sync, "state_sync")
    _require_keys(state_sync, ("mode", "response", "connect_rule"), "state_sync")
    mode = state_sync["mode"]
    if mode not in ("dump", "periodic_broadcast", "dump_and_periodic"):
        _fail("state_sync.mode",
              "must be dump, periodic_broadcast or dump_and_periodic")
    _require_string(state_sync["response"], "state_sync.response")
    _require_string(state_sync["connect_rule"], "state_sync.connect_rule")

    if mode in ("dump", "dump_and_periodic"):
        request_address = _require_string(
            state_sync.get("request_address"), "state_sync.request_address"
        )
        if not request_address.startswith(namespace + "/"):
            _fail("state_sync.request_address", "must use the native namespace")
    if mode in ("periodic_broadcast", "dump_and_periodic"):
        if state_sync.get("periodic_broadcast") is not True:
            _fail("state_sync.periodic_broadcast", "must be true for periodic mode")
        _positive_number(state_sync.get("broadcast_interval_ms"),
                         "state_sync.broadcast_interval_ms")


def _validate_voice_alias(raw_alias: Any, patterns: set[str]) -> None:
    if raw_alias is None:
        return
    alias = _require_mapping(raw_alias, "voice_model_alias")
    _require_keys(alias, ("enabled", "status", "voice_parameter", "voice_bounds",
                          "mapping"), "voice_model_alias")
    _require_bool(alias["enabled"], "voice_model_alias.enabled")
    _require_string(alias["status"], "voice_model_alias.status")
    voice_parameter = _require_string(alias["voice_parameter"],
                                      "voice_model_alias.voice_parameter")
    _validate_range(alias["voice_bounds"], "voice_model_alias.voice_bounds",
                    integer=True)
    mapping = _require_mapping(alias["mapping"], "voice_model_alias.mapping")
    if set(mapping) != {"gain", "pan", "phase"}:
        _fail("voice_model_alias.mapping", "must map exactly gain, pan and phase")
    for logical_name, address_pattern in mapping.items():
        address = _require_string(address_pattern,
                                  f"voice_model_alias.mapping.{logical_name}")
        if address not in patterns:
            _fail(f"voice_model_alias.mapping.{logical_name}",
                  "must reference a declared capability address_pattern")
        if set(_PLACEHOLDER_RE.findall(address)) != {voice_parameter}:
            _fail(f"voice_model_alias.mapping.{logical_name}",
                  "must use only the declared voice_parameter placeholder")


def _validate_instrument(manifest: dict[str, Any]) -> None:
    _require_keys(manifest, _INSTRUMENT_REQUIRED, "instrument manifest")
    namespace = manifest["namespace"]
    if namespace == "/src" or namespace.startswith("/src/"):
        _fail("namespace", "instrument contracts cannot use the source namespace")

    instrument = _require_mapping(manifest["instrument"], "instrument")
    _require_keys(instrument, ("instrument_id", "description",
                               "native_namespace_rule"), "instrument")
    instrument_id = _require_string(instrument["instrument_id"],
                                    "instrument.instrument_id")
    if not _IDENTIFIER_RE.fullmatch(instrument_id):
        _fail("instrument.instrument_id", "must match [a-z][a-z0-9_-]*")

    handshake = manifest["handshake"]
    expected_hello = f"{namespace}/hello"
    expected_request = f"{namespace}/hello/request"
    if handshake.get("hello_address") != expected_hello:
        _fail("handshake.hello_address", f"must be {expected_hello}")
    if handshake.get("hello_request_address") != expected_request:
        _fail("handshake.hello_request_address", f"must be {expected_request}")
    hello_args = _require_list(handshake.get("hello_args"), "handshake.hello_args",
                               nonempty=True)
    if "contract_id" not in hello_args or "stream_id" not in hello_args:
        _fail("handshake.hello_args", "must carry stream_id and contract_id")

    capabilities = _require_list(manifest["capabilities"], "capabilities",
                                 nonempty=True)
    seen_names: set[str] = set()
    patterns: set[str] = set()
    for index, capability in enumerate(capabilities):
        _validate_capability(capability, index, namespace, seen_names, patterns)
    _validate_state_sync(manifest["state_sync"], namespace)
    if "voice_model_alias" in manifest:
        _validate_voice_alias(manifest["voice_model_alias"], patterns)


def validate_manifest(manifest: dict[str, Any], *,
                      expected_contract_id: str | None = None) -> dict[str, Any]:
    """Validate a Source Frame v1 or Instrument Control v1 manifest.

    The original manifest is returned for convenient load/validate pipelines.
    Structural errors and identifier mismatches raise
    :class:`ContractValidationError`.
    """

    if not isinstance(manifest, dict):
        raise ContractValidationError("manifest root must be a JSON object")
    _validate_common(manifest)
    contract_type = manifest["contract_type"]
    if contract_type == "source_frame":
        _validate_source(manifest)
    elif contract_type == "instrument_control":
        _validate_instrument(manifest)
    else:
        _fail("contract_type", "must be source_frame or instrument_control")
    if expected_contract_id is not None:
        verify_contract_id(manifest, expected_contract_id)
    return manifest


# ---------------------------------------------------------------------------
# OSC 1.0 primitives (stdlib only)
# ---------------------------------------------------------------------------


def _pad4(payload: bytes) -> bytes:
    return payload + b"\x00" * ((4 - len(payload) % 4) % 4)


def _encode_string(value: str) -> bytes:
    if "\x00" in value:
        raise ValueError("OSC strings cannot contain NUL")
    return _pad4(value.encode("utf-8") + b"\x00")


def _encode_blob(value: bytes) -> bytes:
    return struct.pack(">i", len(value)) + _pad4(value)


def encode_message(address: str, args: list[Any]) -> bytes:
    """Encode one OSC message.

    Python ``int``, ``float``, ``str`` and bytes-like values map to ``i``,
    ``f``, ``s`` and ``b``.  Use ``("h", int)`` or ``("d", float)`` for
    explicit int64 or float64 values.  Booleans and non-finite floats are
    rejected so control data remains unambiguous.
    """

    if not isinstance(address, str) or not address.startswith("/"):
        raise ValueError("OSC address must be an absolute string path")
    typetags = ","
    payload = b""
    for argument in args:
        if isinstance(argument, tuple):
            if len(argument) != 2:
                raise ValueError("explicit OSC argument must be a (tag, value) pair")
            tag, value = argument
            if tag == "h" and isinstance(value, int) and not isinstance(value, bool):
                typetags += "h"
                payload += struct.pack(">q", value)
            elif tag == "d" and _is_finite_number(value):
                typetags += "d"
                payload += struct.pack(">d", float(value))
            else:
                raise ValueError(f"unsupported explicit OSC argument {argument!r}")
        elif isinstance(argument, bool):
            raise ValueError("bool is not allowed; use an explicit int")
        elif isinstance(argument, int):
            typetags += "i"
            payload += struct.pack(">i", argument)
        elif isinstance(argument, float):
            if not math.isfinite(argument):
                raise ValueError("non-finite OSC floats are prohibited")
            typetags += "f"
            payload += struct.pack(">f", argument)
        elif isinstance(argument, str):
            typetags += "s"
            payload += _encode_string(argument)
        elif isinstance(argument, (bytes, bytearray)):
            typetags += "b"
            payload += _encode_blob(bytes(argument))
        else:
            raise ValueError(f"unsupported OSC argument type: {type(argument).__name__}")
    return _encode_string(address) + _encode_string(typetags) + payload


def encode_bundle(messages: list[bytes], timetag: bytes = _IMMEDIATELY) -> bytes:
    """Encode an OSC bundle with the ``immediately`` timetag by default."""

    if len(timetag) != 8:
        raise ValueError("OSC timetag must contain exactly 8 bytes")
    output = _encode_string("#bundle") + timetag
    for message in messages:
        if not isinstance(message, (bytes, bytearray)):
            raise TypeError("bundle members must be encoded OSC bytes")
        member = bytes(message)
        output += struct.pack(">i", len(member)) + member
    return output


def _decode_string(data: bytes, offset: int) -> tuple[str, int]:
    try:
        end = data.index(b"\x00", offset)
    except ValueError as exc:
        raise ValueError("unterminated OSC string") from exc
    try:
        value = data[offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid UTF-8 in OSC string") from exc
    offset = end + 1
    offset += (4 - offset % 4) % 4
    if offset > len(data):
        raise ValueError("truncated OSC string padding")
    return value, offset


def decode_message(data: bytes | bytearray) -> tuple[str, list[Any]]:
    """Decode one OSC message produced by :func:`encode_message`."""

    packet = bytes(data)
    address, offset = _decode_string(packet, 0)
    if not address.startswith("/"):
        raise ValueError("invalid OSC address")
    typetags, offset = _decode_string(packet, offset)
    if not typetags.startswith(","):
        raise ValueError("invalid OSC typetag string")
    arguments: list[Any] = []
    try:
        for tag in typetags[1:]:
            if tag == "i":
                arguments.append(struct.unpack_from(">i", packet, offset)[0])
                offset += 4
            elif tag == "f":
                arguments.append(struct.unpack_from(">f", packet, offset)[0])
                offset += 4
            elif tag == "h":
                arguments.append(struct.unpack_from(">q", packet, offset)[0])
                offset += 8
            elif tag == "d":
                arguments.append(struct.unpack_from(">d", packet, offset)[0])
                offset += 8
            elif tag == "s":
                value, offset = _decode_string(packet, offset)
                arguments.append(value)
            elif tag == "b":
                length = struct.unpack_from(">i", packet, offset)[0]
                offset += 4
                if length < 0 or offset + length > len(packet):
                    raise ValueError("invalid OSC blob length")
                arguments.append(packet[offset:offset + length])
                offset += length + (4 - length % 4) % 4
            else:
                raise ValueError(f"unsupported OSC typetag: {tag}")
    except struct.error as exc:
        raise ValueError("truncated OSC message") from exc
    if offset != len(packet):
        raise ValueError("trailing bytes in OSC message")
    return address, arguments


def decode_bundle(data: bytes | bytearray) -> list[tuple[str, list[Any]]]:
    """Decode an OSC bundle, or return a one-item list for a bare message."""

    packet = bytes(data)
    if not packet.startswith(b"#bundle\x00"):
        return [decode_message(packet)]
    if len(packet) < 16:
        raise ValueError("truncated OSC bundle header")
    offset = 16  # padded '#bundle' plus 8-byte timetag
    messages: list[tuple[str, list[Any]]] = []
    try:
        while offset < len(packet):
            length = struct.unpack_from(">i", packet, offset)[0]
            offset += 4
            if length <= 0 or offset + length > len(packet):
                raise ValueError("invalid OSC bundle member length")
            messages.append(decode_message(packet[offset:offset + length]))
            offset += length
    except struct.error as exc:
        raise ValueError("truncated OSC bundle") from exc
    return messages


__all__ = [
    "CONTRACT_ID_EXCLUDED_KEYS",
    "ContractIdMismatch",
    "ContractValidationError",
    "canonical_json_bytes",
    "canonical_json_dumps",
    "canonical_json_hash",
    "check_golden_sidecar",
    "contract_id_from_manifest",
    "decode_bundle",
    "decode_manifest",
    "decode_message",
    "encode_bundle",
    "encode_manifest",
    "encode_message",
    "load_manifest",
    "validate_golden_sidecar",
    "validate_manifest",
    "verify_contract_id",
]
