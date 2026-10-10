"""Tests that PID calibration reads and writes state through the state manager."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.calibration import _compute_pid_balance
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.pid import (
    DEFAULT_PID_KP,
    PIDState,
    build_pid_key,
    build_pid_loop_key,
)
from tests.factories import ThermostatStandIn, make_state


class _PidStateStub:
    """Minimal stand-in for the state manager's PID accessors."""

    def __init__(self) -> None:
        self.pid: dict[str, PIDState] = {}

    @property
    def state(self) -> _PidStateStub:
        """Expose the stored entries the way the state manager does."""
        return self

    def get_pid(self, key: str) -> PIDState:
        """Return the stored state for ``key``, creating it on first access."""
        return self.pid.setdefault(key, PIDState())

    def set_pid(self, key: str, pid: PIDState) -> None:
        """Store ``pid`` under ``key``."""
        self.pid[key] = pid


def _make_bt(state_mgr: _PidStateStub) -> ThermostatStandIn:
    """Return a BetterThermostat mock wired for a single heating TRV."""
    bt = ThermostatStandIn()
    bt.kernel_state = make_state()
    bt.device_name = "Test BT"
    bt.unique_id = "uid"
    bt.heat_target_temperature = 22.0
    bt.room_temperature = 20.0
    bt.room_temperature_filtered = None
    bt.temperature_slope = 0.0
    bt.window_open = False
    bt.contact_open = False
    bt.bt_hvac_mode = "heat"
    bt.clock = FakeClock(monotonic_value=1_000.0)
    bt.real_trvs = {
        "climate.trv": Trv(
            entity_id="climate.trv",
            advanced={},
            current_temperature=21.0,
            min_temp=5.0,
            max_temp=30.0,
        )
    }
    bt.state_mgr = state_mgr
    return bt


def test_pid_balance_persists_learned_state_in_state_manager() -> None:
    """The loop state lands under the TRV's key, the gains under the bucket's."""
    state_mgr = _PidStateStub()
    bt = _make_bt(state_mgr)

    _compute_pid_balance(bt, "climate.trv")

    loop = state_mgr.pid[build_pid_loop_key(bt, "climate.trv")]
    # error = |target - current| = |22.0 - 20.0|
    assert loop.last_abs_error == 2.0
    assert state_mgr.pid[build_pid_key(bt, "climate.trv")].pid_kp is not None


def test_pid_balance_threads_the_same_state_across_calls() -> None:
    """Each call continues from the loop state the previous one stored."""
    state_mgr = _PidStateStub()
    bt = _make_bt(state_mgr)
    key = build_pid_loop_key(bt, "climate.trv")

    _compute_pid_balance(bt, "climate.trv")
    _compute_pid_balance(bt, "climate.trv")

    assert state_mgr.pid[key].previous_abs_error == 2.0


def test_pid_sanitized_state_is_persisted_when_compute_raises() -> None:
    """The healed state replaces the poisoned one even on a compute failure."""
    state_mgr = _PidStateStub()
    bt = _make_bt(state_mgr)
    key = build_pid_loop_key(bt, "climate.trv")
    poisoned = PIDState()
    poisoned.pid_integral = float("nan")
    state_mgr.pid[key] = poisoned

    with patch(
        "custom_components.better_thermostat.calibration.compute_pid",
        side_effect=ValueError("boom"),
    ):
        percent, supports_valve = _compute_pid_balance(bt, "climate.trv")

    assert percent is None
    assert supports_valve is False
    stored = state_mgr.pid[key]
    assert stored.pid_integral == 0.0  # sanitized default, not NaN


def test_pid_healed_bucket_gains_are_persisted_when_compute_raises() -> None:
    """Runaway gains learned at a target are healed on disk on a failure too."""
    state_mgr = _PidStateStub()
    bt = _make_bt(state_mgr)
    key = build_pid_key(bt, "climate.trv")
    state_mgr.pid[key] = PIDState(pid_kp=1e9)
    state_mgr.set_pid = MagicMock(wraps=state_mgr.set_pid)

    with patch(
        "custom_components.better_thermostat.calibration.compute_pid",
        side_effect=ValueError("boom"),
    ):
        percent, _ = _compute_pid_balance(bt, "climate.trv")

    assert percent is None
    state_mgr.set_pid.assert_called_once_with(key, state_mgr.pid[key])
    assert state_mgr.pid[key].pid_kp is None


