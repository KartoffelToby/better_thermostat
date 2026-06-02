"""Branch coverage for BetterThermostat.reset_pid_learnings_service.

The service clears cached PID state for the entity and can optionally seed PID
defaults into the current target bucket and its ±0.5 °C neighbours.  These tests
pin the reset count, the bucket key construction, and the seed conditions.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.utils.calibration.pid import PIDParams

_CLIMATE = "custom_components.better_thermostat.climate"
_PID = "custom_components.better_thermostat.utils.calibration.pid"


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for the reset-PID service."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock._unique_id = "uid"
    mock.unique_id = "uid"
    mock.bt_target_temp = 21.0
    mock.real_trvs = {"climate.trv": {}}
    mock.schedule_save_state = MagicMock()
    mock.control_queue_task = AsyncMock()
    return mock


@pytest.mark.asyncio
async def test_resets_each_cached_key(bt):
    """Every exported PID key for this entity is reset and persistence scheduled."""
    keys = {"uid:climate.trv:t21.0": {}, "uid:climate.trv:t20.0": {}}
    with (
        patch(f"{_CLIMATE}.pid_export_states", MagicMock(return_value=keys)),
        patch(f"{_CLIMATE}.pid_reset_state") as reset,
    ):
        await BetterThermostat.reset_pid_learnings_service(bt)
    assert reset.call_count == 2
    bt.schedule_save_state.assert_called()


@pytest.mark.asyncio
async def test_no_keys_still_schedules_save(bt):
    """With nothing cached, no reset happens but a save is still scheduled."""
    with (
        patch(f"{_CLIMATE}.pid_export_states", MagicMock(return_value={})),
        patch(f"{_CLIMATE}.pid_reset_state") as reset,
    ):
        await BetterThermostat.reset_pid_learnings_service(bt)
    reset.assert_not_called()
    bt.schedule_save_state.assert_called_once()


@pytest.mark.asyncio
async def test_no_defaults_does_not_seed(bt):
    """Without apply_pid_defaults, no gains are seeded."""
    with (
        patch(f"{_CLIMATE}.pid_export_states", MagicMock(return_value={})),
        patch(f"{_CLIMATE}.pid_reset_state"),
        patch(f"{_PID}.seed_pid_gains") as seed,
    ):
        await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=False)
    seed.assert_not_called()


@pytest.mark.asyncio
async def test_seeds_current_and_neighbour_buckets(bt):
    """Defaults seed the current bucket and its ±0.5 °C neighbours per TRV."""
    with (
        patch(f"{_CLIMATE}.pid_export_states", MagicMock(return_value={})),
        patch(f"{_CLIMATE}.pid_reset_state"),
        patch(f"{_PID}.seed_pid_gains", MagicMock(return_value=True)) as seed,
    ):
        await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=True)
    seeded_keys = {call.args[0] for call in seed.call_args_list}
    assert seeded_keys == {
        "uid:climate.trv:t21.0",
        "uid:climate.trv:t21.5",
        "uid:climate.trv:t20.5",
    }
    # Seeding happened -> control loop is kicked
    bt.control_queue_task.put.assert_awaited_once()


@pytest.mark.asyncio
async def test_defaults_use_pidparams_values(bt):
    """Without overrides, the PIDParams defaults are seeded."""
    defaults = PIDParams()
    with (
        patch(f"{_CLIMATE}.pid_export_states", MagicMock(return_value={})),
        patch(f"{_CLIMATE}.pid_reset_state"),
        patch(f"{_PID}.seed_pid_gains", MagicMock(return_value=True)) as seed,
    ):
        await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=True)
    kwargs = seed.call_args_list[0].kwargs
    assert kwargs == {"kp": defaults.kp, "ki": defaults.ki, "kd": defaults.kd}


@pytest.mark.asyncio
async def test_overrides_are_passed_through(bt):
    """Explicit kp/ki/kd overrides are forwarded to seeding."""
    with (
        patch(f"{_CLIMATE}.pid_export_states", MagicMock(return_value={})),
        patch(f"{_CLIMATE}.pid_reset_state"),
        patch(f"{_PID}.seed_pid_gains", MagicMock(return_value=True)) as seed,
    ):
        await BetterThermostat.reset_pid_learnings_service(
            bt,
            apply_pid_defaults=True,
            defaults_kp=1.5,
            defaults_ki=0.2,
            defaults_kd=0.05,
        )
    kwargs = seed.call_args_list[0].kwargs
    assert kwargs == {"kp": 1.5, "ki": 0.2, "kd": 0.05}


@pytest.mark.asyncio
async def test_no_trvs_seeds_nothing(bt):
    """With no TRVs, nothing is seeded and the control loop is not kicked."""
    bt.real_trvs = {}
    with (
        patch(f"{_CLIMATE}.pid_export_states", MagicMock(return_value={})),
        patch(f"{_CLIMATE}.pid_reset_state"),
        patch(f"{_PID}.seed_pid_gains", MagicMock(return_value=True)) as seed,
    ):
        await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=True)
    seed.assert_not_called()
    bt.control_queue_task.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_numeric_target_seeds_nothing(bt):
    """A non-numeric target yields no buckets, so nothing is seeded."""
    bt.bt_target_temp = None
    with (
        patch(f"{_CLIMATE}.pid_export_states", MagicMock(return_value={})),
        patch(f"{_CLIMATE}.pid_reset_state"),
        patch(f"{_PID}.seed_pid_gains", MagicMock(return_value=True)) as seed,
    ):
        await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=True)
    seed.assert_not_called()
    bt.control_queue_task.put.assert_not_awaited()
