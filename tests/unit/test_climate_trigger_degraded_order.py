"""Degradation-ladder stepping in the recurring trigger handlers.

Every recurring trigger advances the control-mode ladder via
check_and_update_degraded_mode before the critical-entity check may abort
the handler. The ladder therefore keeps stepping in the combined failure
case (room sensor lost while a TRV is offline) instead of freezing at
OPTIMAL behind the critical-entity early return.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    LadderParams,
)
from custom_components.better_thermostat.trv import Trv

_CLIMATE = "custom_components.better_thermostat.climate"
_WATCHER = "custom_components.better_thermostat.utils.watcher"
SENSOR_ID = "sensor.room_temp"
TRV_ID = "climate.test_trv"


@pytest.fixture
def bt():
    """Mock BT whose room sensor and only TRV both read as unavailable."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.kernel_state = KernelState()
    mock.clock = FakeClock()
    mock.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID)}
    mock.sensor_entity_id = SENSOR_ID
    mock.humidity_sensor_entity_id = None
    mock.window_id = None
    mock.door_id = None
    mock.outdoor_sensor = None
    mock.weather_entity = None
    mock.cooler_entity_id = None
    mock.unavailable_sensors = []
    mock._degraded_warning_emitted = False
    mock.in_maintenance = False
    mock.hass = MagicMock()
    mock.hass.states.get.return_value = None
    return mock


@pytest.mark.asyncio
async def test_trigger_steps_ladder_while_trv_is_unavailable(bt):
    """The ladder leaves OPTIMAL even though the critical check aborts."""
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=False)),
        patch(f"{_CLIMATE}.check_ambient_air_temperature", AsyncMock()) as ambient,
        patch(f"{_WATCHER}.ir.async_create_issue"),
        patch(
            "custom_components.better_thermostat.utils.helpers.async_fire_logbook_entry",
            AsyncMock(),
        ),
    ):
        await BetterThermostat._trigger_time(bt, None)
        assert bt.kernel_state.control_mode.degraded is True

        # A downgrade commits only after the debounce window has elapsed.
        bt.clock.advance(LadderParams().down_debounce_s + 1)
        await BetterThermostat._trigger_time(bt, None)

    assert bt.kernel_state.control_mode.mode != ControlMode.OPTIMAL
    assert bt.kernel_state.control_mode.mode == ControlMode.HOLD
    assert bt.kernel_state.control_mode.degraded is True
    # The critical-entity early return still stops the rest of the handler.
    ambient.assert_not_awaited()
