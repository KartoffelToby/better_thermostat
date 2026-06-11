"""Coverage for BetterThermostat._run_valve_maintenance.

Focus is the state-flag contract around the valve exercise: in_maintenance and
ignore_states MUST always be released (even on error), otherwise the control
loop can stall.  Also covers the re-entry guard, reschedule, and control kick.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv

_CLIMATE = "custom_components.better_thermostat.climate"
_NEXT = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for the valve-maintenance run."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.in_maintenance = False
    mock.ignore_states = False
    mock.real_trvs = {"climate.trv": Trv(entity_id="climate.trv")}
    mock.bt_hvac_mode = HVACMode.HEAT
    mock.hass = MagicMock()
    mock.control_queue_task = MagicMock()
    return mock


def _snapshots():
    """build_trv_snapshots stand-in: one serviced TRV."""
    return MagicMock(return_value=[SimpleNamespace(entity_id="climate.trv")])


@pytest.mark.asyncio
async def test_reentry_guard(bt):
    """A run while already in maintenance returns without doing work."""
    bt.in_maintenance = True
    with patch(f"{_CLIMATE}.build_trv_snapshots") as snap:
        await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])
    snap.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_resets_flags_and_reschedules(bt):
    """A successful run releases the flags, reschedules, and kicks control."""
    with (
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(f"{_CLIMATE}.run_valve_maintenance", AsyncMock()),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
    ):
        await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])
    assert bt.in_maintenance is False
    assert bt.ignore_states is False
    assert bt.next_valve_maintenance == _NEXT
    bt.control_queue_task.put_nowait.assert_called_once_with(bt)


@pytest.mark.asyncio
async def test_flags_released_even_on_error(bt):
    """If the exercise raises, in_maintenance and ignore_states are still cleared."""
    with (
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(
            f"{_CLIMATE}.run_valve_maintenance",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
    ):
        with pytest.raises(RuntimeError):
            await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])
    assert bt.in_maintenance is False
    assert bt.ignore_states is False


@pytest.mark.asyncio
async def test_no_control_kick_when_off(bt):
    """In OFF mode no control cycle is queued after maintenance."""
    bt.bt_hvac_mode = HVACMode.OFF
    with (
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(f"{_CLIMATE}.run_valve_maintenance", AsyncMock()),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
    ):
        await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])
    bt.control_queue_task.put_nowait.assert_not_called()
