"""Branch coverage for BetterThermostat.reset_pid_learnings_service.

The service clears the entity's PID state in the StateManager and can
optionally seed PID defaults into the current target bucket and its ±0.5 °C
neighbours.  These tests pin the reset scope, the bucket key construction,
the seed conditions, and the errors a call that cannot do its work raises.
"""

import math
from unittest.mock import MagicMock

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
)
from tests.factories import ThermostatStandIn, make_trv


class _StateMgrStub:
    """Minimal stand-in for the StateManager's PID surface."""

    def __init__(self) -> None:
        self.pid: dict[str, PIDState] = {}
        self.dirty = False

    @property
    def state(self):
        return self

    def get_pid(self, key: str) -> PIDState:
        return self.pid.setdefault(key, PIDState())

    def set_pid(self, key: str, pid: PIDState) -> None:
        self.pid[key] = pid
        self.dirty = True

    def reset_pid_states(self, prefix: str) -> int:
        keys = [key for key in self.pid if key.startswith(prefix)]
        for key in keys:
            del self.pid[key]
        if keys:
            self.dirty = True
        return len(keys)

    def mark_dirty(self) -> None:
        self.dirty = True


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for the reset-PID service."""
    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock._unique_id = "uid"
    mock.unique_id = "uid"
    mock.heat_target_temperature = 21.0
    mock.real_trvs = {"climate.trv": make_trv("climate.trv")}
    mock.schedule_save_state = MagicMock()
    mock.control_queue_task = MagicMock()
    mock.state_mgr = _StateMgrStub()
    return mock


@pytest.mark.asyncio
async def test_resets_each_cached_key(bt):
    """Every PID key for this entity is removed and persistence scheduled."""
    bt.state_mgr.pid = {
        "uid:climate.trv:t21.0": PIDState(),
        "uid:climate.trv:t20.0": PIDState(),
        "other:climate.x:t21.0": PIDState(),
    }
    await BetterThermostat.reset_pid_learnings_service(bt)
    assert set(bt.state_mgr.pid) == {"other:climate.x:t21.0"}
    bt.schedule_save_state.assert_called()


@pytest.mark.asyncio
async def test_no_keys_still_schedules_save(bt):
    """With nothing cached, no removal happens but a save is still scheduled."""
    await BetterThermostat.reset_pid_learnings_service(bt)
    assert bt.state_mgr.pid == {}
    bt.schedule_save_state.assert_called_once()


@pytest.mark.asyncio
async def test_no_state_manager_is_reported(bt):
    """Before startup has loaded the state, the call says so and saves nothing."""
    bt.state_mgr = None
    with pytest.raises(HomeAssistantError) as refused:
        await BetterThermostat.reset_pid_learnings_service(bt)
    assert refused.value.translation_key == "pid_state_not_ready"
    bt.schedule_save_state.assert_not_called()


@pytest.mark.asyncio
async def test_no_defaults_does_not_seed(bt):
    """Without apply_pid_defaults, no gains are seeded."""
    await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=False)
    assert bt.state_mgr.pid == {}


@pytest.mark.asyncio
async def test_seeds_current_and_neighbour_buckets(bt):
    """Defaults seed the current bucket and its ±0.5 °C neighbours per TRV."""
    await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=True)
    assert set(bt.state_mgr.pid) == {
        "uid:climate.trv:t21.0",
        "uid:climate.trv:t21.5",
        "uid:climate.trv:t20.5",
    }
    # Seeding happened -> control loop is kicked
    bt.control_queue_task.put_nowait.assert_called_once()


@pytest.mark.asyncio
async def test_defaults_use_pidparams_values(bt):
    """Without overrides, the PIDParams defaults are seeded."""
    defaults = PIDParams()
    await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=True)
    seeded = bt.state_mgr.pid["uid:climate.trv:t21.0"]
    assert seeded.pid_kp == defaults.kp
    assert seeded.pid_ki == defaults.ki
    assert seeded.pid_kd == defaults.kd


@pytest.mark.asyncio
async def test_overrides_are_passed_through(bt):
    """Explicit kp/ki/kd overrides are forwarded to seeding."""
    await BetterThermostat.reset_pid_learnings_service(
        bt, apply_pid_defaults=True, defaults_kp=1.5, defaults_ki=0.2, defaults_kd=0.05
    )
    seeded = bt.state_mgr.pid["uid:climate.trv:t21.0"]
    assert seeded.pid_kp == 1.5
    assert seeded.pid_ki == 0.2
    assert seeded.pid_kd == 0.05


@pytest.mark.asyncio
async def test_seeding_preserves_other_state_fields(bt):
    """Seeding only updates gains; learned fields like the integral survive.

    Note: this isolates the seeding code path by mocking
    ``reset_pid_states`` so the full reset is not run. The assertions
    therefore verify seeding-in-isolation, not that learned fields
    survive a complete ``reset_pid_learnings_service`` execution.
    """
    bt.state_mgr.pid["uid:climate.trv:t21.0"] = PIDState(pid_integral=7.5)
    # Reset clears the entry, so seed into a pre-populated *fresh* manager:
    bt.state_mgr.reset_pid_states = lambda prefix: 0
    await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=True)
    seeded = bt.state_mgr.pid["uid:climate.trv:t21.0"]
    assert seeded.pid_integral == 7.5
    assert seeded.pid_kp == PIDParams().kp


@pytest.mark.asyncio
async def test_no_trvs_seeds_nothing(bt):
    """With no TRVs, nothing is seeded and the control loop is not kicked."""
    bt.real_trvs = dict[str, Trv]()
    await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=True)
    assert bt.state_mgr.pid == {}
    bt.control_queue_task.put_nowait.assert_not_called()


@pytest.mark.parametrize("target", [None, math.nan])
@pytest.mark.asyncio
async def test_defaults_without_a_target_are_refused_before_the_reset(bt, target):
    """Defaults need a target bucket; without one nothing is reset or seeded.

    The call asked for a reset followed by seeding. Doing only the first
    half would leave the learned gains gone and no defaults in their place.
    """
    bt.heat_target_temperature = target
    bt.state_mgr.pid = {"uid:climate.trv:t21.0": PIDState(pid_integral=7.5)}
    with pytest.raises(ServiceValidationError) as refused:
        await BetterThermostat.reset_pid_learnings_service(bt, apply_pid_defaults=True)
    assert refused.value.translation_key == "pid_defaults_without_target"
    assert set(bt.state_mgr.pid) == {"uid:climate.trv:t21.0"}
    bt.control_queue_task.put_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_reset_is_reported(bt, caplog):
    """A failure of the state store surfaces and leaves its cause in the log."""

    def _broken(prefix: str) -> int:
        raise RuntimeError("store gone")

    bt.state_mgr.reset_pid_states = _broken
    with pytest.raises(HomeAssistantError) as failed:
        await BetterThermostat.reset_pid_learnings_service(bt)
    assert failed.value.translation_key == "pid_reset_failed"
    assert isinstance(failed.value.__cause__, RuntimeError)
    assert "store gone" in caplog.text
