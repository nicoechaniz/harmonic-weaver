"""MIDI source driver for Harmonic Weaver.

Generalizes cymatic-control's ``midi_relay.py`` (one CC relayed to OSC) into a
hot-plug tolerant Source Frame v1 producer: CC and note-velocity events from a
MIDI controller become normalized 0-1 channels, emitted through the standard
driver seam::

    on_frame(source_id, channel_values)

``channel_values`` maps channel name -> ``(value, state, confidence)`` where
``state`` is a Source Frame v1 states_enum name (``"observed"``, ``"held"`` or
``"invalid"``; wire codes 0/1/2).  Invalid channels always carry the 0.0
sentinel value with 0.0 confidence.  Frames are complete snapshots: every
known channel appears in every frame, per the Source Frame v1 completeness
rule.  Registry addresses are ``source_id.channel`` (CORE_DESIGN §2), e.g.
``midi.cc_74`` or ``midi.note_60_vel``.

Channel map (every value normalized to [0.0, 1.0]):

    ================  =========================  ============================
    channel           MIDI event                 normalization
    ================  =========================  ============================
    ``cc_<n>``        control_change n (0-127)   value / 127
    ``modwheel``      alias of cc_1              value / 127
    ``note_<n>_vel``  note_on / note_off n       velocity / 127 (0 on release)
    ================  =========================  ============================

Device conventions inherited from cymatic-control (``midi_relay.py`` and
``config.json``):

* port selection is a case-insensitive substring match
  (``--port "Launchpad Mini"``); with no explicit pattern the driver tries
  ``DEFAULT_PORT_PATTERNS`` and finally falls back to the first available
  port, which is the ``midi_port: null`` auto-detect behavior;
* CC 1 is the mod wheel (``cc_number: 1``), mirrored to the ``modwheel``
  alias channel;
* raw 0-127 controller data normalizes by division by 127.

The Arturia MiniLab 3 and the Novation Launchpad Mini are both matched by
``DEFAULT_PORT_PATTERNS``; any other class-compliant device is picked up by
auto-detect.  All 16 MIDI channels are merged (omni).  One explicit
deviation from ``midi_relay.find_port``: an explicit ``port_pattern`` that
matches nothing leaves the driver absent instead of binding an arbitrary
port, so a misconfigured show file cannot silently drive the wrong device.

Hot-plug contract: an absent or removed device marks every known channel
``invalid`` and never raises; a (re)connected device flips channels back to
``observed`` as its events arrive.  ``held`` is not produced: MIDI controls
are absolute, so while the device is connected the last reported position is
the current one.

``mido`` + ``python-rtmidi`` are imported lazily inside :class:`MidoBackend`.
Without them the driver still runs and reports every channel ``invalid``.
Tests inject a fake backend and never require mido or real hardware.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Iterable, Optional, Protocol

# Source Frame v1 states_enum names (wire codes 0/1/2 respectively).
STATE_OBSERVED = "observed"
STATE_HELD = "held"
STATE_INVALID = "invalid"

# Short aliases retained for convenient comparisons at call sites.
OBSERVED = STATE_OBSERVED
HELD = STATE_HELD
INVALID = STATE_INVALID

#: Channel value tuple: (value, state, confidence).
ChannelValue = tuple[float, str, float]

#: Driver seam: full channel snapshot per frame (Source Frame v1 completeness).
FrameCallback = Callable[[str, dict[str, ChannelValue]], None]

#: Known-device port name substrings, tried in order before auto-detect.
DEFAULT_PORT_PATTERNS = ("minilab 3", "minilab3", "launchpad mini")

#: CC 1 is the mod wheel (cymatic-control config.json: midi_relay.cc_number).
MODWHEEL_CC = 1

_INVALID_TUPLE: ChannelValue = (0.0, INVALID, 0.0)
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class MidiBackend(Protocol):
    """Port enumeration/opening interface (subset of mido's module API)."""

    def available(self) -> bool:
        """Return True when the backend can talk to real MIDI ports."""
        ...

    def input_names(self) -> list[str]:
        """Return the currently present MIDI input port names."""
        ...

    def open_input(self, name: str) -> Any:
        """Open an input port supporting ``iter_pending()`` and ``close()``."""
        ...


class MidoBackend:
    """Backend over real ports via mido + python-rtmidi, imported lazily.

    When mido is missing the backend simply reports itself unavailable; the
    driver degrades to all-invalid channels instead of raising.
    """

    def __init__(self) -> None:
        self._rtmidi_backend: Any = None

    def _load(self) -> Any:
        """Return mido's python-rtmidi backend, importing both lazily."""

        if self._rtmidi_backend is None:
            import mido
            import rtmidi  # noqa: F401  # provided by python-rtmidi

            self._rtmidi_backend = mido.Backend("mido.backends.rtmidi")
        return self._rtmidi_backend

    def available(self) -> bool:
        try:
            self._load()
        except ImportError:
            return False
        return True

    def input_names(self) -> list[str]:
        return list(self._load().get_input_names())

    def open_input(self, name: str) -> Any:
        return self._load().open_input(name)


def _coerce_int(value: Any, low: int, high: int) -> Optional[int]:
    """Return an integral value in range, or None for malformed MIDI data."""

    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number != value or not low <= number <= high:
        return None
    return number


class MidiSourceDriver:
    """Hot-plug tolerant MIDI controller -> Source Frame v1 channel driver.

    Parameters
    ----------
    source_id:
        Registry source id; channel registry addresses are
        ``source_id.channel``.  Must match ``[a-z][a-z0-9_-]*``.
    on_frame:
        ``on_frame(source_id, channel_values)`` callback, the same seam as
        the other drivers.  May be None (frames are then only kept in
        :attr:`last_frame`).
    backend:
        Port provider; defaults to :class:`MidoBackend`.  Tests inject a
        fake backend instead of mido virtual ports.
    port_patterns:
        Case-insensitive device name substrings tried in order.
    port_pattern:
        Explicit substring override (midi_relay's ``--port``).  When given,
        only a matching port is opened; no match means the driver stays
        absent (deliberate deviation from midi_relay's fallthrough).
    declared_ccs / declared_notes:
        Controllers/notes known from boot, so their channels exist (invalid)
        before the first event.  Defaults to CC 1 + ``modwheel``, the
        cymatic-control convention.
    poll_interval_s:
        Sleep between scan/drain cycles in :meth:`start`'s thread.
    rescan_interval_s:
        Minimum seconds between port enumerations (hot-plug detection).
        ``0.0`` rescans every cycle; tests use this.
    """

    def __init__(
        self,
        source_id: str = "midi",
        on_frame: Optional[FrameCallback] = None,
        backend: Optional[MidiBackend] = None,
        port_patterns: Iterable[str] = DEFAULT_PORT_PATTERNS,
        port_pattern: Optional[str] = None,
        declared_ccs: Iterable[int] = (MODWHEEL_CC,),
        declared_notes: Iterable[int] = (),
        poll_interval_s: float = 0.01,
        rescan_interval_s: float = 1.0,
    ) -> None:
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise ValueError(f"invalid source_id {source_id!r}")
        self.source_id = source_id
        self._on_frame = on_frame
        self._backend: MidiBackend = backend if backend is not None else MidoBackend()
        self._port_patterns = tuple(port_patterns)
        self._port_pattern = port_pattern
        self._poll_interval_s = poll_interval_s
        self._rescan_interval_s = rescan_interval_s

        self._channels: dict[str, ChannelValue] = {}
        for control in sorted({self._check_number(c, "CC") for c in declared_ccs}):
            self._channels[f"cc_{control}"] = _INVALID_TUPLE
            if control == MODWHEEL_CC:
                self._channels["modwheel"] = _INVALID_TUPLE
        for note in sorted({self._check_number(n, "note") for n in declared_notes}):
            self._channels[f"note_{note}_vel"] = _INVALID_TUPLE

        self._port: Any = None
        self._port_name: Optional[str] = None
        self._dirty = True  # emit the initial (all-invalid) frame once
        self._next_rescan = 0.0
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[BaseException] = None
        self.last_frame: Optional[dict[str, ChannelValue]] = None

    @staticmethod
    def _check_number(value: int, kind: str) -> int:
        number = _coerce_int(value, 0, 127)
        if number is None or number != value:
            raise ValueError(f"{kind} number must be an int in 0..127: {value!r}")
        return number

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True while a device port is open."""

        return self._port is not None

    @property
    def port_name(self) -> Optional[str]:
        """Name of the open port, or None while the device is absent."""

        return self._port_name

    def available_ports(self) -> list[str]:
        """Return present MIDI input port names ([] when mido is missing)."""

        try:
            if not self._backend.available():
                return []
            return self._backend.input_names()
        except Exception as exc:  # backend can fail while hot-plugging
            self.last_error = exc
            return []

    def snapshot(self) -> dict[str, ChannelValue]:
        """Return a copy of the current channel values."""

        with self._lock:
            return dict(self._channels)

    # ------------------------------------------------------------------
    # Driving loop
    # ------------------------------------------------------------------

    def poll_once(self) -> bool:
        """Run one scan/drain cycle; return True when a frame was emitted.

        Synchronous and side-effect free apart from port I/O, so tests can
        drive it directly without the background thread.
        """

        with self._lock:
            self._rescan_if_due()
            if self._port is not None:
                self._drain_port()
            if self._dirty:
                self._dirty = False
                self._emit_locked()
                return True
            return False

    def feed_message(self, message: Any) -> bool:
        """Apply one mido-style message and emit immediately if it changed.

        This is the push-style entry point (e.g. a mido virtual-port or
        callback wiring); :meth:`poll_once` is the pull-style one.  Returns
        True when a frame was emitted.
        """

        with self._lock:
            self._apply_message(message)
            if self._dirty:
                self._dirty = False
                self._emit_locked()
                return True
            return False

    def start(self) -> None:
        """Start the background poll thread (idempotent)."""

        with self._lock:
            if self._thread is not None:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=f"{self.source_id}-midi-driver",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the background thread and close the device port."""

        with self._lock:
            thread, self._thread = self._thread, None
        self._stop_event.set()
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            self._close_port_locked()

    def close(self) -> None:
        """Alias for :meth:`stop`."""

        self.stop()

    def __enter__(self) -> "MidiSourceDriver":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # the driver must never die
                self.last_error = exc
            self._stop_event.wait(self._poll_interval_s)

    # ------------------------------------------------------------------
    # Hot-plug state machine (all called with self._lock held)
    # ------------------------------------------------------------------

    def _rescan_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_rescan:
            return
        self._next_rescan = now + self._rescan_interval_s

        try:
            if not self._backend.available():
                self._disconnect_locked()
                return
            names = self._backend.input_names()
        except Exception as exc:  # backend can fail while hot-plugging
            self.last_error = exc
            self._disconnect_locked()
            return

        if self._port is not None:
            if self._port_name in names:
                return  # still present; keep it
            self._disconnect_locked()

        if self._port is None:
            selected = self._select_port(names)
            if selected is None:
                return
            try:
                port = self._backend.open_input(selected)
            except Exception as exc:  # device vanished between scan and open
                self.last_error = exc
                return
            if port is None:
                self.last_error = RuntimeError(
                    f"MIDI backend returned no port for {selected!r}"
                )
                return
            self._port = port
            self._port_name = selected
            self.last_error = None

    def _select_port(self, names: list[str]) -> Optional[str]:
        if not names:
            return None
        if self._port_pattern is not None:
            pattern = self._port_pattern.lower()
            for name in names:
                if pattern in name.lower():
                    return name
            return None  # explicit pattern: never bind an arbitrary port
        for pattern in self._port_patterns:
            lowered = pattern.lower()
            for name in names:
                if lowered in name.lower():
                    return name
        return names[0]  # midi_relay auto-detect: first available port

    def _drain_port(self) -> None:
        try:
            pending = list(self._port.iter_pending())
        except Exception as exc:  # unplugged mid-poll, rtmidi error, ...
            self.last_error = exc
            self._disconnect_locked()
            return
        for message in pending:
            self._apply_message(message)

    def _close_port_locked(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None
            self._port_name = None

    def _disconnect_locked(self) -> None:
        """Device absent: close the port and invalidate every known channel."""

        self._close_port_locked()
        for name, (_, state, _) in self._channels.items():
            if state != INVALID:
                self._channels[name] = _INVALID_TUPLE
                self._dirty = True

    # ------------------------------------------------------------------
    # Message -> channel mapping
    # ------------------------------------------------------------------

    def _apply_message(self, message: Any) -> None:
        """Map one mido-style message (``type`` + fields) onto channels.

        Works with real ``mido.Message`` objects and with any attribute
        look-alike; unknown types and malformed fields are ignored.
        """

        mtype = getattr(message, "type", None)
        if mtype == "control_change":
            control = _coerce_int(getattr(message, "control", None), 0, 127)
            value = _coerce_int(getattr(message, "value", None), 0, 127)
            if control is None or value is None:
                return
            self._set_channel(f"cc_{control}", value / 127.0)
            if control == MODWHEEL_CC:
                self._set_channel("modwheel", value / 127.0)
        elif mtype == "note_on":
            note = _coerce_int(getattr(message, "note", None), 0, 127)
            velocity = _coerce_int(getattr(message, "velocity", None), 0, 127)
            if note is None or velocity is None:
                return
            # velocity 0 is the running-status note off convention
            self._set_channel(f"note_{note}_vel", velocity / 127.0)
        elif mtype == "note_off":
            note = _coerce_int(getattr(message, "note", None), 0, 127)
            if note is None:
                return
            self._set_channel(f"note_{note}_vel", 0.0)
        # clock, sysex, program_change, ... are not source channels: ignore

    def _set_channel(self, name: str, value: float) -> None:
        value = min(1.0, max(0.0, float(value)))
        updated: ChannelValue = (value, OBSERVED, 1.0)
        if self._channels.get(name) != updated:
            self._channels[name] = updated
            self._dirty = True

    def _emit_locked(self) -> None:
        frame = dict(self._channels)
        self.last_frame = frame
        if self._on_frame is not None:
            self._on_frame(self.source_id, frame)


__all__ = [
    "ChannelValue",
    "DEFAULT_PORT_PATTERNS",
    "FrameCallback",
    "HELD",
    "INVALID",
    "MODWHEEL_CC",
    "MidiBackend",
    "MidiSourceDriver",
    "MidoBackend",
    "OBSERVED",
    "STATE_HELD",
    "STATE_INVALID",
    "STATE_OBSERVED",
]

# Sibling drivers use acronym-style class names.  Keep the descriptive name as
# the implementation and expose the conventional spelling as an alias.
MIDIDriver = MidiSourceDriver
MidiDriver = MidiSourceDriver
__all__.extend(["MIDIDriver", "MidiDriver"])
