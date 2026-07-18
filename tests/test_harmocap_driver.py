"""Tests for the HarMoCAP source driver (T4.3a).

Replays kit fixtures by encoding them with the kit's ``osc_codec`` (path
import only in tests — the driver itself is stdlib and self-contained) and
feeding raw datagrams into ``HarMoCAPDriver.handle_datagram`` (no UDP ports).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Imports: driver under test + kit codec (encode only, for fixtures → wire)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Prefer package import; fall back to loading the module file directly so the
# test works before the orchestrator adds drivers/__init__.py.
try:
    from harmonic_weaver.drivers.harmocap_driver import (  # type: ignore
        FEATURE_NAMES,
        KEYPOINT_NAMES,
        STATE_HELD,
        STATE_INVALID,
        STATE_OBSERVED,
        HarMoCAPDriver,
        channel_names,
        registry_address,
    )
except ModuleNotFoundError:
    _path = SRC / "harmonic_weaver" / "drivers" / "harmocap_driver.py"
    _spec = importlib.util.spec_from_file_location("harmocap_driver", _path)
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    FEATURE_NAMES = _mod.FEATURE_NAMES
    KEYPOINT_NAMES = _mod.KEYPOINT_NAMES
    STATE_HELD = _mod.STATE_HELD
    STATE_INVALID = _mod.STATE_INVALID
    STATE_OBSERVED = _mod.STATE_OBSERVED
    HarMoCAPDriver = _mod.HarMoCAPDriver
    channel_names = _mod.channel_names
    registry_address = _mod.registry_address

KIT_ROOT = Path.home() / "Projects" / "HarMoCAP" / "harmocap-nico-kit"
KIT_CODEC = KIT_ROOT / "osc_codec.py"
TWO_PERSONS = KIT_ROOT / "examples" / "fixtures" / "two_persons.jsonl"
LIFECYCLE = KIT_ROOT / "examples" / "fixtures" / "lifecycle.jsonl"
STREAM_RESTART = KIT_ROOT / "examples" / "fixtures" / "stream_restart.jsonl"


def _load_kit_codec():
    spec = importlib.util.spec_from_file_location("harmocap_kit_osc_codec", KIT_CODEC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


osc_codec = _load_kit_codec()


# ---------------------------------------------------------------------------
# Wire helpers (mirror kit replay.py frame_to_wire / handshake_bytes)
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict]:
    frames = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                frames.append(json.loads(line))
    return frames


def handshake_bytes(d: dict) -> list[bytes]:
    params = d.get("calibration_params")
    params_blob = b""
    calib_hash = "0" * 32
    if params:
        order = [
            "torso_height_norm",
            "vmax_hand",
            "vmax_center",
            "jerk_ref",
            "energy_ref",
            "accel_ref",
        ]
        params_blob = osc_codec.pack_calibration_params([params[k] for k in order])
        calib_hash = osc_codec.calibration_hash(params_blob)
    out = [
        osc_codec.build_hello(
            stream_id=d["stream_id"],
            schema_version=d["schema_version"],
            feature_set_version=d["feature_set_version"],
            producer_version=d["producer_version"] + "+test",
            model_id=d["model_id"],
            config_hash=d["config_hash"],
            contract_id=d["contract_id"],
            calibration_generation=d["calibration_generation"],
            calibration_state=d["calibration_state"],
            calib_hash=calib_hash,
            effective_from_frame_id=d.get("calibration_effective_from", 0),
            frame_w=d["frame_w"],
            frame_h=d["frame_h"],
        )
    ]
    if params:
        out.append(
            osc_codec.build_calibration(
                stream_id=d["stream_id"],
                generation=d["calibration_generation"],
                calib_hash=calib_hash,
                effective_from_frame_id=d.get("calibration_effective_from", 0),
                params_blob=params_blob,
            )
        )
    return out


def frame_to_wire(d: dict, first_seq: int, queued_us: int = 0) -> list[bytes]:
    bundles: list[bytes] = []
    seq = first_seq
    for p in d.get("persons", []):
        if not p.get("present"):
            pw = {"slot_id": p["slot_id"], "present": False}
        else:
            kps = [(k[0], k[1], k[2]) for k in p["keypoints"]]
            kst = [(s[0], s[1], s[2]) for s in p["kp_state"]]
            pw = {
                "slot_id": p["slot_id"],
                "present": True,
                "focused": bool(p.get("focused")),
                "keypoints_blob": osc_codec.pack_keypoints(kps),
                "kp_state_blob": osc_codec.pack_kp_state(kst),
                "bbox": p["bbox_xywhn"],
                "features_blob": osc_codec.pack_features(
                    [0.0 if v is None else v for v in p["features"]]
                ),
                "feat_state_blob": osc_codec.pack_feat_state(p["feat_state"]),
            }
        bundles.append(
            osc_codec.build_person_bundle(
                stream_id=d["stream_id"],
                captured_frame_id=d["captured_frame_id"],
                bundle_seq=seq,
                n_persons=d["n_persons"],
                fps=d["fps"],
                contract_id=d["contract_id"],
                calibration_generation=d["calibration_generation"],
                calibration_state=d["calibration_state"],
                captured_at_us=d["captured_at_us"],
                processed_at_us=d["processed_at_us"],
                queued_for_send_at_us=queued_us,
                person=pw,
            )
        )
        seq += 1
    return bundles


def feed_session(
    driver: HarMoCAPDriver,
    frames: list[dict],
    *,
    t0_ms: float = 1_000_000.0,
    dt_ms: float = 33.0,
    handshake_every: int = 30,
) -> int:
    """Feed handshake + person bundles; returns next free bundle_seq."""
    seq = 0
    now = t0_ms
    last_hs_frame = -10_000
    for i, d in enumerate(frames):
        if i - last_hs_frame >= handshake_every or i == 0:
            for pkt in handshake_bytes(d):
                driver.handle_datagram(pkt, now_ms=now)
            last_hs_frame = i
        for pkt in frame_to_wire(d, seq + 1):
            seq += 1
            driver.handle_datagram(pkt, now_ms=now)
        now += dt_ms
    return seq


class FrameCollector:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict[str, tuple[float, int, float]]]] = []

    def __call__(
        self, source_id: str, channel_values: dict[str, tuple[float, int, float]]
    ) -> None:
        self.frames.append((source_id, channel_values))

    @property
    def last(self) -> dict[str, tuple[float, int, float]]:
        return self.frames[-1][1]

    def focused_slots(self, ch: dict | None = None) -> list[int]:
        ch = ch if ch is not None else self.last
        out = []
        for s in range(8):
            v, st, _ = ch[f"slot_{s}_focused"]
            if st == STATE_OBSERVED and v >= 0.5:
                out.append(s)
        return out

    def present_slots(self, ch: dict | None = None) -> list[int]:
        ch = ch if ch is not None else self.last
        out = []
        for s in range(8):
            v, st, _ = ch[f"slot_{s}_present"]
            if st != STATE_INVALID and v >= 0.5:
                out.append(s)
        return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(TWO_PERSONS.is_file(), f"missing fixture {TWO_PERSONS}")
class TestTwoPersonsFixture(unittest.TestCase):
    def test_channels_for_both_slots_and_names(self) -> None:
        frames = load_jsonl(TWO_PERSONS)
        col = FrameCollector()
        drv = HarMoCAPDriver(on_frame=col)
        feed_session(drv, frames[:5])

        self.assertGreater(len(col.frames), 0)
        source_id, ch = col.frames[-1]
        self.assertEqual(source_id, "harmocap")

        # Both occupied slots present with correct naming.
        for slot in (0, 1):
            self.assertIn(f"slot_{slot}_present", ch)
            self.assertIn(f"slot_{slot}_focused", ch)
            self.assertEqual(ch[f"slot_{slot}_present"][0], 1.0)
            self.assertEqual(ch[f"slot_{slot}_present"][1], STATE_OBSERVED)
            self.assertIn(f"slot_{slot}_keypoint_left_wrist_x", ch)
            self.assertIn(f"slot_{slot}_keypoint_left_wrist_y", ch)
            self.assertIn(f"slot_{slot}_keypoint_left_wrist_conf", ch)
            self.assertIn(f"slot_{slot}_qom", ch)
            self.assertIn(f"slot_{slot}_laban_weight_proxy", ch)
            self.assertIn(f"slot_{slot}_kinetic_energy", ch)
            # kinetic_energy aliases laban_weight_proxy
            self.assertEqual(
                ch[f"slot_{slot}_kinetic_energy"],
                ch[f"slot_{slot}_laban_weight_proxy"],
            )

        # Registry addressing helper matches CORE_DESIGN §2 style.
        self.assertEqual(
            registry_address("slot_3_keypoint_left_wrist_x"),
            "harmocap.slot_3_keypoint_left_wrist_x",
        )
        self.assertEqual(
            registry_address("slot_3_kinetic_energy"),
            "harmocap.slot_3_kinetic_energy",
        )

        # Catalogue covers 8 slots × (present/focused + 17*3 + 21 + kinetic alias).
        catalogue = channel_names()
        per_slot = 2 + 17 * 3 + 21 + 1
        self.assertEqual(len(catalogue), 8 * per_slot)
        for name in catalogue:
            self.assertIn(name, ch)

        # Wrist coords are finite observed values for present people.
        x, st, conf = ch["slot_0_keypoint_left_wrist_x"]
        self.assertEqual(st, STATE_OBSERVED)
        self.assertGreater(conf, 0.0)
        self.assertTrue(0.0 <= x <= 3.0)

    def test_focus_migration_mid_stream(self) -> None:
        frames = load_jsonl(TWO_PERSONS)
        # Fixture: focus on slot 0 until frame_id 120, then slot 1 from 121.
        col = FrameCollector()
        drv = HarMoCAPDriver(on_frame=col)
        feed_session(drv, frames)

        # After early frames, focus is on slot 0.
        # Find a snapshot where only slot 0 is focused, then one with slot 1.
        saw_focus_0 = False
        saw_focus_1 = False
        focus_history: list[tuple[int, ...]] = []
        for _sid, ch in col.frames:
            foc = tuple(col.focused_slots(ch))
            if foc and (not focus_history or foc != focus_history[-1]):
                focus_history.append(foc)
            if foc == (0,):
                saw_focus_0 = True
            if foc == (1,):
                saw_focus_1 = True

        self.assertTrue(saw_focus_0, f"never saw focus on 0; history={focus_history}")
        self.assertTrue(saw_focus_1, f"never saw focus on 1; history={focus_history}")
        # Final focus is slot 1.
        self.assertEqual(col.focused_slots(), [1])
        self.assertEqual(col.last["slot_0_focused"][0], 0.0)
        self.assertEqual(col.last["slot_1_focused"][0], 1.0)

    def test_gating_without_handshake(self) -> None:
        frames = load_jsonl(TWO_PERSONS)
        col = FrameCollector()
        drv = HarMoCAPDriver(on_frame=col)
        # Person bundles only — no hello/calibration.
        for pkt in frame_to_wire(frames[0], 1):
            drv.handle_datagram(pkt, now_ms=1_000.0)
        self.assertEqual(len(col.frames), 0)
        self.assertGreater(drv.stats.gated, 0)


@unittest.skipUnless(STREAM_RESTART.is_file(), f"missing fixture {STREAM_RESTART}")
class TestStreamRestart(unittest.TestCase):
    def test_new_stream_id_resets_counters(self) -> None:
        frames = load_jsonl(STREAM_RESTART)
        streams = {f["stream_id"] for f in frames}
        self.assertEqual(len(streams), 2)

        col = FrameCollector()
        drv = HarMoCAPDriver(on_frame=col)

        # Split session at stream change.
        first_stream = frames[0]["stream_id"]
        part_a = [f for f in frames if f["stream_id"] == first_stream]
        part_b = [f for f in frames if f["stream_id"] != first_stream]
        self.assertTrue(part_a and part_b)

        seq = feed_session(drv, part_a, t0_ms=2_000_000.0)
        resets_after_a = drv.stats.stream_resets
        last_seq_a = drv.last_seq
        self.assertGreater(last_seq_a, 0)

        # Feed second stream — must reset last_seq and accept low seq numbers.
        feed_session(drv, part_b, t0_ms=3_000_000.0)
        self.assertGreater(drv.stats.stream_resets, resets_after_a)
        self.assertEqual(drv.stream_id, part_b[0]["stream_id"])
        # After reset, counters advance from the new stream only.
        self.assertGreater(drv.last_seq, 0)
        self.assertGreater(len(col.frames), 0)
        self.assertIn(1.0, [col.last["slot_0_present"][0]])

    def test_stale_bundle_seq_discarded(self) -> None:
        frames = load_jsonl(TWO_PERSONS if TWO_PERSONS.is_file() else STREAM_RESTART)
        col = FrameCollector()
        drv = HarMoCAPDriver(on_frame=col)
        d = frames[0]
        for pkt in handshake_bytes(d):
            drv.handle_datagram(pkt, now_ms=100.0)
        for pkt in frame_to_wire(d, 5):
            drv.handle_datagram(pkt, now_ms=110.0)
        n = len(col.frames)
        last_seq = drv.last_seq
        # Re-inject older seq → discarded.
        for pkt in frame_to_wire(d, 1):
            drv.handle_datagram(pkt, now_ms=120.0)
        self.assertEqual(drv.last_seq, last_seq)
        self.assertGreater(drv.stats.dropped_old, 0)
        self.assertEqual(len(col.frames), n)


@unittest.skipUnless(LIFECYCLE.is_file(), f"missing fixture {LIFECYCLE}")
class TestHeldInvalidAndLease(unittest.TestCase):
    def test_held_and_invalid_states_propagate(self) -> None:
        frames = load_jsonl(LIFECYCLE)
        # Find a frame that already carries held/invalid on the wire.
        target = None
        for fr in frames:
            for p in fr["persons"]:
                if not p.get("present"):
                    continue
                kp_states = {s[0] for s in p["kp_state"]}
                feat_states = set(p["feat_state"])
                if (STATE_HELD in kp_states or STATE_HELD in feat_states) and (
                    STATE_INVALID in kp_states or STATE_INVALID in feat_states
                ):
                    target = fr
                    break
            if target:
                break

        col = FrameCollector()
        drv = HarMoCAPDriver(on_frame=col)

        if target is None:
            # Synthesize held/invalid on top of the first present frame.
            base = next(f for f in frames if f["persons"][0].get("present"))
            d = json.loads(json.dumps(base))
            p = d["persons"][0]
            p["kp_state"][9] = [STATE_HELD, 3, 100_000]  # left_wrist
            p["kp_state"][10] = [STATE_INVALID, 0, 0]  # right_wrist
            p["feat_state"][0] = STATE_HELD  # qom
            p["feat_state"][1] = STATE_INVALID  # contraction
            p["features"][1] = 0.0
            target = d
            for pkt in handshake_bytes(target):
                drv.handle_datagram(pkt, now_ms=500.0)
            for pkt in frame_to_wire(target, 1):
                drv.handle_datagram(pkt, now_ms=510.0)
        else:
            # Replay up to and including target so handshake generations match.
            idx = frames.index(target)
            feed_session(drv, frames[: idx + 1])

        ch = col.last
        # Held keypoint keeps coordinates with held state.
        _x, st_lw, conf_lw = ch["slot_0_keypoint_left_wrist_x"]
        self.assertEqual(st_lw, STATE_HELD, "left_wrist should be held")
        self.assertGreaterEqual(conf_lw, 0.0)

        # Invalid keypoint → invalid channels, sentinel value ignored by consumers.
        vx, st_rw, conf_rw = ch["slot_0_keypoint_right_wrist_x"]
        self.assertEqual(st_rw, STATE_INVALID)
        self.assertEqual(vx, 0.0)
        self.assertEqual(conf_rw, 0.0)

        # Feature states.
        _q, st_q, conf_q = ch["slot_0_qom"]
        self.assertEqual(st_q, STATE_HELD)
        self.assertLess(conf_q, 1.0)

        cv, st_c, conf_c = ch["slot_0_contraction"]
        self.assertEqual(st_c, STATE_INVALID)
        self.assertEqual(cv, 0.0)
        self.assertEqual(conf_c, 0.0)

    def test_lease_expiry_marks_channels_invalid(self) -> None:
        frames = load_jsonl(LIFECYCLE)
        present = next(f for f in frames if any(p.get("present") for p in f["persons"]))
        col = FrameCollector()
        lease_ms = 2000.0
        drv = HarMoCAPDriver(on_frame=col, lease_ms=lease_ms)

        t0 = 10_000.0
        for pkt in handshake_bytes(present):
            drv.handle_datagram(pkt, now_ms=t0)
        for pkt in frame_to_wire(present, 1):
            drv.handle_datagram(pkt, now_ms=t0 + 10)
        self.assertIn(0, col.present_slots())

        # Just inside lease — still present.
        drv.tick(now_ms=t0 + 10 + lease_ms - 1)
        self.assertIn(0, col.present_slots())

        # Past lease — slot becomes absent, channels invalid.
        drv.tick(now_ms=t0 + 10 + lease_ms + 1)
        self.assertNotIn(0, col.present_slots())
        ch = col.last
        self.assertEqual(ch["slot_0_present"][0], 0.0)
        self.assertEqual(ch["slot_0_keypoint_nose_x"][1], STATE_INVALID)
        self.assertEqual(ch["slot_0_qom"][1], STATE_INVALID)
        self.assertGreaterEqual(drv.stats.lease_expiries, 1)

    def test_tombstone_clears_slot(self) -> None:
        frames = load_jsonl(LIFECYCLE)
        # Prefer an explicit present=0 frame from the fixture tail.
        tomb = None
        for fr in frames:
            if any(p.get("present") is False for p in fr["persons"]):
                tomb = fr
        self.assertIsNotNone(tomb)
        assert tomb is not None

        col = FrameCollector()
        drv = HarMoCAPDriver(on_frame=col)
        # Feed some live frames then tombstones.
        live = [f for f in frames if f["captured_frame_id"] < tomb["captured_frame_id"]]
        feed_session(drv, live[-5:] + [tomb])

        self.assertEqual(col.last["slot_0_present"][0], 0.0)
        self.assertEqual(col.last["slot_0_keypoint_left_wrist_x"][1], STATE_INVALID)
        self.assertGreaterEqual(drv.stats.tombstones, 1)


class TestSyntheticHeldInvalid(unittest.TestCase):
    """Held/invalid without relying on fixture state content."""

    @unittest.skipUnless(TWO_PERSONS.is_file(), "need two_persons for base frame")
    def test_mixed_states_on_synthetic_person(self) -> None:
        base = load_jsonl(TWO_PERSONS)[0]
        d = json.loads(json.dumps(base))
        p0 = d["persons"][0]
        # left_wrist held, right_wrist invalid; qom held, contraction invalid
        p0["kp_state"][9] = [STATE_HELD, 5, 50_000]
        p0["kp_state"][10] = [STATE_INVALID, 0, 0]
        p0["keypoints"][10] = [0.0, 0.0, 0.0]
        p0["feat_state"][0] = STATE_HELD
        p0["feat_state"][1] = STATE_INVALID
        p0["features"][1] = 0.0

        col = FrameCollector()
        drv = HarMoCAPDriver(on_frame=col)
        for pkt in handshake_bytes(d):
            drv.handle_datagram(pkt, now_ms=1.0)
        for pkt in frame_to_wire(d, 1):
            drv.handle_datagram(pkt, now_ms=2.0)

        ch = col.last
        self.assertEqual(ch["slot_0_keypoint_left_wrist_x"][1], STATE_HELD)
        self.assertEqual(ch["slot_0_keypoint_right_wrist_x"][1], STATE_INVALID)
        self.assertEqual(ch["slot_0_qom"][1], STATE_HELD)
        self.assertEqual(ch["slot_0_contraction"][1], STATE_INVALID)
        self.assertEqual(ch["slot_0_contraction"][0], 0.0)
        # Second person still observed.
        self.assertEqual(ch["slot_1_present"][0], 1.0)
        self.assertEqual(ch["slot_1_keypoint_nose_x"][1], STATE_OBSERVED)


class TestMonotonicAcrossStreamReset(unittest.TestCase):
    @unittest.skipUnless(TWO_PERSONS.is_file(), "need two_persons")
    def test_stale_stream_id_resets_then_accepts(self) -> None:
        frames = load_jsonl(TWO_PERSONS)
        col = FrameCollector()
        drv = HarMoCAPDriver(on_frame=col)

        d0 = frames[0]
        for pkt in handshake_bytes(d0):
            drv.handle_datagram(pkt, now_ms=100.0)
        for pkt in frame_to_wire(d0, 10):
            drv.handle_datagram(pkt, now_ms=110.0)
        self.assertEqual(drv.last_seq, 10 + d0["n_persons"] - 1)
        resets = drv.stats.stream_resets

        # New stream with a lower seq must be accepted after reset.
        d1 = json.loads(json.dumps(d0))
        d1["stream_id"] = "deadbeefcafebabe"
        for pkt in handshake_bytes(d1):
            drv.handle_datagram(pkt, now_ms=200.0)
        self.assertGreater(drv.stats.stream_resets, resets)
        self.assertEqual(drv.last_seq, -1)  # reset pending first frame

        for pkt in frame_to_wire(d1, 1):
            drv.handle_datagram(pkt, now_ms=210.0)
        self.assertEqual(drv.stream_id, "deadbeefcafebabe")
        self.assertEqual(drv.last_seq, d1["n_persons"])
        self.assertGreater(len(col.frames), 0)
        self.assertEqual(col.last["slot_0_present"][0], 1.0)


if __name__ == "__main__":
    unittest.main()
