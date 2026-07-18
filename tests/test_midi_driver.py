"""Tests for the hot-plug tolerant MIDI source driver.

All port discovery and MIDI messages are fakes; these tests neither import a
real MIDI backend nor require attached hardware.
"""

from __future__ import annotations

import builtins
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from harmonic_weaver.drivers.midi_driver import (
    INVALID,
    OBSERVED,
    MIDIDriver,
)


@dataclass
class FakeMessage:
    type: str
    control: int | None = None
    value: int | None = None
    note: int | None = None
    velocity: int | None = None
    channel: int = 0


class FakePort:
    def __init__(self) -> None:
        self.pending: list[FakeMessage] = []
        self.closed = False
        self.poll_error: Exception | None = None

    def send(self, *messages: FakeMessage) -> None:
        self.pending.extend(messages)

    def iter_pending(self) -> list[FakeMessage]:
        if self.poll_error is not None:
            raise self.poll_error
        messages = list(self.pending)
        self.pending.clear()
        return messages

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.ports: dict[str, FakePort] = {}
        self.opened: list[str] = []
        self.is_available = True
        self.enumeration_error: Exception | None = None
        self.open_error: Exception | None = None

    def available(self) -> bool:
        return self.is_available

    def input_names(self) -> list[str]:
        if self.enumeration_error is not None:
            raise self.enumeration_error
        return list(self.names)

    def open_input(self, name: str) -> FakePort:
        if self.open_error is not None:
            raise self.open_error
        self.opened.append(name)
        return self.ports[name]

    def connect(self, name: str) -> FakePort:
        port = FakePort()
        self.ports[name] = port
        if name not in self.names:
            self.names.append(name)
        return port

    def disconnect(self, name: str) -> None:
        self.names.remove(name)


Frame = dict[str, tuple[float, str, float]]


def collecting_driver(
    backend: FakeBackend, **kwargs: Any
) -> tuple[MIDIDriver, list[tuple[str, Frame]]]:
    frames: list[tuple[str, Frame]] = []

    def on_frame(source_id: str, channels: Frame) -> None:
        frames.append((source_id, dict(channels)))

    driver = MIDIDriver(
        backend=backend,
        on_frame=on_frame,
        rescan_interval_s=0.0,
        **kwargs,
    )
    return driver, frames


def assert_invalid(channel: tuple[float, str, float]) -> None:
    assert channel == (0.0, INVALID, 0.0)


