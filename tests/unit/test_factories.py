"""Smoke tests: the shared factories work against production functions."""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import HVACAction
import pytest

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.decide import decide
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.scheduler import request_control_cycle
from tests.factories import (
    DEFAULT_TRV_ID,
    STAND_IN_DEFAULTS,
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
    ],
)
def test_the_stand_in_refuses_state_it_was_not_given(name):
    """Reading thermostat state nobody set raises instead of answering a mock."""
    assert name in THERMOSTAT_STATE
    with pytest.raises(AttributeError, match=name):
        getattr(ThermostatStandIn(), name)


@pytest.mark.parametrize("name", sorted(STAND_IN_DEFAULTS))
def test_a_defaulted_name_is_thermostat_state(name):
    """Every default the stand-in answers names state the thermostat holds."""
    assert name in THERMOSTAT_STATE


def test_the_stand_in_answers_constructor_bookkeeping():
    """Bookkeeping a constructed thermostat holds reads as the constructor set it."""
    bt = ThermostatStandIn()

    assert bt.unavailable_sensors == []
    assert bt._critical_grace_until is None
    assert bt._outdoor_check_lock is None
    assert bt.flight_recorder.export() == []
    assert bt.task_manager.tasks == set()
    assert bt.task_manager.hass is None
    assert bt.unique_id is None


def test_each_stand_in_gets_its_own_defaults():
    """A mutable default is built per stand-in, so tests cannot share it."""
    first = ThermostatStandIn()
    first.unavailable_sensors.append("sensor.outdoor")

    assert ThermostatStandIn().unavailable_sensors == []
    assert first.unavailable_sensors == ["sensor.outdoor"]


def test_the_unique_id_answers_from_the_private_id():
    """``unique_id`` reads ``_unique_id``, as the property does."""
    bt = ThermostatStandIn()
    bt._unique_id = "entry_1"

    assert bt.unique_id == "entry_1"


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


async def test_a_specced_stand_in_keeps_coroutine_methods_awaitable():
    """Under a spec, a coroutine method is an AsyncMock as on a plain spec mock."""
    bt = ThermostatStandIn(spec=BetterThermostat)

    await bt.async_set_temperature(temperature=21.0)

    bt.async_set_temperature.assert_awaited_once_with(temperature=21.0)
    assert not isinstance(bt.async_write_ha_state, AsyncMock)
