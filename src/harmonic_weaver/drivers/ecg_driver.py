"""ECG source driver for harmonic-weaver (rhythmic trigger).

Receives the ESP32 + AD8232 chest-sensor stream (OSC UDP :5001), runs
Pan-Tompkins R-peak detection, and emits Source Frame v1 channel values via
``on_frame(source_id, channel_values)``.

OSC input scheme (from cymatic-control firmware ``ecg_esp32.ino`` / ``hr_relay``):
  listen port ............... 5001 (config ``hr_relay.ecg_listen_port``)
  ``/ecg/raw`` .............. N int32 ADC samples (firmware uses batches of 8
                              at 250 Hz; 12-bit range 0–4095)
  ``/ecg/leads_off`` ........ one int32: 0 = contacts OK, 1 = leads off

Channels (registry addresses = ``source_id.channel``):
  beat ............ rising-edge trigger (1.0 on R-peak frame, else 0.0);
                    never queued; dropped (not held) when source held/invalid
  bpm ............. continuous smoothed heart rate; observed on beat, held
                    briefly between beats with decaying confidence
  signal_quality .. 0–1 estimate from lead-off + recent SNR / beat recency

Trigger semantics (CORE_DESIGN §3): rising-edge ECG routes discard invalid/held
triggers and never queue events. This driver never emits ``beat=1`` while the
source is held or invalid, and never buffers undetected beats for later flush.

Vendored processor: minimum of cymatic-control ``ecg_analysis.ECGProcessor``
(Pan-Tompkins pipeline) so harmonic-weaver stays free of a cymatic-control
package dependency. Requires numpy and scipy at runtime for detection.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy.signal import butter, find_peaks, sosfilt

# ─────────────────────────────────────────────────────────────────────────────
# Public constants
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SOURCE_ID = "ecg"
DEFAULT_LISTEN_PORT = 5001
DEFAULT_SAMPLE_RATE = 250
DEFAULT_LEASE_MS = 2000.0
DEFAULT_BPM_HOLD_MS = 2000.0
DEFAULT_BPM_SMOOTH = 0.35  # EMA toward newly observed beat BPM

OSC_ADDR_RAW = "/ecg/raw"
OSC_ADDR_LEADS_OFF = "/ecg/leads_off"

CHANNEL_BEAT = "beat"
CHANNEL_BPM = "bpm"
CHANNEL_SIGNAL_QUALITY = "signal_quality"
CHANNEL_NAMES: Tuple[str, ...] = (
    CHANNEL_BEAT,
    CHANNEL_BPM,
    CHANNEL_SIGNAL_QUALITY,
)

STATE_OBSERVED = "observed"
STATE_HELD = "held"
STATE_INVALID = "invalid"

# Full registry addresses when source_id is the default.
REG_BEAT = f"{DEFAULT_SOURCE_ID}.{CHANNEL_BEAT}"
REG_BPM = f"{DEFAULT_SOURCE_ID}.{CHANNEL_BPM}"
REG_SIGNAL_QUALITY = f"{DEFAULT_SOURCE_ID}.{CHANNEL_SIGNAL_QUALITY}"


# ─────────────────────────────────────────────────────────────────────────────
# Vendored from cymatic-control/ecg_analysis.py (Pan-Tompkins R-peak detector)
# Source: ~/Projects/cymatic-control/ecg_analysis.py — wrap, don't rewrite.
# Only additive: last_snr for signal_quality exposure.
# ─────────────────────────────────────────────────────────────────────────────

ECG_SAMPLE_RATE = 250
ECG_BUFFER_SECONDS = 4
ECG_BUFFER_SIZE = ECG_SAMPLE_RATE * ECG_BUFFER_SECONDS

REFRACTORY_MS = 300
INTEGRATION_WINDOW_MS = 150
DETECTION_INTERVAL = 0.25

BPM_MIN = 30.0
BPM_MAX = 200.0


def design_ecg_bandpass(fs=ECG_SAMPLE_RATE, lowcut=5.0, highcut=15.0, order=2):
    """Butterworth bandpass for QRS complex isolation (Pan-Tompkins)."""
    nyq = fs / 2.0
    sos = butter(order, [lowcut / nyq, highcut / nyq], btype="band", output="sos")
    return sos


class ECGProcessor:
    """Real-time R-peak detector for streaming ECG data.

    Vendored from cymatic-control ``ecg_analysis.ECGProcessor``. Maintains a
    ring buffer of raw ADC samples, applies the Pan-Tompkins pipeline on the
    analysis window, and returns ``(bpm, rr_ms)`` for each newly detected
    R-peak.

    Additive for weaver: ``last_snr`` is updated on each detection pass so the
    driver can expose ``signal_quality`` (0–1).
    """

    def __init__(self, sample_rate=ECG_SAMPLE_RATE, buffer_seconds=ECG_BUFFER_SECONDS):
        self.sample_rate = sample_rate
        self.buffer_size = int(sample_rate * buffer_seconds)
        self.buffer = np.zeros(self.buffer_size)
        self.write_pos = 0
        self.samples_received = 0

        self.sos = design_ecg_bandpass(fs=sample_rate)
        self._int_window = int(INTEGRATION_WINDOW_MS / 1000.0 * sample_rate)
        self._refractory_samples = int(REFRACTORY_MS / 1000.0 * sample_rate)
        self._detect_interval = int(DETECTION_INTERVAL * sample_rate)
        self._samples_since_detect = 0

        self.rr_history: Deque[float] = deque(maxlen=8)
        self._last_peak_pos = -1
        self._last_peak_abs = -self._refractory_samples
        self.leads_off = False
        self.last_snr: float = 0.0

    def _get_ordered_buffer(self):
        n = min(self.samples_received, self.buffer_size)
        wp = self.write_pos % self.buffer_size
        return np.roll(self.buffer, -wp)[:n].copy()

    def add_samples(self, samples):
        """Add a batch of raw ADC samples and detect R-peaks.

        Returns:
            List of (bpm, rr_ms) tuples for each newly detected R-peak.
        """
        if not samples:
            return []

        for s in samples:
            self.buffer[self.write_pos % self.buffer_size] = float(s)
            self.write_pos += 1
            self.samples_received += 1

        if self.leads_off:
            return []

        self._samples_since_detect += len(samples)

        if self.samples_received < self.sample_rate * 2:
            return []

        if self._samples_since_detect < self._detect_interval:
            return []
        self._samples_since_detect = 0

        return self._detect_peaks()

    def _detect_peaks(self):
        signal = self._get_ordered_buffer()
        n = len(signal)

        signal_centered = signal - np.mean(signal)
        filtered = sosfilt(self.sos, signal_centered)
        diff = np.diff(filtered, prepend=filtered[0])
        squared = diff ** 2
        kernel = np.ones(self._int_window) / self._int_window
        integrated = np.convolve(squared, kernel, mode="same")

        peak_val = np.max(integrated)
        median_val = np.median(integrated)
        floor = max(float(median_val), 1e-10)
        snr = float(peak_val) / floor
        self.last_snr = snr

        if peak_val < 5.0 * floor:
            return []
        threshold = 0.5 * peak_val

        peaks, _ = find_peaks(
            integrated,
            height=threshold,
            distance=self._refractory_samples,
            prominence=0.3 * peak_val,
        )

        if len(peaks) < 2:
            return []

        buf_start_abs = self.samples_received - n
        beats = []
        for pk in peaks:
            abs_pos = buf_start_abs + pk
            if abs_pos <= self._last_peak_abs:
                continue
            if abs_pos - self._last_peak_abs < self._refractory_samples:
                continue

            if self._last_peak_abs > 0:
                rr_samples = abs_pos - self._last_peak_abs
                rr_seconds = rr_samples / self.sample_rate
                rr_ms = rr_seconds * 1000.0
                bpm = 60.0 / rr_seconds

                if BPM_MIN <= bpm <= BPM_MAX:
                    self.rr_history.append(rr_seconds)
                    self._last_peak_abs = abs_pos
                    beats.append((bpm, rr_ms))
                elif bpm > BPM_MAX:
                    self._last_peak_abs = abs_pos
            else:
                self._last_peak_abs = abs_pos

        return beats

    def compute_bpm(self):
        """Current BPM from median of recent RR intervals."""
        if not self.rr_history:
            return 0.0
        median_rr = float(np.median(list(self.rr_history)))
        if median_rr < 0.001:
            return 0.0
        bpm = 60.0 / median_rr
        return float(np.clip(bpm, BPM_MIN, BPM_MAX))

    def set_leads_off(self, off):
        """Gate detection based on electrode contact."""
        self.leads_off = bool(off)


# ─────────────────────────────────────────────────────────────────────────────
# Channel value envelope
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChannelValue:
    """One Source Frame v1 channel payload (value, state, confidence)."""

    value: float
    state: str
    confidence: float

    def as_tuple(self) -> Tuple[float, str, float]:
        return (self.value, self.state, self.confidence)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "value": float(self.value),
            "state": self.state,
            "confidence": float(self.confidence),
        }


FrameCallback = Callable[[str, Mapping[str, ChannelValue]], None]


def _observed(value: float, confidence: float = 1.0) -> ChannelValue:
    conf = float(np.clip(confidence, 0.0, 1.0))
    return ChannelValue(value=float(value), state=STATE_OBSERVED, confidence=conf)


def _held(value: float, confidence: float) -> ChannelValue:
    conf = float(np.clip(confidence, 0.0, 1.0))
    return ChannelValue(value=float(value), state=STATE_HELD, confidence=conf)


def _invalid_cv() -> ChannelValue:
    return ChannelValue(value=0.0, state=STATE_INVALID, confidence=0.0)


# ─────────────────────────────────────────────────────────────────────────────
# ECG source driver
# ─────────────────────────────────────────────────────────────────────────────

class ECGDriver:
    """Weaver source adapter around vendored :class:`ECGProcessor`.

    Feed samples programmatically with :meth:`feed_samples` (tests / simulators)
    or start the OSC listener with :meth:`start` (ESP32 on ``:5001``).
    """

    def __init__(
        self,
        source_id: str = DEFAULT_SOURCE_ID,
        on_frame: Optional[FrameCallback] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        lease_ms: float = DEFAULT_LEASE_MS,
        bpm_hold_ms: float = DEFAULT_BPM_HOLD_MS,
        bpm_smooth: float = DEFAULT_BPM_SMOOTH,
        listen_host: str = "0.0.0.0",
        listen_port: int = DEFAULT_LISTEN_PORT,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.source_id = source_id
        self.on_frame = on_frame
        self.sample_rate = int(sample_rate)
        self.lease_ms = float(lease_ms)
        self.bpm_hold_ms = float(bpm_hold_ms)
        self.bpm_smooth = float(np.clip(bpm_smooth, 0.0, 1.0))
        self.listen_host = listen_host
        self.listen_port = int(listen_port)
        self._clock = clock or time.monotonic

        self.processor = ECGProcessor(sample_rate=self.sample_rate)

        self._lock = threading.RLock()
        self._last_sample_time: Optional[float] = None
        self._last_beat_time: Optional[float] = None
        self._smoothed_bpm: float = 0.0
        self._has_bpm: bool = False
        self._pending_beat: bool = False  # edge for current emission only
        self._leads_off: bool = False
        self._frame_count: int = 0

        self._osc_server: Any = None
        self._osc_thread: Optional[threading.Thread] = None

    # ── presence / lease ──────────────────────────────────────────────────

    def _now(self) -> float:
        return float(self._clock())

    def stream_alive(self, now: Optional[float] = None) -> bool:
        """True while raw samples arrived within the presence lease."""
        t = self._now() if now is None else float(now)
        if self._last_sample_time is None:
            return False
        age_ms = (t - self._last_sample_time) * 1000.0
        return age_ms <= self.lease_ms

    def _source_valid(self, now: float) -> bool:
        return self.stream_alive(now) and not self._leads_off

    # ── quality ───────────────────────────────────────────────────────────

    def _compute_signal_quality(self, now: float) -> float:
        if not self.stream_alive(now) or self._leads_off:
            return 0.0
        snr = float(self.processor.last_snr)
        # Map SNR (detection threshold is 5×) into 0–1 with soft saturation.
        snr_term = 0.0
        if snr > 0.0:
            snr_term = float(np.clip((snr - 1.0) / 15.0, 0.0, 1.0))
        recency = 0.0
        if self._last_beat_time is not None:
            age_ms = (now - self._last_beat_time) * 1000.0
            # Full credit for a beat within one RR at 40 BPM (~1.5 s), fade to 0 by 3 s.
            recency = float(np.clip(1.0 - age_ms / 3000.0, 0.0, 1.0))
        # Warm-up: some quality once stream is up even before first beat.
        stream_term = 0.25
        return float(np.clip(0.55 * snr_term + 0.30 * recency + stream_term, 0.0, 1.0))

    # ── channel construction ──────────────────────────────────────────────

    def _build_channels(
        self,
        now: float,
        beat_edge: bool,
    ) -> Dict[str, ChannelValue]:
        alive = self.stream_alive(now)
        valid = alive and not self._leads_off

        # --- beat (edge-triggered; never held, never queued) ---
        if not valid:
            # Drop any pending edge; invalid/held sources do not emit triggers.
            beat_cv = _invalid_cv()
        elif beat_edge:
            beat_cv = _observed(1.0, confidence=1.0)
        else:
            beat_cv = _observed(0.0, confidence=1.0)

        # --- bpm (observed on beat, held between beats) ---
        if not alive:
            bpm_cv = _invalid_cv()
        elif not self._has_bpm:
            bpm_cv = _invalid_cv()
        elif self._leads_off:
            # Leads off: hold last BPM briefly then invalid (not a trigger path).
            if self._last_beat_time is not None:
                age_ms = (now - self._last_beat_time) * 1000.0
                if age_ms <= self.bpm_hold_ms:
                    conf = max(0.0, 1.0 - age_ms / self.bpm_hold_ms)
                    bpm_cv = _held(self._smoothed_bpm, conf)
                else:
                    bpm_cv = _invalid_cv()
            else:
                bpm_cv = _invalid_cv()
        elif beat_edge:
            bpm_cv = _observed(self._smoothed_bpm, confidence=1.0)
        else:
            # Between beats: held with monotonic confidence decay.
            if self._last_beat_time is None:
                bpm_cv = _invalid_cv()
            else:
                age_ms = (now - self._last_beat_time) * 1000.0
                if age_ms <= self.bpm_hold_ms:
                    conf = max(0.0, 1.0 - age_ms / self.bpm_hold_ms)
                    # Fresh observation window: first ~50 ms stay observed.
                    if age_ms < 50.0:
                        bpm_cv = _observed(self._smoothed_bpm, confidence=1.0)
                    else:
                        bpm_cv = _held(self._smoothed_bpm, conf)
                else:
                    bpm_cv = _invalid_cv()

        # --- signal_quality ---
        if not alive:
            sq_cv = _invalid_cv()
        else:
            q = self._compute_signal_quality(now)
            if self._leads_off:
                sq_cv = _observed(0.0, confidence=1.0)
            else:
                sq_cv = _observed(q, confidence=1.0)

        return {
            CHANNEL_BEAT: beat_cv,
            CHANNEL_BPM: bpm_cv,
            CHANNEL_SIGNAL_QUALITY: sq_cv,
        }

    def _emit(self, channels: Mapping[str, ChannelValue]) -> None:
        self._frame_count += 1
        cb = self.on_frame
        if cb is not None:
            cb(self.source_id, dict(channels))

    # ── public feed API (tests / simulators) ──────────────────────────────

    def feed_samples(
        self,
        samples: Sequence[float],
        now: Optional[float] = None,
    ) -> Dict[str, ChannelValue]:
        """Ingest ADC samples, run detection, emit one source frame.

        Returns the channel map that was (or would be) passed to ``on_frame``.
        """
        t = self._now() if now is None else float(now)
        with self._lock:
            self._last_sample_time = t
            beats = self.processor.add_samples(list(samples))

            beat_edge = False
            if self._source_valid(t) and beats:
                # Never queue: only the freshest beat in this detection window
                # produces one rising edge. Extra peaks update BPM only.
                last_bpm, _rr = beats[-1]
                alpha = self.bpm_smooth
                if not self._has_bpm:
                    self._smoothed_bpm = float(last_bpm)
                    self._has_bpm = True
                else:
                    self._smoothed_bpm = (
                        (1.0 - alpha) * self._smoothed_bpm + alpha * float(last_bpm)
                    )
                # Blend toward processor median RR when available (smoother BPM).
                median_bpm = self.processor.compute_bpm()
                if median_bpm > 0.0:
                    self._smoothed_bpm = (
                        (1.0 - alpha) * self._smoothed_bpm + alpha * float(median_bpm)
                    )
                self._last_beat_time = t
                beat_edge = True
            # else: beats while invalid/leads-off are dropped — never queued

            channels = self._build_channels(t, beat_edge=beat_edge)
            self._emit(channels)
            return channels

    def feed_leads_off(self, off: bool, now: Optional[float] = None) -> Dict[str, ChannelValue]:
        """Update lead-off gate and emit a frame (no beat edge)."""
        t = self._now() if now is None else float(now)
        with self._lock:
            self._leads_off = bool(off)
            self.processor.set_leads_off(self._leads_off)
            # Receiving leads_off renews presence if stream was already up;
            # do not invent a sample timestamp when no data has ever arrived.
            if self._last_sample_time is not None:
                # Leads status alone is not a lease renewal; leave sample time.
                pass
            channels = self._build_channels(t, beat_edge=False)
            self._emit(channels)
            return channels

    def tick(self, now: Optional[float] = None) -> Dict[str, ChannelValue]:
        """Re-evaluate hold/lease without new samples; emit one frame.

        Call periodically so BPM can transition observed→held→invalid and so
        lease expiry propagates ``invalid`` on all channels after dropout.
        """
        t = self._now() if now is None else float(now)
        with self._lock:
            channels = self._build_channels(t, beat_edge=False)
            self._emit(channels)
            return channels

    def reset(self) -> None:
        """Clear detection and presence state (does not stop OSC)."""
        with self._lock:
            self.processor = ECGProcessor(sample_rate=self.sample_rate)
            self._last_sample_time = None
            self._last_beat_time = None
            self._smoothed_bpm = 0.0
            self._has_bpm = False
            self._pending_beat = False
            self._leads_off = False

    # ── OSC server (optional; requires python-osc) ────────────────────────

    def _handle_raw(self, _address: str, *args: Any) -> None:
        samples = [float(a) for a in args]
        if samples:
            self.feed_samples(samples)

    def _handle_leads_off(self, _address: str, *args: Any) -> None:
        off = bool(args[0]) if args else True
        self.feed_leads_off(off)

    def start(self) -> None:
        """Start a blocking-thread OSC UDP server on ``listen_port``."""
        try:
            from pythonosc import dispatcher as osc_dispatcher
            from pythonosc import osc_server
        except ImportError as exc:  # pragma: no cover - optional runtime dep
            raise RuntimeError(
                "python-osc is required for ECGDriver.start(); "
                "install python-osc or feed samples via feed_samples()"
            ) from exc

        with self._lock:
            if self._osc_server is not None:
                return
            disp = osc_dispatcher.Dispatcher()
            disp.map(OSC_ADDR_RAW, self._handle_raw)
            disp.map(OSC_ADDR_LEADS_OFF, self._handle_leads_off)
            server = osc_server.ThreadingOSCUDPServer(
                (self.listen_host, self.listen_port), disp
            )
            self._osc_server = server
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"ecg-osc-{self.listen_port}",
                daemon=True,
            )
            self._osc_thread = thread
            thread.start()

    def stop(self) -> None:
        """Stop the OSC server if running."""
        with self._lock:
            server = self._osc_server
            self._osc_server = None
            self._osc_thread = None
        if server is not None:
            server.shutdown()
            server.server_close()

    def __enter__(self) -> "ECGDriver":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic ECG helper (adapted from cymatic-control tests for driver tests)
# ─────────────────────────────────────────────────────────────────────────────

def make_synthetic_ecg(
    bpm: float = 72.0,
    duration_s: float = 6.0,
    sample_rate: int = ECG_SAMPLE_RATE,
    baseline: float = 2048.0,
    qrs_amplitude: float = 800.0,
    noise_level: float = 10.0,
    seed: Optional[int] = 0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Generate synthetic ECG-like signal with R-peaks at a known BPM.

    Returns ``(signal, peak_times_seconds)``. Adapted from
    cymatic-control ``tests/test_ecg_analysis.make_synthetic_ecg``.
    """
    rng = np.random.default_rng(seed)
    n = int(duration_s * sample_rate)
    t = np.arange(n) / sample_rate
    signal = baseline + 50.0 * np.sin(2.0 * np.pi * 0.3 * t)
    interval = 60.0 / bpm
    peak_times = np.arange(0.5, duration_s - 0.2, interval)
    for pt in peak_times:
        signal += qrs_amplitude * np.exp(-0.5 * ((t - pt) / 0.006) ** 2)
    if noise_level > 0:
        signal = signal + rng.normal(0.0, noise_level, n)
    return signal.astype(np.float64), peak_times


def feed_signal(
    driver: ECGDriver,
    signal: Iterable[float],
    batch_size: int = 8,
    sample_rate: Optional[int] = None,
    t0: float = 0.0,
) -> List[Dict[str, ChannelValue]]:
    """Feed a full signal into the driver in OSC-sized batches.

    Advances a virtual clock by ``batch_size / sample_rate`` seconds per batch
    so lease logic can be tested without wall-clock waits.
    """
    sr = int(sample_rate if sample_rate is not None else driver.sample_rate)
    arr = np.asarray(list(signal), dtype=np.float64)
    frames: List[Dict[str, ChannelValue]] = []
    t = float(t0)
    for i in range(0, len(arr), batch_size):
        batch = arr[i : i + batch_size]
        frames.append(driver.feed_samples(batch.tolist(), now=t))
        t += len(batch) / sr
    return frames