def test_module_and_default_driver_do_not_eagerly_import_midi_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even constructing the default driver must work without mido/rtmidi."""

    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "harmonic_weaver"
        / "drivers"
        / "midi_driver.py"
    )
    real_import: Callable[..., Any] = builtins.__import__
    attempted: list[str] = []

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".", 1)[0] in {"mido", "rtmidi"}:
            attempted.append(name)
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    spec = importlib.util.spec_from_file_location("lazy_midi_driver", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    driver = module.MIDIDriver()

    assert attempted == []
    assert driver.poll_once() is True
    assert attempted == ["mido"]
    assert_invalid(driver.last_frame["cc_1"])
    assert_invalid(driver.last_frame["modwheel"])


def test_absent_device_emits_complete_invalid_declared_channels() -> None:
    backend = FakeBackend()
    driver, frames = collecting_driver(
        backend,
        source_id="midi",
        declared_ccs=(1, 74),
        declared_notes=(36, 60),
    )

    assert driver.poll_once() is True
    assert driver.connected is False
    source_id, channels = frames[-1]
    assert source_id == "midi"
    assert set(channels) == {
        "cc_1",
        "cc_74",
        "modwheel",
        "note_36_vel",
        "note_60_vel",
    }
    for channel in channels.values():
        assert_invalid(channel)
    assert driver.poll_once() is False


def test_control_changes_are_normalized_and_cc1_has_modwheel_alias() -> None:
    backend = FakeBackend()
    port = backend.connect("Launchpad Mini MK3 MIDI In")
    driver, frames = collecting_driver(backend, declared_ccs=(1, 74))
    port.send(
        FakeMessage("control_change", control=1, value=64, channel=15),
        FakeMessage("control_change", control=74, value=127, channel=2),
    )

    assert driver.poll_once() is True
    channels = frames[-1][1]
    expected = 64.0 / 127.0
    assert channels["cc_1"] == pytest.approx((expected, OBSERVED, 1.0))
    assert channels["modwheel"] == pytest.approx((expected, OBSERVED, 1.0))
    assert channels["cc_74"] == (1.0, OBSERVED, 1.0)


def test_note_velocity_and_explicit_note_off_are_normalized() -> None:
    backend = FakeBackend()
    port = backend.connect("Arturia MiniLab 3 MIDI")
    driver, frames = collecting_driver(backend, declared_notes=(60,))
    port.send(FakeMessage("note_on", note=60, velocity=96, channel=1))

    driver.poll_once()
    assert frames[-1][1]["note_60_vel"] == pytest.approx(
        (96.0 / 127.0, OBSERVED, 1.0)
    )

    port.send(FakeMessage("note_off", note=60, velocity=55, channel=1))
    driver.poll_once()
    assert frames[-1][1]["note_60_vel"] == (0.0, OBSERVED, 1.0)


def test_note_on_zero_velocity_is_an_observed_release() -> None:
    backend = FakeBackend()
    port = backend.connect("Arturia MiniLab3 MIDI")
    driver, frames = collecting_driver(backend, declared_notes=(48,))
    port.send(FakeMessage("note_on", note=48, velocity=127))
    driver.poll_once()
    assert frames[-1][1]["note_48_vel"] == (1.0, OBSERVED, 1.0)

    port.send(FakeMessage("note_on", note=48, velocity=0))
    driver.poll_once()
    assert frames[-1][1]["note_48_vel"] == (0.0, OBSERVED, 1.0)


@pytest.mark.parametrize(
    ("port_name", "explicit_pattern"),
    [
        ("Arturia MiniLab 3 MIDI", None),
        ("Launchpad Mini MK3 LPMiniMK3 MIDI In", "launchPAD MINI"),
    ],
)
def test_minilab3_and_launchpad_mini_port_conventions(
    port_name: str, explicit_pattern: str | None
) -> None:
    backend = FakeBackend()
    backend.connect("Unrelated MIDI Device")
    backend.connect(port_name)
    driver, _frames = collecting_driver(backend, port_pattern=explicit_pattern)

    driver.poll_once()
    assert driver.connected is True
    assert driver.port_name == port_name
    assert backend.opened == [port_name]


def test_explicit_absent_device_does_not_bind_an_unrelated_port() -> None:
    backend = FakeBackend()
    backend.connect("Unrelated MIDI Device")
    driver, frames = collecting_driver(
        backend,
        port_pattern="Launchpad Mini",
        declared_ccs=(1,),
    )

    assert driver.poll_once() is True
    assert driver.connected is False
    assert backend.opened == []
    assert_invalid(frames[-1][1]["cc_1"])


def test_absence_arrival_removal_and_rearrival_state_transitions() -> None:
    backend = FakeBackend()
    driver, frames = collecting_driver(
        backend,
        declared_ccs=(1,),
        declared_notes=(36,),
    )

    driver.poll_once()
    assert_invalid(frames[-1][1]["cc_1"])
    assert_invalid(frames[-1][1]["note_36_vel"])

    first_port = backend.connect("Launchpad Mini MK3 MIDI In")
    first_port.send(
        FakeMessage("control_change", control=1, value=127),
        FakeMessage("note_on", note=36, velocity=100),
    )
    driver.poll_once()
    assert driver.connected is True
    assert frames[-1][1]["cc_1"] == (1.0, OBSERVED, 1.0)
    assert frames[-1][1]["modwheel"] == (1.0, OBSERVED, 1.0)
    assert frames[-1][1]["note_36_vel"] == pytest.approx(
        (100.0 / 127.0, OBSERVED, 1.0)
    )

    backend.disconnect("Launchpad Mini MK3 MIDI In")
    driver.poll_once()
    assert driver.connected is False
    assert first_port.closed is True
    for channel in frames[-1][1].values():
        assert_invalid(channel)

    second_port = backend.connect("Launchpad Mini MK3 MIDI In")
    second_port.send(FakeMessage("control_change", control=1, value=32))
    driver.poll_once()
    assert driver.connected is True
    assert frames[-1][1]["cc_1"] == pytest.approx(
        (32.0 / 127.0, OBSERVED, 1.0)
    )
    assert frames[-1][1]["modwheel"] == frames[-1][1]["cc_1"]
    # A reconnected device must not resurrect a stale note velocity.
    assert_invalid(frames[-1][1]["note_36_vel"])


def test_enumeration_and_mid_poll_failures_degrade_to_invalid() -> None:
    backend = FakeBackend()
    port = backend.connect("Launchpad Mini MIDI")
    driver, frames = collecting_driver(backend, declared_ccs=(1,))
    port.send(FakeMessage("control_change", control=1, value=80))
    driver.poll_once()
    assert frames[-1][1]["cc_1"][1] == OBSERVED

    backend.enumeration_error = OSError("MIDI subsystem disappeared")
    assert driver.poll_once() is True
    assert driver.connected is False
    assert_invalid(frames[-1][1]["cc_1"])
    assert port.closed is True
