"""Everything ``_finalize_startup`` registers, asserted as complete sets.

A test that checks one registration is present says nothing about the
one next to it. Startup registers periodic tasks, state subscriptions
and a daily time trigger, and each set is claimed here in full: the
assertion names every pair that must be registered for a configuration
and nothing else, so a deleted registration and an accidental duplicate
are both failures. Which pairs belong to a set depends on the
configuration, so each set is parametrized over the options that gate
its members.

The registration is the wiring only. That a callback does something
once it fires is a separate question, asked per callback in the file
that owns the callback's behaviour.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import (
    EXTERNAL_TEMPERATURE_KEEPALIVE_INTERVAL,
    BetterThermostat,
)
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.trv import Trv

_CLIMATE = "custom_components.better_thermostat.climate"
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

TRV_ID = "climate.trv"
SENSOR_ID = "sensor.room_temp"
HUMIDITY_ID = "sensor.room_humidity"
WINDOW_ID = "binary_sensor.window"
DOOR_ID = "binary_sensor.door"
COOLER_ID = "climate.cooler"
OUTDOOR_ID = "sensor.outdoor_temp"

# Every optional entity off and no balance, calibration or maintenance
# mode: the configuration whose sets hold only the unconditional members.
_BARE = {
    "advanced": {},
    "sensor_entity_id": SENSOR_ID,
    "humidity_sensor_entity_id": None,
    "window_id": None,
    "door_id": None,
    "cooler_entity_id": None,
    "outdoor_sensor": None,
}


def _startup_bt(**overrides):
    """A BetterThermostat stand-in for ``_finalize_startup``.

    Every attribute the run reads is set explicitly, including the ones
    that are ``None`` in the bare configuration: an attribute left to
    ``MagicMock`` would auto-create a truthy child and silently enable
    the branch it gates.
    """
    config = {**_BARE, **overrides}
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.is_removed = False
    mock.kernel_state = KernelState()
    mock.clock = MagicMock()
    mock.clock.now.return_value = _NOW
    mock.clock.monotonic.return_value = 1000.0
    mock.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID, advanced=config.pop("advanced"))}
    mock.entity_ids = [TRV_ID]
    mock.all_trvs = None
    mock.all_entities = []
    mock._async_unsub_state_changed = None
    mock._trigger_time = AsyncMock()
    mock._trigger_check_weather = AsyncMock()
    mock._startup_control_trvs = AsyncMock()
    mock.async_update_ha_state = AsyncMock()
    mock.hass = MagicMock()
    for name, value in config.items():
        setattr(mock, name, value)
    return mock


def _timer_target(registered):
    """The coroutine function a registered timer callback runs.

    Each tick goes to the tracker through a dispatcher that spawns the firing
    as work the entity owns, bound with ``partial``, so the callable handed to
    the tracker is that dispatcher rather than the tick itself. The set under
    test is which tick runs on which interval, so the binding is unwrapped
    here.
    """
    return registered.args[0]


class _Registrations:
    """What one ``_finalize_startup`` run handed to the event helpers."""

    def __init__(self, intervals, state_changes, time_changes):
        self.intervals = Counter(
            (_timer_target(call.args[1]), call.args[2])
            for call in intervals.call_args_list
        )
        self.state_changes = Counter(
            (tuple(call.args[1]), call.args[2]) for call in state_changes.call_args_list
        )
        self.time_changes = Counter(
            (_timer_target(call.args[1]), call.args[2:])
            for call in time_changes.call_args_list
        )


async def _run_finalize_startup(bt, *, shared_cooler=False):
    """Run ``_finalize_startup`` against recording event helpers."""
    track_interval = MagicMock()
    track_state = MagicMock()
    track_time = MagicMock()
    with (
        patch(f"{_CLIMATE}.await_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.await_optional_sensors", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.find_battery_entity", AsyncMock(return_value=None)),
        patch(f"{_CLIMATE}.request_control_cycle", MagicMock()),
        patch(
            f"{_CLIMATE}.dual_role_entity_id",
            MagicMock(return_value=TRV_ID if shared_cooler else None),
        ),
        patch(f"{_CLIMATE}.async_track_time_interval", track_interval),
        patch(f"{_CLIMATE}.async_track_state_change_event", track_state),
        patch(f"{_CLIMATE}.async_track_time_change", track_time),
        patch(f"{_CLIMATE}.asyncio.sleep", AsyncMock()),
    ):
        await BetterThermostat._finalize_startup(bt)
    return _Registrations(track_interval, track_state, track_time)


# ---------------------------------------------------------------------------
# The periodic tasks
# ---------------------------------------------------------------------------

# The four ticks that are the same for every configuration, whatever the
# five-minute ladder tick turns out to be. Weather is read hourly,
# the room temperature is re-sent to mirroring TRVs on its own interval,
# the outdoor EMA is advanced every minute and the reconciliation tick
# re-converges the devices every five.
_ALWAYS_ON_TICKS = (
    ("_trigger_check_weather", timedelta(hours=1)),
    ("_external_temperature_keepalive", EXTERNAL_TEMPERATURE_KEEPALIVE_INTERVAL),
    ("_async_update_ema_periodic", timedelta(minutes=1)),
    ("_reconcile_tick", timedelta(minutes=5)),
)


def _expected_intervals(bt, extra=(), ladder_tick="_availability_tick"):
    """The complete interval set for ``bt``, as a Counter of pairs.

    Every configuration carries one five-minute tick that advances the
    degradation ladder. Which of the two it is depends on whether the
    calibration mode also wants the control recompute.
    """
    return Counter(
        (getattr(bt, name), interval)
        for name, interval in (
            *_ALWAYS_ON_TICKS,
            (ladder_tick, timedelta(minutes=5)),
            *extra,
        )
    )


@pytest.mark.asyncio
async def test_a_bare_configuration_registers_only_the_unconditional_ticks():
    """No balance mode, no calibration mode, no maintenance: five ticks.

    The four unconditional ones plus the availability tick, which is what
    a configuration without a recompute gets in place of the control tick.
    """
    bt = _startup_bt()

    registered = await _run_finalize_startup(bt)

    assert registered.intervals == _expected_intervals(bt)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "advanced",
    [
        {"balance_mode": "pid"},
        {"balance_mode": "heuristic"},
        {"calibration_mode": "mpc_calibration"},
        {"calibration_mode": "pid_calibration"},
    ],
    ids=["balance_pid", "balance_heuristic", "calibration_mpc", "calibration_pid"],
)
async def test_a_balance_or_calibration_mode_adds_the_five_minute_control_tick(
    advanced,
):
    """A mode that recomputes gets the control tick in the ladder tick's place."""
    bt = _startup_bt(advanced=advanced)

    registered = await _run_finalize_startup(bt)

    assert registered.intervals == _expected_intervals(bt, ladder_tick="_trigger_time")


