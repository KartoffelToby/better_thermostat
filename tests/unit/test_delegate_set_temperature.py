"""Rounding in the delegate's set_temperature stays in Celsius.

The temperature handed to the delegate is Celsius, so every step it may round
by has to be a Celsius step. The step a device publishes in its state is in the
device's own unit and is therefore not one of them.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters.delegate import set_temperature
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.trv"


@pytest.fixture
def bt():
    """Mock thermostat whose TRV carries no step of its own."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.hass = MagicMock()
    mock.bt_target_temp_step = None
    trv = Trv(entity_id=ENTITY_ID, min_temp=5.0, max_temp=30.0)
    trv.adapter = MagicMock()
    trv.adapter.set_temperature = AsyncMock(return_value=True)
    mock.real_trvs = {ENTITY_ID: trv}
    mock.hass.states.get.return_value = State(
        ENTITY_ID, "heat", attributes={"target_temp_step": 1.0}
    )
    return mock


@pytest.mark.asyncio
async def test_device_step_attribute_is_not_used_for_celsius_rounding(bt):
    """With no Celsius step known, the 0.5 default rounds, not the attribute."""
    await set_temperature(bt, ENTITY_ID, 21.3)

    bt.real_trvs[ENTITY_ID].adapter.set_temperature.assert_awaited_once_with(
        bt, ENTITY_ID, pytest.approx(21.5)
    )


@pytest.mark.asyncio
async def test_per_trv_step_rounds_the_setpoint(bt):
    """The TRV's own Celsius step rounds the outbound setpoint."""
    bt.real_trvs[ENTITY_ID].target_temp_step = 0.1

    await set_temperature(bt, ENTITY_ID, 21.34)

    bt.real_trvs[ENTITY_ID].adapter.set_temperature.assert_awaited_once_with(
        bt, ENTITY_ID, pytest.approx(21.3)
    )
