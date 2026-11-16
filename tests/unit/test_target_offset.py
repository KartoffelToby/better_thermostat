"""Tests for the internal heating target offset."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.number import (
    BetterThermostatTargetOffsetNumber,
)
from custom_components.better_thermostat.utils.helpers import regulation_target
from tests.factories import ThermostatStandIn


@pytest.fixture
def thermostat():
    """Build a thermostat with an initially empty control queue."""
    bt = ThermostatStandIn()
    bt.unique_id = "test"
    bt.device_name = "Test"
    bt.target_offset = 0.0
    bt.heat_target_temperature = 19.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 24.0
    bt.cooler_entity_id = None
    bt.startup_running = False
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    return bt


@pytest.mark.parametrize(
    ("target", "offset", "expected"),
    [(5.0, -10.0, 5.0), (24.0, 10.0, 24.0), (20.0, -1.0, 19.0)],
)
def test_regulation_target_clamps_offset_to_room_range(
    thermostat, target, offset, expected
):
    """An offset cannot move regulation outside the heating range."""
    thermostat.heat_target_temperature = target
    thermostat.target_offset = offset

    assert regulation_target(thermostat) == pytest.approx(expected)


async def test_offset_reversal_restores_cooling_target(thermostat):
    """Removing an offset undoes the cooling-target lift it caused."""
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.cool_target_temperature = 22.0
    thermostat.heat_target_temperature = 21.0
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 35.0
    thermostat._offset_ordered_cool_target = None
    thermostat.hvac_mode = HVACMode.HEAT_COOL
    thermostat._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(thermostat, **kwargs)
    )
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(2.0)
    assert thermostat.cool_target_temperature == 23.5

    await entity.async_set_native_value(0.0)

    assert thermostat.cool_target_temperature == 22.0


@pytest.mark.parametrize(
    ("current", "saved", "expected"),
    [
        (23.5, [22.0, 23.5], (22.0, 23.5)),
        (24.0, [22.0, 23.5], None),
        (23.5, [float("nan"), 23.5], None),
    ],
)
def test_restore_offset_cooling_target_pair(thermostat, current, saved, expected):
    """Restore only a finite rollback pair that still matches the cool target."""
    thermostat.cool_target_temperature = current
    thermostat._offset_ordered_cool_target = None
    state = MagicMock(attributes={"offset_ordered_cool_target": saved})

    BetterThermostat._restore_offset_ordered_cool_target(thermostat, state)

    assert thermostat._offset_ordered_cool_target == expected


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
