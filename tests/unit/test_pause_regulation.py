"""Tests for the regulation interlock."""

from unittest.mock import MagicMock

from homeassistant.components.climate import HVACAction
from homeassistant.core import Event, EventStateChangedData, State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import decide
from custom_components.better_thermostat.core.desired import HeatingBlock, Suppression
from custom_components.better_thermostat.core.snapshot import HvacMode
from custom_components.better_thermostat.utils.state_manager import StateManager
from tests.factories import ThermostatStandIn, make_snapshot, make_state


def test_pause_suppresses_heating_without_changing_user_mode():
    """The interlock sends off intents while preserving the selected mode."""
    snapshot = make_snapshot(regulation_paused=True)
    state = make_state()

    desired, next_state = decide(snapshot, state)

    assert desired.heating_blocked_by == HeatingBlock.REGULATION_PAUSED
    assert all(trv.hvac_mode == HvacMode.OFF for trv in desired.trvs.values())
    assert all(
        trv.suppression == Suppression.REGULATION_PAUSED
        for trv in desired.trvs.values()
    )
    assert next_state.mode == state.mode


def test_suspending_controllers_preserves_learned_parameters(hass):
    """An interrupted measurement cannot erase gains or learned models."""
    manager = StateManager(hass, "pause_test")
    pid = manager.get_pid("pid")
    pid.pid_kp = 42.0
    pid.pid_last_time = 100.0
    pid.pid_last_meas = 20.0
    mpc = manager.get_mpc("mpc")
    mpc.gain_est = 0.7
    mpc.last_learn_temperature = 19.0
    mpc.last_learn_time = 90.0
    tpi = manager.get_tpi("tpi")
    tpi.last_update_ts = 100.0

    manager.suspend_regulation()

    assert pid.pid_kp == 42.0
    assert pid.pid_last_meas is None
    assert pid.pid_last_time == 0.0
    assert mpc.gain_est == 0.7
    assert mpc.last_learn_temperature is None
    assert mpc.last_learn_time is None
    assert tpi.last_update_ts == 0.0


def test_pause_interrupts_active_maintenance():
    """A pause stops an in-progress valve exercise and queues shutdown."""
    bt = ThermostatStandIn()
    bt.learning_pause_changes = 0
    bt.learning_held = False
    bt.state_loaded = False
    bt._heating_tracker = MagicMock()
    bt._maintenance_task = MagicMock()
    bt.control_queue_task = MagicMock()
    event = Event(
        "state_changed",
        EventStateChangedData(
            entity_id="input_boolean.pause",
            old_state=State("input_boolean.pause", "off"),
            new_state=State("input_boolean.pause", "on"),
        ),
    )

    BetterThermostat._trigger_pause_change(bt, event)

    bt._maintenance_task.cancel.assert_called_once()
    bt._heating_tracker.abort_cycle.assert_called_once()
    assert bt.learning_held
    bt.control_queue_task.put_nowait.assert_called_once_with(bt)


@pytest.mark.parametrize("learning_paused", [False, True])
async def test_passive_loss_learning_respects_user_pause(hass, learning_paused):
    """The regulation interlock alone does not stop passive heat-loss learning."""
    bt = ThermostatStandIn()
    bt.regulation_paused = True
    bt.learning_held = True
    bt.learning_paused = learning_paused
    bt.devices_released = False
    bt.released_trvs = set()
    bt.contact_sensed_open = False
    bt.room_temperature = 19.0
    bt.clock = FakeClock()
    bt._compute_hvac_action.return_value = HVACAction.IDLE
    bt._loss_tracker = MagicMock()
    bt._loss_tracker.update.return_value.cycle_result = None

    await BetterThermostat.calculate_heat_loss(bt)

    assert (
        bt._loss_tracker.update.call_args.kwargs["learning_paused"] is learning_paused
    )