def test_pid_balance_without_a_state_store_publishes_no_valve() -> None:
    """Before the store is loaded there is no state to run the controller on."""
    bt = _make_bt(_PidStateStub())
    bt.state_mgr = None

    payload, supports_valve = _compute_pid_balance(bt, "climate.trv")

    assert payload is None
    assert supports_valve is False
    assert bt.real_trvs["climate.trv"].calibration_balance is None


def test_pid_failed_compute_on_a_healthy_state_leaves_the_store_alone() -> None:
    """Only a healed state is written back after a failed compute."""
    state_mgr = _PidStateStub()
    bt = _make_bt(state_mgr)
    healthy = state_mgr.get_pid(build_pid_loop_key(bt, "climate.trv"))
    state_mgr.set_pid = MagicMock(wraps=state_mgr.set_pid)

    with patch(
        "custom_components.better_thermostat.calibration.compute_pid",
        side_effect=ValueError("boom"),
    ):
        payload, _ = _compute_pid_balance(bt, "climate.trv")

    assert payload is None
    state_mgr.set_pid.assert_not_called()
    assert state_mgr.pid[build_pid_loop_key(bt, "climate.trv")] is healthy


_CYCLE_S = 300.0
_OUTDOOR = 10.0
# K/min the radiator adds at 100 % and K/min per kelvin above outdoors the
# room loses: holding 21 °C takes about 66 % valve.
_HEATER_GAIN = 0.05
_LOSS_RATE = 0.003


def _run_room(bt: ThermostatStandIn, cycles: int) -> list[tuple[float, float]]:
    """Run closed-loop PID cycles against a first-order room.

    Returns the (valve percent, room temperature) of every cycle.
    """
    trace: list[tuple[float, float]] = []
    for _ in range(cycles):
        percent, _ = _compute_pid_balance(bt, "climate.trv")
        assert percent is not None
        room = bt.room_temperature
        trace.append((percent, room))
        minutes = _CYCLE_S / 60.0
        bt.room_temperature = room + minutes * (
            _HEATER_GAIN * percent / 100.0 - _LOSS_RATE * (room - _OUTDOOR)
        )
        bt.clock.advance(_CYCLE_S)
    return trace


def _settled_room() -> tuple[ThermostatStandIn, float]:
    """Return a room held at 21 °C by the PID loop, and its valve opening."""
    bt = _make_bt(_PidStateStub())
    bt.heat_target_temperature = 21.0
    bt.room_temperature = 21.0
    trace = _run_room(bt, 600)
    percent, room = trace[-1]
    assert abs(room - 21.0) < 0.1
    assert 50.0 < percent < 80.0
    return bt, percent


@pytest.mark.parametrize("new_target", [21.2, 21.3, 21.5, 22.0])
def test_a_higher_target_never_closes_the_valve(new_target: float) -> None:
    """Raising the target keeps the valve at least as open as before.

    The integral that holds the room at its target carries over to the new
    target; the larger error only adds to it.
    """
    bt, held = _settled_room()

    bt.heat_target_temperature = new_target
    (first, _), *_ = _run_room(bt, 1)

    assert first >= held


def test_after_a_target_raise_the_room_warms_without_dipping() -> None:
    """After the user asks for more heat, the room warms from where it was.

    Over the hours after the raise the room temperature only goes up until
    it reaches the new target.
    """
    bt, _ = _settled_room()
    start = bt.room_temperature

    bt.heat_target_temperature = 21.5
    trace = _run_room(bt, 72)

    rooms = [room for _, room in trace]
    assert min(rooms) >= start - 0.01
    assert max(rooms) > 21.4


def test_lowering_the_target_to_the_room_is_no_overshoot() -> None:
    """A lower target the room already sits at leaves the gains learned there.

    Errors measured against the previous target are not compared with the
    first error at the new one, so the drop from 1 K to 0 K is not read as
    an overshoot that would lower Kp at the new target.
    """
    state_mgr = _PidStateStub()
    bt = _make_bt(state_mgr)
    bt.room_temperature = 21.0
    bt.heat_target_temperature = 22.0
    for _ in range(2):
        _compute_pid_balance(bt, "climate.trv")
        bt.clock.advance(_CYCLE_S)

    bt.heat_target_temperature = 21.0
    _compute_pid_balance(bt, "climate.trv")

    assert state_mgr.pid[build_pid_key(bt, "climate.trv")].pid_kp == DEFAULT_PID_KP
