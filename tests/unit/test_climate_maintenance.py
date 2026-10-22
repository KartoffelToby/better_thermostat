"""Branch coverage for BetterThermostat._maintenance_tick.

_maintenance_tick decides, on each periodic tick, whether to run valve
maintenance now, postpone it, or schedule it far out.  These tests pin every
decision branch so the scheduling contract is locked down.  A second section
covers what _finalize_startup does for maintenance: the schedule it takes
up from the restored state, and the via device binding, which depends on how
many valves the setup has.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.core.fsm.maintenance import (
    MAX_RUN_S,
    MaintenanceIdle,
    MaintenanceRunning,
)
from tests.factories import ThermostatStandIn, make_trv, trv_record

_CLIMATE = "custom_components.better_thermostat.climate"
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for maintenance scheduling."""
    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock.contact_open = False
    mock.real_trvs = {"climate.trv": make_trv("climate.trv")}
    mock.clock = FakeClock(now_value=_NOW, monotonic_value=1000.0)
    mock.kernel_state = KernelState()
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
    bt._spawn_owned.assert_called_once()


@pytest.mark.asyncio
async def test_availability_check_exception_returns(bt):
    """An exception during the availability check aborts the tick safely."""
    with (
        patch(
            f"{_CLIMATE}.check_critical_entities",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt._spawn_owned.assert_not_called()


@pytest.mark.asyncio
async def test_already_in_maintenance_returns(bt):
    """A tick during an in-flight maintenance run does nothing."""
    bt.kernel_state = replace(
        bt.kernel_state,
        maintenance=MaintenanceRunning(
            running_since=bt.clock.monotonic(), next_due=_NOW
        ),
    )
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt._spawn_owned.assert_not_called()


@pytest.mark.asyncio
async def test_not_due_yet_returns(bt):
    """When the next run is still in the future, the tick is a no-op."""
    bt.kernel_state = KernelState(
        maintenance=MaintenanceIdle(next_due=_NOW + timedelta(hours=2))
    )
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt._spawn_owned.assert_not_called()


@pytest.mark.asyncio
async def test_window_open_postpones_one_hour(bt):
    """An open window postpones maintenance by one hour."""
    bt.window_open = True
    bt.contact_open = True
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
    ):
        await BetterThermostat._maintenance_tick(bt)
    assert bt.kernel_state.maintenance.next_due == _NOW + timedelta(hours=1)
    bt._spawn_owned.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode_attr", ["hvac_mode", "bt_hvac_mode"])
async def test_hvac_off_still_runs_maintenance(bt, mode_attr):
    """HVAC OFF does not postpone: a valve left shut over summer is the one that seizes."""
    setattr(bt, mode_attr, HVACMode.OFF)
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt._spawn_owned.assert_called_once()


@pytest.mark.asyncio
async def test_window_open_still_postpones_while_off(bt):
    """An open contact keeps postponing even now that OFF no longer does."""
    bt.bt_hvac_mode = HVACMode.OFF
    bt.contact_open = True
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
    ):
        await BetterThermostat._maintenance_tick(bt)
    assert bt.kernel_state.maintenance.next_due == _NOW + timedelta(hours=1)
    bt._spawn_owned.assert_not_called()


@pytest.mark.asyncio
async def test_no_enabled_trvs_schedules_far_future(bt):
    """With no TRV enabled for maintenance, the next run is pushed out a week."""
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.collect_maintenance_trvs", MagicMock(return_value=[])),
    ):
        await BetterThermostat._maintenance_tick(bt)
    assert bt.kernel_state.maintenance.next_due == _NOW + timedelta(days=7)
    bt._spawn_owned.assert_not_called()


@pytest.mark.asyncio
async def test_tick_keeps_a_stale_run_unblocked(bt):
    """A tick leaves a stale run as it is instead of restarting its clock.

    A RUNNING region older than MAX_RUN_S no longer blocks; a tick that
    reset running_since would turn it back into a blocking one.
    """
    stale_now = MAX_RUN_S + 1.0
    bt.clock = FakeClock(now_value=_NOW, monotonic_value=stale_now)
    bt.kernel_state = replace(
        bt.kernel_state,
        maintenance=MaintenanceRunning(
            next_due=_NOW - timedelta(hours=2), running_since=0.0
        ),
    )
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
    ):
        await BetterThermostat._maintenance_tick(bt)
    region = bt.kernel_state.maintenance
    assert isinstance(region, MaintenanceRunning)
    assert region.running_since == 0.0
    assert region.is_blocking(stale_now) is False


