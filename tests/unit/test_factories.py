"""Smoke tests: the shared factories work against production functions."""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction
import pytest

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.core.decide import decide
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.scheduler import request_control_cycle
from tests.factories import (
    DEFAULT_TRV_ID,
    THERMOSTAT_STATE,
    ThermostatStandIn,
    make_bt,
    make_snapshot,
    make_state,
)


def test_make_bt_runs_through_both_calibration_channels():
    """A factory-built entity feeds the real calibration functions."""
    bt = make_bt(
        hvac_action=HVACAction.IDLE,
        advanced={"calibration_mode": CalibrationMode.DEFAULT},
    )
    assert calculate_calibration_local(bt, DEFAULT_TRV_ID) is not None
    assert calculate_calibration_setpoint(bt, DEFAULT_TRV_ID) is not None


def test_make_bt_works_with_the_scheduler_facade():
    """The factory's control queue accepts a real cycle request."""
    bt = make_bt()
    request_control_cycle(bt)
    assert bt.control_queue_task.get_nowait() is bt


def test_make_snapshot_and_state_run_through_the_kernel():
    """The kernel input builders produce a decidable pair."""
    desired, _ = decide(make_snapshot(), make_state())
    assert set(desired.trvs) == {"climate.trv1", "climate.trv2"}


@pytest.mark.parametrize(
    "name",
    [
        "kernel_state",  # assigned in __init__
        "real_trvs",  # assigned in __init__
        "call_for_heat",  # a property with a setter
        "in_maintenance",  # a read-only property
        "task_manager",  # declared in the class body without a value
    ],
)
def test_the_stand_in_refuses_state_it_was_not_given(name):
    """Reading thermostat state nobody set raises instead of answering a mock."""
    assert name in THERMOSTAT_STATE
    with pytest.raises(AttributeError, match=name):
        getattr(ThermostatStandIn(), name)


def test_the_stand_in_answers_state_it_was_given():
    """State the test set reads back unchanged."""
    bt = ThermostatStandIn()
    bt.call_for_heat = False

    assert bt.call_for_heat is False


def test_the_stand_in_still_mocks_methods():
    """A method is behaviour, not state, so a mock answers it."""
    bt = ThermostatStandIn()

    bt.async_write_ha_state()

    bt.async_write_ha_state.assert_called_once_with()
    assert "async_write_ha_state" not in THERMOSTAT_STATE


def test_the_stand_ins_children_stay_permissive():
    """Only the thermostat is strict; ``bt.hass.config`` is a plain mock."""
    bt = ThermostatStandIn()

    assert "config" in THERMOSTAT_STATE
    assert not isinstance(bt.hass, ThermostatStandIn)
    assert isinstance(bt.hass.config, MagicMock)