@pytest.mark.asyncio
async def test_valve_maintenance_adds_its_own_tick_and_nothing_else():
    """Maintenance is orthogonal: it brings one tick, not the control tick."""
    bt = _startup_bt(advanced={"valve_maintenance": True})

    registered = await _run_finalize_startup(bt)

    assert registered.intervals == _expected_intervals(
        bt, [("_maintenance_tick", timedelta(minutes=5))]
    )
    assert isinstance(bt.next_valve_maintenance, datetime)


@pytest.mark.asyncio
async def test_a_calibration_mode_and_maintenance_together_register_both_ticks():
    """Both gates open independently, and both ticks are then registered."""
    bt = _startup_bt(
        advanced={"calibration_mode": "mpc_calibration", "valve_maintenance": True}
    )

    registered = await _run_finalize_startup(bt)

    assert registered.intervals == _expected_intervals(
        bt, [("_maintenance_tick", timedelta(minutes=5))], ladder_tick="_trigger_time"
    )


@pytest.mark.asyncio
async def test_a_missing_room_sensor_cuts_the_interval_set_short():
    """The required-sensor guard returns before the later registrations.

    Weather, the availability tick and maintenance are registered above
    it; the keepalive, the EMA tick and the reconciliation tick are not.
    The entity runs with a partially wired timer set until the sensor is
    configured, and that is what the guard's error message reports.
    """
    bt = _startup_bt(sensor_entity_id=None, advanced={"valve_maintenance": True})

    registered = await _run_finalize_startup(bt)

    assert registered.intervals == Counter(
        {
            (bt._trigger_check_weather, timedelta(hours=1)): 1,
            (bt._availability_tick, timedelta(minutes=5)): 1,
            (bt._maintenance_tick, timedelta(minutes=5)): 1,
        }
    )