@pytest.mark.asyncio
async def test_due_and_enabled_dispatches_maintenance(bt):
    """When due, heating, window closed and TRVs enabled, maintenance is dispatched."""
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt._spawn_owned.assert_called_once()


# ---------------------------------------------------------------------------
# _finalize_startup: via device binding
# ---------------------------------------------------------------------------


def _startup_bt(advanced):
    """Minimal BetterThermostat mock for _finalize_startup."""

    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock.is_removed = False
    mock._degraded_grace_until = None
    mock.kernel_state = KernelState()
    mock.clock = FakeClock(now_value=_NOW, monotonic_value=1000.0)
    mock.real_trvs = {
        "climate.trv": trv_record(entity_id="climate.trv", advanced=advanced)
    }
    mock.entity_ids = ["climate.trv"]
    mock.all_trvs = None
    mock.all_entities = dict[str, None]()
    mock.sensor_entity_id = "sensor.room_temp"
    mock.humidity_sensor_entity_id = None
    mock.window_sensor_entity_id = None
    mock.door_sensor_entity_id = None
    mock.cooler_entity_id = None
    mock.outdoor_sensor_entity_id = None
    mock._async_unsub_state_changed = None
    mock._trigger_time = AsyncMock()
    mock._trigger_check_weather = AsyncMock()
    mock._startup_control_trvs = AsyncMock()
    mock._initialize_arrived_trvs = AsyncMock()
    mock._hand_over_room_sensor_state = AsyncMock()
    mock._hand_over_humidity_state = AsyncMock()
    mock._hand_over_contact_states = AsyncMock()
    mock.async_update_ha_state = AsyncMock()
    mock.hass = MagicMock()
    return mock


async def _run_finalize_startup(bt):
    """Run _finalize_startup with all external hooks patched."""
    with (
        patch(f"{_CLIMATE}.await_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.await_optional_sensors", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.async_track_time_interval", MagicMock()),
        patch(f"{_CLIMATE}.async_track_state_change_event", MagicMock()),
        patch(f"{_CLIMATE}.async_track_time_change", MagicMock()),
        patch(f"{_CLIMATE}.asyncio.sleep", AsyncMock()),
    ):
        await BetterThermostat._finalize_startup(bt)


def _restarted_bt(advanced, next_valve_maintenance):
    """A _finalize_startup mock whose restored state carries a schedule.

    Returns the mock and the list ``async_write_ha_state`` records the
    schedule of each published state in, in order.
    """
    bt = _startup_bt(advanced)
    attributes = (
        {}
        if next_valve_maintenance is None
        else {"next_valve_maintenance": next_valve_maintenance}
    )
    bt._saved_state = State("climate.bt", "heat", attributes)
    published: list[datetime | None] = []
    bt.async_write_ha_state = MagicMock(
        side_effect=lambda: published.append(bt.kernel_state.maintenance.next_due)
    )
    return bt, published


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "due_in", [timedelta(hours=30), -timedelta(hours=2)], ids=["ahead", "overdue"]
)
async def test_startup_takes_up_the_restored_schedule(due_in):
    """The date published before the restart is the one that holds after it.

    Already the first state the startup publishes carries it, so the date
    shown does not jump.
    """
    restored = _NOW + due_in
    bt, published = _restarted_bt({"valve_maintenance": True}, restored.isoformat())

    await _run_finalize_startup(bt)

    assert bt.kernel_state.maintenance.next_due == restored
    assert published[0] == restored


@pytest.mark.asyncio
async def test_startup_without_a_restored_schedule_rolls_the_startup_delay():
    """A first start, with nothing restored, draws a delay of 1 h to 5 d."""
    bt, published = _restarted_bt({"valve_maintenance": True}, None)

    await _run_finalize_startup(bt)

    due = bt.kernel_state.maintenance.next_due
    assert due is not None
    assert timedelta(hours=1) <= due - _NOW <= timedelta(days=5)
    assert published[0] == due


