"""Branch coverage for BetterThermostat.async_set_hvac_mode.

Pins the supported-mode handling, the rejection of unsupported modes, and the
maintenance defer that must not enqueue a control action mid-exercise.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.climate import BetterThermostat

_CLIMATE = "custom_components.better_thermostat.climate"


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for setting the HVAC mode."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.bt_hvac_mode = HVACMode.HEAT
    mock.in_maintenance = False
    mock._control_needed_after_maintenance = False
    mock.async_write_ha_state = MagicMock()
    mock.control_queue_task = AsyncMock()
    return mock


def _identity_mode():
    """get_hvac_bt_mode stand-in: return the requested mode unchanged."""
    return MagicMock(side_effect=lambda _self, mode: mode)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.OFF])
async def test_supported_mode_is_applied_and_queued(bt, mode):
    """A supported mode is stored, state is written, and control is queued."""
    with patch(f"{_CLIMATE}.get_hvac_bt_mode", _identity_mode()):
        await BetterThermostat.async_set_hvac_mode(bt, mode)
    assert bt.bt_hvac_mode == mode
    bt.async_write_ha_state.assert_called_once()
    bt.control_queue_task.put.assert_awaited_once_with(bt)


@pytest.mark.asyncio
async def test_unsupported_mode_is_rejected(bt):
    """An unsupported mode leaves bt_hvac_mode untouched but still queues control."""
    with patch(f"{_CLIMATE}.get_hvac_bt_mode", _identity_mode()) as mapper:
        await BetterThermostat.async_set_hvac_mode(bt, HVACMode.COOL)
    assert bt.bt_hvac_mode == HVACMode.HEAT  # unchanged
    mapper.assert_not_called()
    bt.control_queue_task.put.assert_awaited_once_with(bt)


@pytest.mark.asyncio
async def test_maintenance_defers_control(bt):
    """During maintenance the control queue is not touched; a flag is set instead."""
    bt.in_maintenance = True
    with patch(f"{_CLIMATE}.get_hvac_bt_mode", _identity_mode()):
        await BetterThermostat.async_set_hvac_mode(bt, HVACMode.HEAT)
    assert bt._control_needed_after_maintenance is True
    bt.control_queue_task.put.assert_not_awaited()
