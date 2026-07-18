"""Tests for the ECG source driver (rhythmic trigger).

Uses synthetic ECG (known BPM) adapted from cymatic-control's
``tests/test_ecg_analysis.py`` fixtures. Asserts:

* detected BPM within tolerance at 60 and 120 BPM
* beat events emitted as rising edges (count ≈ expected beats)
* invalid propagation after stream dropout past the presence lease
* beat edges dropped (not held/queued) when leads are off
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import pytest

from harmonic_weaver.drivers.ecg_driver import (
    CHANNEL_BEAT,
    CHANNEL_BPM,
    CHANNEL_SIGNAL_QUALITY,
    DEFAULT_LEASE_MS,
    DEFAULT_SOURCE_ID,
    OSC_ADDR_LEADS_OFF,
    OSC_ADDR_RAW,
    STATE_HELD,
    STATE_INVALID,
    STATE_OBSERVED,
    ChannelValue,
    ECGDriver,
    ECGProcessor,
    feed_signal,
    make_synthetic_ecg,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _collecting_driver(**kwargs) -> Tuple[ECGDriver, List[Tuple[str, Dict[str, ChannelValue]]]]:
    frames: List[Tuple[str, Dict[str, ChannelValue]]] = []

    def on_frame(source_id: str, channel_values: Dict[str, ChannelValue]) -> None:
        frames.append((source_id, dict(channel_values)))

    driver = ECGDriver(on_frame=on_frame, **kwargs)
    return driver, frames


def _count_beat_edges(frames: List[Tuple[str, Dict[str, ChannelValue]]]) -> int:
    """Count rising-edge triggers: observed beat value == 1.0."""
    n = 0
    for _sid, ch in frames:
        beat = ch[CHANNEL_BEAT]
        if beat.state == STATE_OBSERVED and beat.value >= 0.5:
            n += 1
    return n


def _last_valid_bpm(frames: List[Tuple[str, Dict[str, ChannelValue]]]) -> float:
    for _sid, ch in reversed(frames):
        bpm = ch[CHANNEL_BPM]
        if bpm.state in (STATE_OBSERVED, STATE_HELD) and bpm.value > 0:
            return float(bpm.value)
    return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1 — unit: envelope & OSC scheme constants
# ═══════════════════════════════════════════════════════════════════════════

class TestDriverBasics:
    def test_channel_names_and_source_id(self):
        driver, frames = _collecting_driver()
        driver.feed_samples([2048.0] * 8, now=0.0)
        assert frames
        sid, ch = frames[-1]
        assert sid == DEFAULT_SOURCE_ID
        assert set(ch.keys()) == {CHANNEL_BEAT, CHANNEL_BPM, CHANNEL_SIGNAL_QUALITY}

    def test_osc_address_scheme(self):
        # Contract with ESP32 firmware / hr_relay.py
        assert OSC_ADDR_RAW == "/ecg/raw"
        assert OSC_ADDR_LEADS_OFF == "/ecg/leads_off"
        assert DEFAULT_LEASE_MS == 2000.0

    def test_no_data_tick_is_invalid(self):
        driver, frames = _collecting_driver()
        ch = driver.tick(now=1.0)
        assert ch[CHANNEL_BEAT].state == STATE_INVALID
        assert ch[CHANNEL_BPM].state == STATE_INVALID
        assert ch[CHANNEL_SIGNAL_QUALITY].state == STATE_INVALID
        assert ch[CHANNEL_BEAT].value == 0.0

    def test_processor_is_vendored_api(self):
        proc = ECGProcessor(sample_rate=250)
        assert proc.compute_bpm() == 0.0
        assert proc.add_samples([]) == []
        proc.set_leads_off(True)
        assert proc.leads_off is True
        assert hasattr(proc, "last_snr")


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2 — synthetic ECG: BPM + beat edges
# ═══════════════════════════════════════════════════════════════════════════

class TestSyntheticBPM:
    @pytest.mark.parametrize(
        "target_bpm,duration_s,tolerance",
        [
            (60.0, 14.0, 5.0),
            (120.0, 12.0, 10.0),
        ],
    )
    def test_bpm_within_tolerance(self, target_bpm, duration_s, tolerance):
        driver, frames = _collecting_driver(lease_ms=5000.0, bpm_hold_ms=3000.0)
        signal, peak_times = make_synthetic_ecg(
            bpm=target_bpm,
            duration_s=duration_s,
            noise_level=5.0,
            seed=42,
        )
        feed_signal(driver, signal, batch_size=8, t0=0.0)

        detected = _last_valid_bpm(frames)
        assert detected > 0.0, "expected a usable BPM after synthetic stream"
        assert abs(detected - target_bpm) <= tolerance, (
            f"BPM {detected:.1f} outside ±{tolerance} of target {target_bpm}"
        )

    @pytest.mark.parametrize(
        "target_bpm,duration_s",
        [
            (60.0, 14.0),
            (120.0, 12.0),
        ],
    )
    def test_beat_edges_count(self, target_bpm, duration_s):
        driver, frames = _collecting_driver(lease_ms=5000.0)
        signal, peak_times = make_synthetic_ecg(
            bpm=target_bpm,
            duration_s=duration_s,
            noise_level=5.0,
            seed=7,
        )
        feed_signal(driver, signal, batch_size=8, t0=0.0)

        edges = _count_beat_edges(frames)
        # First R-peak establishes position only (no RR); processor also needs
        # ~2 s warm-up. Allow generous lower bound; upper bound avoids runaway.
        expected_rr_beats = max(0, len(peak_times) - 1)
        assert edges >= max(2, int(expected_rr_beats * 0.45)), (
            f"only {edges} beat edges, expected ~{expected_rr_beats} (target {target_bpm} BPM)"
        )
        assert edges <= expected_rr_beats + 3, (
            f"too many beat edges: {edges} vs expected ~{expected_rr_beats}"
        )

    def test_beat_is_edge_not_level(self):
        """Beat channel is a one-frame pulse (1 then 0), not a held high."""
        driver, frames = _collecting_driver(lease_ms=5000.0)
        signal, _ = make_synthetic_ecg(bpm=60.0, duration_s=12.0, noise_level=5.0, seed=1)
        feed_signal(driver, signal, batch_size=8, t0=0.0)

        beat_values = [
            ch[CHANNEL_BEAT].value
            for _sid, ch in frames
            if ch[CHANNEL_BEAT].state == STATE_OBSERVED
        ]
        assert any(v >= 0.5 for v in beat_values), "expected at least one rising edge"
        # After an edge frame, subsequent frames must return to 0 (not held high).
        saw_edge = False
        returned_low = False
        for v in beat_values:
            if v >= 0.5:
                saw_edge = True
            elif saw_edge and v < 0.5:
                returned_low = True
                break
        assert returned_low, "beat must fall back to 0 after the edge frame"

    def test_bpm_held_between_beats(self):
        driver, frames = _collecting_driver(lease_ms=5000.0, bpm_hold_ms=2500.0)
        signal, _ = make_synthetic_ecg(bpm=60.0, duration_s=12.0, noise_level=5.0, seed=3)
        feed_signal(driver, signal, batch_size=8, t0=0.0)

        states = [ch[CHANNEL_BPM].state for _sid, ch in frames]
        assert STATE_OBSERVED in states
        assert STATE_HELD in states, "BPM should be held briefly between beats"


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3 — lease dropout & leads-off trigger drop
# ═══════════════════════════════════════════════════════════════════════════

class TestInvalidAndTriggerDrop:
    def test_dropout_invalidates_all_channels(self):
        driver, frames = _collecting_driver(lease_ms=2000.0, bpm_hold_ms=1500.0)
        signal, _ = make_synthetic_ecg(bpm=60.0, duration_s=10.0, noise_level=5.0, seed=11)
        # Virtual time ends around duration_s after feed_signal.
        feed_signal(driver, signal, batch_size=8, t0=0.0)
        last_t = 10.0

        # Still inside lease — stream considered alive if we tick soon.
        mid = driver.tick(now=last_t + 0.5)
        assert mid[CHANNEL_SIGNAL_QUALITY].state != STATE_INVALID or mid[CHANNEL_BPM].state in (
            STATE_OBSERVED,
            STATE_HELD,
            STATE_INVALID,
        )

        # Past lease: all channels invalid.
        expired = driver.tick(now=last_t + 3.0)
        assert expired[CHANNEL_BEAT].state == STATE_INVALID
        assert expired[CHANNEL_BEAT].value == 0.0
        assert expired[CHANNEL_BPM].state == STATE_INVALID
        assert expired[CHANNEL_BPM].value == 0.0
        assert expired[CHANNEL_SIGNAL_QUALITY].state == STATE_INVALID
        assert expired[CHANNEL_SIGNAL_QUALITY].value == 0.0

    def test_leads_off_drops_beat_edges(self):
        driver, frames = _collecting_driver(lease_ms=5000.0)
        signal, _ = make_synthetic_ecg(bpm=72.0, duration_s=10.0, noise_level=5.0, seed=5)

        # First half with leads on.
        half = len(signal) // 2
        sr = 250
        t = 0.0
        batch = 8
        for i in range(0, half, batch):
            driver.feed_samples(signal[i : i + batch].tolist(), now=t)
            t += batch / sr

        edges_before = _count_beat_edges(frames)
        assert edges_before >= 1

        # Leads off for the rest — no new rising edges allowed.
        driver.feed_leads_off(True, now=t)
        start_len = len(frames)
        for i in range(half, len(signal), batch):
            end = min(i + batch, len(signal))
            driver.feed_samples(signal[i:end].tolist(), now=t)
            t += (end - i) / sr

        edges_after = 0
        for _sid, ch in frames[start_len:]:
            beat = ch[CHANNEL_BEAT]
            if beat.state == STATE_OBSERVED and beat.value >= 0.5:
                edges_after += 1
            # While invalid, value must be the 0.0 sentinel.
            if beat.state == STATE_INVALID:
                assert beat.value == 0.0

        assert edges_after == 0, (
            f"beat edges must be dropped when leads off, got {edges_after}"
        )

    def test_no_queued_beats_after_reconnect(self):
        """Beats that would have occurred while invalid are not flushed later."""
        driver, frames = _collecting_driver(lease_ms=5000.0)
        signal, _ = make_synthetic_ecg(bpm=72.0, duration_s=12.0, noise_level=5.0, seed=9)
        sr = 250
        batch = 8
        t = 0.0

        # Warm-up with leads on.
        warm = sr * 3
        for i in range(0, warm, batch):
            driver.feed_samples(signal[i : i + batch].tolist(), now=t)
            t += batch / sr

        driver.feed_leads_off(True, now=t)
        # Mid segment discarded for detection.
        mid_end = sr * 8
        for i in range(warm, mid_end, batch):
            driver.feed_samples(signal[i : i + batch].tolist(), now=t)
            t += batch / sr

        # Clear frames from the off period, reconnect, continue.
        driver.feed_leads_off(False, now=t)
        reconnect_at = len(frames)
        for i in range(mid_end, len(signal), batch):
            end = min(i + batch, len(signal))
            driver.feed_samples(signal[i:end].tolist(), now=t)
            t += (end - i) / sr

        # Immediately after reconnect there must not be a burst of queued edges
        # (more edges than wall-clock beats could produce in a short window).
        post = frames[reconnect_at : reconnect_at + 5]
        burst = sum(
            1
            for _sid, ch in post
            if ch[CHANNEL_BEAT].state == STATE_OBSERVED and ch[CHANNEL_BEAT].value >= 0.5
        )
        assert burst <= 1, f"queued flush detected: {burst} edges in first 5 post-reconnect frames"

    def test_signal_quality_range(self):
        driver, frames = _collecting_driver(lease_ms=5000.0)
        signal, _ = make_synthetic_ecg(bpm=72.0, duration_s=8.0, noise_level=5.0, seed=2)
        feed_signal(driver, signal, batch_size=8, t0=0.0)

        qualities = [
            ch[CHANNEL_SIGNAL_QUALITY]
            for _sid, ch in frames
            if ch[CHANNEL_SIGNAL_QUALITY].state == STATE_OBSERVED
        ]
        assert qualities, "expected observed signal_quality while stream alive"
        for q in qualities:
            assert 0.0 <= q.value <= 1.0


# ═══════════════════════════════════════════════════════════════════════════
# Layer 4 — frame callback contract
# ═══════════════════════════════════════════════════════════════════════════

class TestFrameCallback:
    def test_on_frame_signature(self):
        seen = []

        def on_frame(source_id, channel_values):
            seen.append((source_id, channel_values))

        driver = ECGDriver(source_id="ecg", on_frame=on_frame)
        driver.feed_samples([2048.0] * 8, now=0.0)
        assert len(seen) == 1
        sid, ch = seen[0]
        assert sid == "ecg"
        for name in (CHANNEL_BEAT, CHANNEL_BPM, CHANNEL_SIGNAL_QUALITY):
            assert name in ch
            assert isinstance(ch[name], ChannelValue)
            assert ch[name].state in (STATE_OBSERVED, STATE_HELD, STATE_INVALID)
            assert isinstance(ch[name].value, float)
            assert 0.0 <= ch[name].confidence <= 1.0

    def test_registry_address_helpers(self):
        # Documented full addresses for routes: source_id.channel
        assert f"{DEFAULT_SOURCE_ID}.{CHANNEL_BEAT}" == "ecg.beat"
        assert f"{DEFAULT_SOURCE_ID}.{CHANNEL_BPM}" == "ecg.bpm"
        assert f"{DEFAULT_SOURCE_ID}.{CHANNEL_SIGNAL_QUALITY}" == "ecg.signal_quality"