@pytest.mark.asyncio
async def test_startup_without_valve_maintenance_leaves_the_schedule_alone():
    """With maintenance off nothing is restored and nothing is rolled."""
    constructed = MaintenanceIdle(next_due=_NOW + timedelta(hours=7))
    bt, published = _restarted_bt({}, (_NOW + timedelta(hours=30)).isoformat())
    bt.kernel_state = replace(bt.kernel_state, maintenance=constructed)

    await _run_finalize_startup(bt)

    assert bt.kernel_state.maintenance == constructed


def _binding_bt(trv_confs):
    """Build a _finalize_startup mock with a configured all_trvs list."""
    bt = _startup_bt({})
    bt.all_trvs = trv_confs
    bt._unique_id = "bt_uid"
    bt._config_entry_id = "entry_1"
    return bt


@pytest.mark.asyncio
async def test_via_device_binding_runs_for_single_trv():
    """A single-TRV setup binds the BT device via that one valve."""
    bt = _binding_bt([{"trv": "climate.trv"}])
    with (
        patch(f"{_CLIMATE}.async_bind_trv_device", AsyncMock()) as bind,
        patch(f"{_CLIMATE}.async_unbind_trv_device", AsyncMock()) as unbind,
    ):
        await _run_finalize_startup(bt)

    bind.assert_awaited_once_with(bt.hass, "bt_uid", "climate.trv", "entry_1")
    unbind.assert_not_awaited()


@pytest.mark.asyncio
async def test_via_device_binding_skipped_and_cleared_for_multi_trv():
    """A multi-TRV setup skips via device binding and clears a stale link.

    The via device link is single-valued, so binding each TRV would just rewrite the
    same BT device row and leave it attached to the last valve only. A link
    that an earlier single-valve binding pass left behind is removed.
    """
    bt = _binding_bt([{"trv": "climate.trv"}, {"trv": "climate.second_trv"}])
    with (
        patch(f"{_CLIMATE}.async_bind_trv_device", AsyncMock()) as bind,
        patch(f"{_CLIMATE}.async_unbind_trv_device", AsyncMock()) as unbind,
    ):
        await _run_finalize_startup(bt)

    bind.assert_not_awaited()
    unbind.assert_awaited_once_with(bt.hass, "bt_uid", "entry_1")


@pytest.mark.asyncio
async def test_a_due_run_waits_while_the_devices_are_released(bt):
    """A due exercise is put off while the devices are the user's."""
    bt.devices_released = True
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv"]),
        ),
    ):
        await BetterThermostat._maintenance_tick(bt)
    bt._spawn_owned.assert_not_called()
    assert bt.kernel_state.maintenance.next_due == _NOW + timedelta(hours=1)


@pytest.mark.parametrize(
    ("released", "serviced", "next_due"),
    [
        pytest.param(set(), ["climate.trv", "climate.other"], None, id="none"),
        pytest.param({"climate.trv"}, ["climate.other"], None, id="one"),
        pytest.param(
            {"climate.trv", "climate.other"}, None, timedelta(hours=1), id="every"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_due_run_leaves_a_released_trv_out(bt, released, serviced, next_due):
    """A TRV released on its own is not exercised; the others are.

    A run that would exercise released TRVs only waits for them by the hour,
    as a run of a released room does.
    """
    bt.real_trvs = {
        entity_id: make_trv(entity_id) for entity_id in ("climate.trv", "climate.other")
    }
    bt.released_trvs = released
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.collect_maintenance_trvs",
            MagicMock(return_value=["climate.trv", "climate.other"]),
        ),
    ):
        await BetterThermostat._maintenance_tick(bt)
    if serviced is None:
        bt._spawn_owned.assert_not_called()
        assert bt.kernel_state.maintenance.next_due == _NOW + next_due
    else:
        bt._spawn_owned.assert_called_once()
        bt._run_valve_maintenance.assert_called_once_with(serviced)
