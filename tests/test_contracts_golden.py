"""Golden reference tests for the two ecosystem contract planes."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from harmonic_weaver.contract_codec import (
    ContractIdMismatch,
    check_golden_sidecar,
    decode_bundle,
    decode_manifest,
    encode_bundle,
    encode_manifest,
    encode_message,
    load_manifest,
    validate_manifest,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = REPO_ROOT / "contracts"
SOURCE_TEMPLATE = CONTRACTS / "source_frame.template.json"
SOURCE_GOLDEN = CONTRACTS / "source_frame.contract_id.golden"
INSTRUMENT_TEMPLATE = CONTRACTS / "instrument_contract.template.json"
INSTRUMENT_GOLDEN = CONTRACTS / "instrument_contract.contract_id.golden"


def minimal_source_manifest() -> dict:
    namespace = "/src/test-source"
    return {
        "name": "test-source-frame",
        "contract_type": "source_frame",
        "contract_version": "1.0",
        "schema_version": "1.0.0",
        "namespace": namespace,
        "transport": {},
        "hashes": {"algorithm": "SHA-256/128", "contract_id": "canonical JSON"},
        "source": {
            "source_id": "test-source",
            "description": "Test signal.",
            "destination_independence": "Source-domain names only.",
        },
        "stream_identity": {},
        "counters": {},
        "presence": {
            "lease_ms": 2000,
            "renewed_by": ["valid frame"],
            "expiry": "Invalidate the source.",
        },
        "states_enum": {
            "observed": {"code": 0, "meaning": "Fresh value."},
            "held": {"code": 1, "meaning": "Held value with decay."},
            "invalid": {"code": 2, "meaning": "Ignore value."},
        },
        "channels": [{
            "name": "level",
            "range": [0.0, 1.0],
            "polarity": "0 = low; 1 = high.",
            "rate_hz_nominal": 10.0,
            "smoothing_hints": "Interpolate to the consumer rate.",
        }],
        "frame": {
            "atomicity": "One bundle.",
            "channel_payload": ["value", "state", "confidence"],
            "completeness": "Every channel is present.",
            "invalid_rule": "Ignore invalid values.",
        },
        "handshake": {
            "hello_address": f"{namespace}/hello",
            "hello_request_address": f"{namespace}/hello/request",
        },
        "addresses": {
            f"{namespace}/hello": {},
            f"{namespace}/hello/request": {},
            f"{namespace}/heartbeat": {},
            f"{namespace}/frame": {},
            f"{namespace}/{{channel}}": {},
        },
        "producer_rules": {
            "normalized_values": "Finite and in range.",
            "channel_names": "Stable source-domain names.",
            "destination_names": "Forbidden.",
            "state_required": "Every channel carries state.",
        },
        "receiver_rules": {},
    }


def minimal_instrument_manifest() -> dict:
    namespace = "/test-instrument"
    return {
        "name": "test-instrument-control",
        "contract_type": "instrument_control",
        "contract_version": "1.0",
        "schema_version": "1.0.0",
        "namespace": namespace,
        "transport": {},
        "hashes": {"algorithm": "SHA-256/128", "contract_id": "canonical JSON"},
        "instrument": {
            "instrument_id": "test-instrument",
            "description": "Test instrument.",
            "native_namespace_rule": "Keep the native namespace.",
        },
        "stream_identity": {},
        "counters": {},
        "handshake": {
            "hello_address": f"{namespace}/hello",
            "hello_request_address": f"{namespace}/hello/request",
            "hello_args": ["stream_id", "contract_id"],
        },
        "capabilities": [{
            "name": "gain",
            "address_pattern": f"{namespace}/gain/{{N}}",
            "parameters": {"N": {"type": "int32", "bounds": [0, 1]}},
            "arguments": [{
                "name": "gain",
                "type": "float32",
                "range": [0.0, 1.0],
            }],
            "lag_ms": 0.0,
            "smoothing": "No smoothing.",
            "read": True,
            "write": True,
        }],
        "state_sync": {
            "mode": "dump",
            "request_address": f"{namespace}/state",
            "response": "Atomic current-state bundle.",
            "connect_rule": "Synchronize after hello.",
        },
        "receiver_rules": {},
    }


class ContractGoldenTests(unittest.TestCase):
    def test_templates_validate_and_match_sidecars(self) -> None:
        pairs = (
            (SOURCE_TEMPLATE, SOURCE_GOLDEN),
            (INSTRUMENT_TEMPLATE, INSTRUMENT_GOLDEN),
        )
        for manifest_path, golden_path in pairs:
            with self.subTest(manifest=manifest_path.name):
                manifest = load_manifest(manifest_path)
                self.assertIs(validate_manifest(manifest), manifest)
                self.assertEqual(check_golden_sidecar(manifest, golden_path),
                                 golden_path.read_text(encoding="ascii").strip())

    def test_mutated_manifest_fails_original_id_check(self) -> None:
        manifest = copy.deepcopy(load_manifest(SOURCE_TEMPLATE))
        manifest["channels"][0]["rate_hz_nominal"] = 60.0
        validate_manifest(manifest)
        with self.assertRaises(ContractIdMismatch):
            check_golden_sidecar(manifest, SOURCE_GOLDEN)

    def test_minimal_manifests_round_trip_through_canonical_codec(self) -> None:
        for manifest in (minimal_source_manifest(), minimal_instrument_manifest()):
            with self.subTest(contract_type=manifest["contract_type"]):
                validate_manifest(manifest)
                decoded = decode_manifest(encode_manifest(manifest))
                self.assertEqual(decoded, manifest)
                validate_manifest(decoded)

    def test_minimal_source_frame_round_trips_through_osc_codec(self) -> None:
        frame = encode_message("/src/test-source/frame", [
            "0123456789abcdef",
            ("h", 1),
            ("h", 123456),
            "0123456789abcdef0123456789abcdef",
        ])
        channel = encode_message("/src/test-source/level", [0.25, 0, 0.9])
        decoded = decode_bundle(encode_bundle([frame, channel]))

        self.assertEqual(decoded[0], (
            "/src/test-source/frame",
            ["0123456789abcdef", 1, 123456,
             "0123456789abcdef0123456789abcdef"],
        ))
        self.assertEqual(decoded[1][0], "/src/test-source/level")
        self.assertAlmostEqual(decoded[1][1][0], 0.25)
        self.assertEqual(decoded[1][1][1], 0)
        self.assertAlmostEqual(decoded[1][1][2], 0.9, places=6)


if __name__ == "__main__":
    unittest.main()
