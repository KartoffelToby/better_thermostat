"""Coverage for BetterThermostat._run_valve_maintenance.

Focus is the state-flag contract around the valve exercise: in_maintenance and
ignore_states MUST always be released (even on error), otherwise the control
loop can stall.  Also covers the re-entry guard, reschedule, and control kick.
"""

import asyncio
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


@pytest.mark.asyncio
async def test_maintenance_setpoint_writes_stay_out_of_the_echo_list(bt):
    """The setpoints maintenance drives through the delegate are not echoes.

    Nothing confirms these writes, so remembering the device limits would read
    a later knob turn to the minimum or the maximum as BT's own value.
    """
    trv = bt.real_trvs["climate.trv"]
    trv.min_temp, trv.max_temp = 5.0, 30.0
    trv.remember_setpoint_written(21.0)
    trv.adapter = MagicMock(set_temperature=AsyncMock(return_value=True))
    bt.bt_target_temp_step = 0.5

    async def _exercise(infos, *, set_temperature_fn, **_):
        await set_temperature_fn("climate.trv", 30.0)
        await set_temperature_fn("climate.trv", 5.0)
        await set_temperature_fn("climate.trv", 21.0)

    with (
        patch(f"{_CLIMATE}.build_trv_snapshots", _snapshots()),
        patch(f"{_CLIMATE}.run_valve_maintenance", AsyncMock(side_effect=_exercise)),
        patch(f"{_CLIMATE}.compute_next_maintenance", MagicMock(return_value=_NEXT)),
    ):
        await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])

    assert trv.adapter.set_temperature.await_count == 3
    assert trv.last_temperature == 21.0
    assert trv.echo_setpoint_values() == [21.0]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True], ids=["completed", "raised"])
async def test_a_trv_that_returned_during_maintenance_is_looked_for_after_it(bt, fails):
    """Returned TRVs are looked for once maintenance has ended.

    A TRV startup went ahead without that came back during maintenance was
    left alone then, and it does not necessarily report again soon.
    """
    maintenance_when_looked = []
    bt._initialize_arrived_trvs = AsyncMock(
        side_effect=lambda: maintenance_when_looked.append(bt.in_maintenance)
    )
    spawned = []
    bt._spawn_owned = lambda coro, name=None: spawned.append(coro)
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
