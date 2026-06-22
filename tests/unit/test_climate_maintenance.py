"""Branch coverage for BetterThermostat._maintenance_tick.

_maintenance_tick decides, on each periodic tick, whether to run valve
maintenance now, postpone it, or schedule it far out.  These tests pin every
decision branch so the scheduling contract is locked down.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.core.fsm.maintenance import (
    MAX_RUN_S,
    MaintenancePhase,
    MaintenanceState,
)

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
    mock.hvac_mode = HVACMode.HEAT
    mock.bt_hvac_mode = HVACMode.HEAT
    mock.real_trvs = {"climate.trv": {}}
    mock.hass = MagicMock()
    mock.hass.async_create_background_task = MagicMock()
    mock.clock = MagicMock()
    mock.clock.now.return_value = _NOW
    mock.clock.monotonic.return_value = 1000.0
    mock.kernel_state = KernelState()
    return mock


@pytest.mark.asyncio
async def test_critical_entities_unavailable_returns_early(bt):
    """When critical entities are unavailable, nothing is scheduled or dispatched."""
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=False)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
    ):
        await BetterThermostat._maintenance_tick(bt)
    assert bt.next_valve_maintenance is None
    bt.hass.async_create_background_task.assert_not_called()


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
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_not_due_yet_returns(bt):
    """When the next run is still in the future, the tick is a no-op."""
    bt.next_valve_maintenance = _NOW + timedelta(hours=2)
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_window_open_postpones_one_hour(bt):
    """An open window postpones maintenance by one hour."""
    bt.window_open = True
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
    ):
        await BetterThermostat._maintenance_tick(bt)
    assert bt.next_valve_maintenance == _NOW + timedelta(hours=1)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_hvac_off_postpones_one_hour(bt):
    """HVAC OFF (on either mode) postpones maintenance by one hour."""
    bt.bt_hvac_mode = HVACMode.OFF
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
    ):
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
    ):
        await BetterThermostat._maintenance_tick(bt)
    assert bt.next_valve_maintenance == _NOW + timedelta(days=7)
    bt.hass.async_create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_resync_keeps_running_since(bt):
    """Re-syncing the schedule must not turn a stale run into a permanent block.

    A RUNNING region older than MAX_RUN_S no longer blocks. When the
    legacy schedule attribute diverges from the region's next_due, the
    resync has to carry running_since along — dropping it would recreate
    a RUNNING region without a timestamp, which blocks unconditionally.
    """
    stale_now = MAX_RUN_S + 1.0
    bt.clock.monotonic.return_value = stale_now
    bt.kernel_state = replace(
        bt.kernel_state,
        maintenance=MaintenanceState(
            phase=MaintenancePhase.RUNNING,
            next_due=_NOW - timedelta(hours=2),
            running_since=0.0,
        ),
    )
    bt.next_valve_maintenance = _NOW
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
    ):
        await BetterThermostat._maintenance_tick(bt)
    region = bt.kernel_state.maintenance
    assert region.running_since == 0.0
    assert region.is_blocking(stale_now) is False


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
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt.hass.async_create_background_task.assert_called_once()
