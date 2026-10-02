"""Coverage for BetterThermostat._run_valve_maintenance.

Focus is the state-flag contract around the valve exercise: in_maintenance and
ignore_states MUST always be released (even on error), otherwise the control
loop can stall.  Also covers the re-entry guard, reschedule, and control kick.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.core.fsm.maintenance import (
    MaintenancePhase,
    MaintenanceState,
    start_run,
)
from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn

_CLIMATE = "custom_components.better_thermostat.climate"
_NEXT = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for the valve-maintenance run."""
    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock.ignore_states = False
    mock.next_valve_maintenance = None
    mock.real_trvs = {"climate.trv": Trv(entity_id="climate.trv")}
    mock.clock = FakeClock(monotonic_value=1000.0)
    mock.kernel_state = KernelState()
    mock.bt_hvac_mode = HVACMode.HEAT
    mock._control_needed_after_maintenance = False
    mock.control_queue_task = MagicMock()
    return mock


def _snapshots():
    """build_trv_snapshots stand-in: one serviced TRV."""
    return MagicMock(return_value=[SimpleNamespace(entity_id="climate.trv")])


@pytest.mark.asyncio
async def test_reentry_guard(bt):
    """A run while the region is RUNNING returns without doing work."""
    bt.kernel_state = replace(
        bt.kernel_state,
        maintenance=start_run(
            MaintenanceState(phase=MaintenancePhase.DUE), now_monotonic=900.0
        ),
    )
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
    assert BetterThermostat.in_maintenance.fget(bt) is False
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
    assert BetterThermostat.in_maintenance.fget(bt) is False
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


@pytest.mark.asyncio
async def test_deferred_control_kicks_even_when_off(bt):
    """A control request deferred during maintenance is honored, even in OFF."""
    bt.bt_hvac_mode = HVACMode.OFF
    bt._control_needed_after_maintenance = True
    with (
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(f"{_CLIMATE}.run_valve_maintenance", AsyncMock()),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
    ):
        await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])
    bt.control_queue_task.put_nowait.assert_called_once_with(bt)
    assert bt._control_needed_after_maintenance is False


@pytest.mark.asyncio
async def test_control_kick_skipped_without_a_queue(bt):
    """Without a control queue the run finishes; the periodic tick catches up."""
    bt.control_queue_task = None
    with (
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(f"{_CLIMATE}.run_valve_maintenance", AsyncMock()),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
    ):
        await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])
    assert BetterThermostat.in_maintenance.fget(bt) is False
    assert bt.ignore_states is False


@pytest.mark.asyncio
async def test_failing_control_kick_is_traced(bt, caplog):
    """A control-kick failure other than a missing queue is recorded."""
    with (
        caplog.at_level(logging.DEBUG, logger=_CLIMATE),
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(f"{_CLIMATE}.run_valve_maintenance", AsyncMock()),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
        patch(
            f"{_CLIMATE}.request_control_cycle",
            MagicMock(side_effect=RuntimeError("boom")),
        ),
    ):
        await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])
    assert "control cycle request after maintenance failed" in caplog.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_failing_control_kick_does_not_mask_the_run(bt):
    """The run's own failure is what the caller sees, not the kick's."""
    with (
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(
            f"{_CLIMATE}.run_valve_maintenance",
            AsyncMock(side_effect=ValueError("unreachable TRV")),
        ),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
        patch(
            f"{_CLIMATE}.request_control_cycle",
            MagicMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(ValueError, match="unreachable TRV"),
    ):
        await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True], ids=["completed", "raised"])
async def test_a_trv_that_returned_during_maintenance_is_looked_for_after_it(bt, fails):
    """Returned TRVs are looked for once maintenance has ended.

    A TRV startup went ahead without that came back during maintenance was
    left alone then, and it does not necessarily report again soon.
    """
    maintenance_when_looked = []

    async def look():
        maintenance_when_looked.append(
            bt.kernel_state.maintenance.is_blocking(bt.clock.monotonic())
        )

    bt._initialize_arrived_trvs = look
    spawned = []
    bt._spawn_owned = lambda coro, name: spawned.append(coro)
    exercise = AsyncMock(side_effect=RuntimeError("boom") if fails else None)
    with (
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(f"{_CLIMATE}.run_valve_maintenance", exercise),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
    ):
        try:
            await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])
        except RuntimeError:
            assert fails
    for coro in spawned:
        await coro

    assert maintenance_when_looked == [False]


@pytest.mark.asyncio
async def test_a_maintenance_setpoint_waits_for_a_running_control_write(bt):
    """A setpoint the exercise writes goes out only once the control lock is free.

    A control cycle that was already running when maintenance started reads
    back the value its own setpoint write sent. A maintenance write landing
    in that window would be taken for the control write and watched as such.
    """
    bt._temp_lock = asyncio.Lock()
    writes = []

    async def _record(_bt, entity_id, temp):
        writes.append((entity_id, temp))

    async def _exercise(infos, *, set_temperature_fn, **kwargs):
        await set_temperature_fn("climate.trv", 30.0)

    with (
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(f"{_CLIMATE}.run_valve_maintenance", _exercise),
        patch(f"{_CLIMATE}.adapter_set_temperature", _record),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
    ):
        async with bt._temp_lock:
            run = asyncio.create_task(
                BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])
            )
            for _ in range(5):
                await asyncio.sleep(0)
            written_while_held = list(writes)
        await run

    assert written_while_held == []
    assert writes == [("climate.trv", 30.0)]


# ---------------------------------------------------------------------------
# run_valve_maintenance_service: what the user sees when it cannot run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_refuses_while_maintenance_runs(bt):
    """A request during a run is refused instead of returning silently."""
    bt.in_maintenance = True
    bt._run_valve_maintenance = AsyncMock()

    with pytest.raises(ServiceValidationError) as refused:
        await BetterThermostat.run_valve_maintenance_service(bt)

    assert refused.value.translation_key == "valve_maintenance_running"
    bt._run_valve_maintenance.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_reports_a_failed_run(bt, caplog):
    """A run that fails surfaces as an error and leaves its cause in the log."""
    bt.in_maintenance = False
    bt.real_trvs = {
        "climate.trv": Trv(
            entity_id="climate.trv", advanced={"valve_maintenance": True}
        )
    }
    bt._run_valve_maintenance = AsyncMock(side_effect=RuntimeError("adapter gone"))

    with pytest.raises(HomeAssistantError) as failed:
        await BetterThermostat.run_valve_maintenance_service(bt)

    assert failed.value.translation_key == "valve_maintenance_failed"
    assert isinstance(failed.value.__cause__, RuntimeError)
    assert "adapter gone" in caplog.text