# ---------------------------------------------------------------------------
# The daily time trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_daily_outdoor_trigger_is_registered_only_with_an_outdoor_sensor():
    """Without an outdoor sensor there is no daily reading to take."""
    without = await _run_finalize_startup(_startup_bt())
    assert without.time_changes == Counter()

    bt = _startup_bt(outdoor_sensor=OUTDOOR_ID)
    with_sensor = await _run_finalize_startup(bt)
    assert with_sensor.time_changes == Counter({(bt._trigger_time, (5, 0, 0)): 1})


# ---------------------------------------------------------------------------
# The state subscriptions
# ---------------------------------------------------------------------------


def _expected_subscriptions(bt, extra=()):
    """The complete subscription set for ``bt``, as a Counter of pairs.

    The room sensor and the TRVs are subscribed for every configuration;
    everything else is gated on an optional entity being configured.
    """
    return Counter(
        [
            ((SENSOR_ID,), bt._trigger_temperature_change),
            (tuple(bt.entity_ids), bt._trigger_trv_change),
            *extra,
        ]
    )


@pytest.mark.asyncio
async def test_a_bare_configuration_subscribes_to_the_sensor_and_the_trvs():
    """Two subscriptions, one per required entity."""
    bt = _startup_bt()

    registered = await _run_finalize_startup(bt)

    assert registered.state_changes == _expected_subscriptions(bt)


@pytest.mark.asyncio
async def test_every_optional_entity_brings_exactly_one_subscription():
    """All optional entities configured at once: seven subscriptions."""
    bt = _startup_bt(
        humidity_sensor_entity_id=HUMIDITY_ID,
        window_id=WINDOW_ID,
        door_id=DOOR_ID,
        cooler_entity_id=COOLER_ID,
        outdoor_sensor=OUTDOOR_ID,
    )

    registered = await _run_finalize_startup(bt)

    assert registered.state_changes == _expected_subscriptions(
        bt,
        [
            ((HUMIDITY_ID,), bt._trigger_humidity_change),
            ((WINDOW_ID,), bt._trigger_window_change),
            ((DOOR_ID,), bt._trigger_door_change),
            ((COOLER_ID,), bt._trigger_cooler_change),
            ((OUTDOOR_ID,), bt._trigger_outdoor_change),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("option", "entity_id", "handler_name"),
    [
        ("humidity_sensor_entity_id", HUMIDITY_ID, "_trigger_humidity_change"),
        ("window_id", WINDOW_ID, "_trigger_window_change"),
        ("door_id", DOOR_ID, "_trigger_door_change"),
        ("cooler_entity_id", COOLER_ID, "_trigger_cooler_change"),
        ("outdoor_sensor", OUTDOOR_ID, "_trigger_outdoor_change"),
    ],
)
async def test_an_optional_entity_adds_its_own_handler_and_no_other(
    option, entity_id, handler_name
):
    """Each option is wired on its own, so one missing wire is visible."""
    bt = _startup_bt(**{option: entity_id})

    registered = await _run_finalize_startup(bt)

    assert registered.state_changes == _expected_subscriptions(
        bt, [((entity_id,), getattr(bt, handler_name))]
    )


@pytest.mark.asyncio
async def test_a_cooler_that_is_also_a_controlled_trv_is_subscribed_once():
    """One device reporting into two handlers reads its own writes back.

    The TRV handler is the one that survives, so the set holds the bare
    two even though a cooler is configured.
    """
    bt = _startup_bt(cooler_entity_id=TRV_ID)

    registered = await _run_finalize_startup(bt, shared_cooler=True)

    assert registered.state_changes == _expected_subscriptions(bt)


@pytest.mark.asyncio
async def test_an_existing_trv_subscription_is_not_registered_a_second_time():
    """A live subscription from an earlier run is kept, not duplicated."""
    bt = _startup_bt()
    bt._async_unsub_state_changed = MagicMock()

    registered = await _run_finalize_startup(bt)

    assert registered.state_changes == Counter(
        {((SENSOR_ID,), bt._trigger_temperature_change): 1}
    )
