"""Tests for the internal heating target offset."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
import pytest

from custom_components.better_thermostat.number import (
    BetterThermostatTargetOffsetNumber,
)
from tests.factories import ThermostatStandIn


@pytest.fixture
def thermostat():
    """Build a thermostat with an initially empty control queue."""
    bt = ThermostatStandIn()
    bt.unique_id = "test"
    bt.device_name = "Test"
    bt.target_offset = 0.0
    bt.heat_target_temperature = 19.0
    bt.cooler_entity_id = None
    bt.startup_running = False
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    return bt


@pytest.mark.parametrize(
    ("unit", "value", "expected"),
    [(UnitOfTemperature.CELSIUS, 0.5, 0.5), (UnitOfTemperature.FAHRENHEIT, 0.9, 0.5)],
)
async def test_set_offset_in_system_unit(thermostat, unit, value, expected):
    """Temperature differences convert without an absolute-temperature shift."""
    entity = BetterThermostatTargetOffsetNumber(thermostat, unit)
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(value)

    assert thermostat.target_offset == pytest.approx(expected)
    assert entity.native_value == pytest.approx(value)
    assert thermostat.heat_target_temperature == 19.0
    assert thermostat.control_queue_task.get_nowait() is thermostat


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -10.1, 10.1])
async def test_invalid_offset_is_rejected(thermostat, value):
    """Invalid settings leave the regulation state and queue unchanged."""
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    await entity.async_set_native_value(value)
    assert thermostat.target_offset == 0.0
    assert thermostat.control_queue_task.empty()


@pytest.mark.parametrize(
    ("saved", "unit", "expected"),
    [
        ("-0.5", UnitOfTemperature.CELSIUS, -0.5),
        ("0.9", UnitOfTemperature.FAHRENHEIT, 0.5),
    ],
)
async def test_restore_offset(thermostat, saved, unit, expected):
    """Restored states retain their saved temperature difference unit."""
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    entity.async_write_ha_state = MagicMock()
    state = MagicMock(state=saved, attributes={"unit_of_measurement": unit})
    entity.async_get_last_state = AsyncMock(return_value=state)
    with patch.object(entity, "_follow_thermostat"):
        await entity.async_added_to_hass()
    assert thermostat.target_offset == pytest.approx(expected)
    assert thermostat.control_queue_task.get_nowait() is thermostat


async def test_pending_cycle_is_coalesced(thermostat):
    """Changing the offset never waits on an already full queue."""
    thermostat.control_queue_task.put_nowait(thermostat)
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    entity.async_write_ha_state = MagicMock()
    await asyncio.wait_for(entity.async_set_native_value(0.5), timeout=1)
    assert thermostat.target_offset == 0.5
    assert thermostat.control_queue_task.qsize() == 1
