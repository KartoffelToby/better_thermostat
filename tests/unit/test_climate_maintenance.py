"""Branch coverage for BetterThermostat._maintenance_tick.

_maintenance_tick decides, on each periodic tick, whether to run valve
maintenance now, postpone it, or schedule it far out.  These tests pin every
decision branch so the scheduling contract is locked down.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.climate import BetterThermostat

_CLIMATE = "custom_components.better_thermostat.climate"
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for maintenance scheduling."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.in_maintenance = False
    mock.next_valve_maintenance = None
    mock.window_open = False
    mock.contact_open = False
    mock.hvac_mode = HVACMode.HEAT
    mock.bt_hvac_mode = HVACMode.HEAT
    mock.real_trvs = {"climate.trv": {}}
    mock.hass = MagicMock()
    mock.hass.async_create_background_task = MagicMock()
    return mock


@pytest.mark.asyncio
async def test_one_unreadable_head_does_not_cost_the_others_their_exercise(bt):
    """Availability is reported, not obeyed: the readable valves still run.

    `check_critical_entities` rejects a room as soon as one head reports
    `unavailable` or `unknown` — the same two states the snapshot step
    filters on. Returning on it means a single flaky head suspends the
    exercise for every healthy valve in the room, for as long as it stays
    flaky, and the snapshot filter never gets to do its job.
    """
    critical = AsyncMock(return_value=False)
    with (
        patch(f"{_CLIMATE}.check_critical_entities", critical),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
    ):
        await BetterThermostat._maintenance_tick(bt)

    # Still consulted, so the repair issues it raises and clears stay current.
    critical.assert_awaited_once_with(bt)
    bt.hass.async_create_background_task.assert_called_once()


@pytest.mark.asyncio
async def test_availability_check_exception_returns(bt):
    """An exception during the availability check aborts the tick safely."""
    with patch(
        f"{_CLIMATE}.check_critical_entities",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_already_in_maintenance_returns(bt):
    """A tick during an in-flight maintenance run does nothing."""
    bt.in_maintenance = True
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.dt_util") as dt,
    ):
        dt.now.return_value = _NOW
        await BetterThermostat._maintenance_tick(bt)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_not_due_yet_returns(bt):
    """When the next run is still in the future, the tick is a no-op."""
    bt.next_valve_maintenance = _NOW + timedelta(hours=2)
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.dt_util") as dt,
    ):
        dt.now.return_value = _NOW
        await BetterThermostat._maintenance_tick(bt)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_window_open_postpones_one_hour(bt):
    """An open window postpones maintenance by one hour."""
    bt.window_open = True
    bt.contact_open = True
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.dt_util") as dt,
    ):
        dt.now.return_value = _NOW
        await BetterThermostat._maintenance_tick(bt)
    assert bt.next_valve_maintenance == _NOW + timedelta(hours=1)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode_attr", ["hvac_mode", "bt_hvac_mode"])
async def test_hvac_off_still_runs_maintenance(bt, mode_attr):
    """HVAC OFF does not postpone: a valve left shut over summer is the one that seizes."""
    setattr(bt, mode_attr, HVACMode.OFF)
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
        patch(f"{_CLIMATE}.dt_util") as dt,
    ):
        dt.now.return_value = _NOW
        await BetterThermostat._maintenance_tick(bt)
    bt.hass.async_create_background_task.assert_called_once()


@pytest.mark.asyncio
async def test_window_open_still_postpones_while_off(bt):
    """An open contact keeps postponing even now that OFF no longer does."""
    bt.bt_hvac_mode = HVACMode.OFF
    bt.contact_open = True
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.dt_util") as dt,
    ):
        dt.now.return_value = _NOW
        await BetterThermostat._maintenance_tick(bt)
    assert bt.next_valve_maintenance == _NOW + timedelta(hours=1)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_no_enabled_trvs_schedules_far_future(bt):
    """With no TRV enabled for maintenance, the next run is pushed out a week."""
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.collect_maintenance_trvs", MagicMock(return_value=[])),
        patch(f"{_CLIMATE}.dt_util") as dt,
    ):
        dt.now.return_value = _NOW
        await BetterThermostat._maintenance_tick(bt)
    assert bt.next_valve_maintenance == _NOW + timedelta(days=7)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_due_and_enabled_dispatches_maintenance(bt):
    """When due, heating, window closed and TRVs enabled, maintenance is dispatched."""
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
        patch(f"{_CLIMATE}.dt_util") as dt,
    ):
        dt.now.return_value = _NOW
        await BetterThermostat._maintenance_tick(bt)
    bt.hass.async_create_background_task.assert_called_once()
