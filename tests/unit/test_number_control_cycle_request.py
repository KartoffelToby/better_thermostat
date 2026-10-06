"""A number entity asks for a control cycle without waiting for the queue.

The control queue holds one pending cycle. While the consumer is held up,
for example by the startup sequence, a caller that awaits a place in a full
queue hangs, and with it the ``number.set_value`` service call or the
platform setup that restores a preset.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import PRESET_HOME, HVACMode
from homeassistant.const import UnitOfTemperature
import pytest

from custom_components.better_thermostat.number import (
    BetterThermostatPresetCoolNumber,
    BetterThermostatPresetNumber,
)
from custom_components.better_thermostat.utils.preset_manager import PresetManager
from tests.factories import ThermostatStandIn


@pytest.fixture(autouse=True)
def _detached_state_tracking():
    """Let the entities subscribe to the thermostat's state without a hass."""
    with (
        patch(
            "custom_components.better_thermostat.entity.async_track_state_change_event",
            MagicMock(),
        ),
        patch(
            "custom_components.better_thermostat.entity.async_dispatcher_connect",
            MagicMock(),
        ),
    ):
        yield


def _thermostat_with_a_pending_cycle():
    """Return a thermostat whose control queue already holds a cycle."""
    bt_climate = ThermostatStandIn()
    bt_climate.unique_id = "test_bt"
    bt_climate.device_name = "Test BT"
    bt_climate.bt_min_temp = 5.0
    bt_climate.bt_max_temp = 30.0
    bt_climate.cool_min_temperature = None
    bt_climate.cool_max_temperature = None
    bt_climate.cooler_entity_id = "climate.cooler"
    bt_climate.bt_target_temp_step = 0.5
    bt_climate.preset_mgr = PresetManager(mode=PRESET_HOME)
    bt_climate.preset_mode = bt_climate.preset_mgr.mode
    bt_climate.bt_hvac_mode = HVACMode.HEAT_COOL
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(bt_climate)
    bt_climate.control_queue_task = queue
    return bt_climate


@pytest.mark.asyncio
async def test_setting_the_active_cooling_preset_does_not_wait_for_the_queue():
    """The service call returns while a cycle is pending; one cycle stays pending."""
    bt_climate = _thermostat_with_a_pending_cycle()
    bt_climate.bt_target_temp = 22.0
    bt_climate.bt_target_cooltemp = 24.0
    bt_climate._preset_cool_temperatures = {PRESET_HOME: 24.0}
    entity = BetterThermostatPresetCoolNumber(bt_climate, PRESET_HOME)
    entity.async_write_ha_state = MagicMock()

    await asyncio.wait_for(entity.async_set_native_value(25.0), timeout=1)

    assert bt_climate.bt_target_cooltemp == 25.0
    assert bt_climate.control_queue_task.qsize() == 1


@pytest.mark.asyncio
async def test_restoring_the_active_preset_does_not_wait_for_the_queue():
    """The restore finishes while a cycle is pending; one cycle stays pending."""
    bt_climate = _thermostat_with_a_pending_cycle()
    bt_climate.bt_target_temp = 20.0
    bt_climate._bound_target_to_range.side_effect = lambda value: value
    entity = BetterThermostatPresetNumber(bt_climate, PRESET_HOME)
    last_state = MagicMock()
    last_state.state = "21.5"
    last_state.attributes = {"unit_of_measurement": UnitOfTemperature.CELSIUS}
    entity.async_get_last_state = AsyncMock(return_value=last_state)

    await asyncio.wait_for(entity.async_added_to_hass(), timeout=1)

    assert bt_climate.bt_target_temp == 21.5
    assert bt_climate.control_queue_task.qsize() == 1
