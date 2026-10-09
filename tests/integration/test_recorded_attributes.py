"""The recorder keeps the thermostat's annunciation, not its controller telemetry.

The climate entity exposes the internals of its controller as state
attributes: PID terms, MPC estimates, cycle records and balance summaries.
Several change on nearly every write, so recording them gives every state
change a fresh attribute row. The live state keeps showing all of them.

The minute tick that keeps the room temperature filter converging writes a
state only when it moves a published value, so a room at a constant
temperature adds no rows.
"""

from dataclasses import replace
from datetime import timedelta
from functools import partial
import sys
from unittest.mock import patch

from homeassistant.components.recorder import history
from homeassistant.core import State
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.state_manager import CURRENT_VERSION

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    OUTDOOR_ID,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

PID = CalibrationMode.PID_CALIBRATION.value

# Telemetry the entity writes outside the PID and MPC v2 families.
_TELEMETRY = {
    "degraded_for_seconds",
    "temperature_slope_kelvin_per_min",
    "temp_slope_K_min",
    "calibration_balance",
    "heating_cycle_count",
    "heating_cycle_last",
    "heat_loss_cycle_count",
    "heat_loss_cycle_last",
    "heat_loss_stats",
    "heating_power_normalized",
    "heating_power_norm",
    "room_temperature_filtered",
    "external_temp_ema",
}


def _is_telemetry(key: str) -> bool:
    return key.startswith(("pid_", "mpc_v2_")) or key in _TELEMETRY


async def _recorded_rows(hass, entity_id: str) -> list[State]:
    """Return every state the recorder stored for ``entity_id`` in the last hour."""
    await async_wait_recording_done(hass)
    start = dt_util.utcnow() - timedelta(hours=1)
    states = await get_instance(hass).async_add_executor_job(
        partial(
            history.get_significant_states,
            hass,
            start,
            None,
            [entity_id],
            significant_changes_only=False,
        )
    )
    rows: list[State] = []
    for row in states.get(entity_id, []):
        assert isinstance(row, State), row
        rows.append(row)
    return rows


async def _recorded_attributes(hass, state) -> dict[str, object]:
    """Return the attributes the recorder stored for the live ``state``.

    Most writes of the climate entity change attributes only, so the query
    keeps insignificant rows and picks the one whose update time is the
    live state's.
    """
    rows = [
        row
        for row in await _recorded_rows(hass, state.entity_id)
        if row.last_updated_timestamp == state.last_updated_timestamp
    ]
    assert len(rows) == 1
    return dict(rows[0].attributes)


async def test_controller_telemetry_stays_out_of_the_recorder(hass):
    """PID terms show on the state but not in the recorded attributes."""
    profile = replace(GENERIC_HEAT_TRV, calibration_mode=PID)
    set_room_sensor(hass, 18.0)
    await build_devices(hass, profile)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await bt.async_set_temperature(temperature=22.0)
    assert await wait_for(
        hass, lambda: "pid_u" in hass.states.get(bt.entity_id).attributes
    )
    live = hass.states.get(bt.entity_id)

    recorded = await _recorded_attributes(hass, live)

    assert recorded["temperature"] == 22.0
    assert "control_mode" in recorded
    assert sorted(key for key in recorded if _is_telemetry(key)) == []


async def test_the_filtered_room_temperature_stays_out_of_the_recorder(hass):
    """The state shows the filter under both names; the recorder keeps neither."""
    set_room_sensor(hass, 18.0)
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await bt.async_set_temperature(temperature=22.0)
    await hass.async_block_till_done()
    live = hass.states.get(bt.entity_id)
    assert live.attributes["room_temperature_filtered"] == 18.0
    assert live.attributes["external_temp_ema"] == 18.0

    recorded = await _recorded_attributes(hass, live)

    assert recorded["temperature"] == 22.0
    assert "room_temperature_filtered" not in recorded
    assert "external_temp_ema" not in recorded


async def _started_with_a_fake_clock(hass, *, with_outdoor_sensor=False):
    """Start a room at a constant 20 °C; return the thermostat and its clock."""
    set_room_sensor(hass, 20.0)
    if with_outdoor_sensor:
        hass.states.async_set(
            OUTDOOR_ID,
            "5.0",
            {"unit_of_measurement": "°C", "device_class": "temperature"},
        )
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV, with_outdoor_sensor=with_outdoor_sensor)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await bt.async_set_temperature(temperature=21.0)
    await hass.async_block_till_done()
    clock = FakeClock(monotonic_value=bt.clock.monotonic())
    bt.clock = clock
    return bt, clock


async def _ema_tick_writes(hass, bt, clock: FakeClock, minutes: int) -> int:
    """Run the minute EMA tick ``minutes`` times; return the states it wrote.

    Other handlers may write in between, for example the ladder tick when
    the test's wall clock crosses a minute; only the EMA tick's own writes
    count.
    """
    tick_code = BetterThermostat._async_update_ema_periodic.__code__
    writes = 0
    write = bt.async_write_ha_state

    def counting_write() -> None:
        nonlocal writes
        if sys._getframe(1).f_code is tick_code:
            writes += 1
        write()

    with (
        patch(
            "custom_components.better_thermostat.events.temperature.monotonic",
            clock.monotonic,
        ),
        patch.object(bt, "async_write_ha_state", counting_write),
    ):
        for _ in range(minutes):
            clock.advance(60.0)
            await bt._async_update_ema_periodic()
            await hass.async_block_till_done()
    return writes


async def test_a_constant_room_writes_no_state_from_the_minute_tick(hass):
    """An hour at a constant temperature moves no value the minute tick publishes."""
    bt, clock = await _started_with_a_fake_clock(hass)
    assert bt.room_temperature_filtered == 20.0
    # The slope needs two ticks before it exists; publishing it is a change.
    assert await _ema_tick_writes(hass, bt, clock, 2) == 1

    writes = await _ema_tick_writes(hass, bt, clock, 60)

    assert bt.room_temperature_filtered == 20.0
    assert bt.temperature_slope == 0.0
    assert writes == 0


async def test_a_moving_room_temperature_still_reaches_the_ema_sensor(hass):
    """While the filter moves, every tick that changes it publishes the new value."""
    bt, clock = await _started_with_a_fake_clock(hass)
    set_room_sensor(hass, 21.0)
    await hass.async_block_till_done()

    writes = await _ema_tick_writes(hass, bt, clock, 60)

    assert bt.room_temperature_filtered == 21.0
    assert float(hass.states.get("sensor.bt_test_temperature_ema").state) == 21.0
    # The filter has a five-minute time constant: it settles on the last
    # hundredth well within the hour, and the ticks after that write nothing.
    assert 10 < writes < 60


async def test_a_degraded_room_keeps_counting_its_degraded_seconds(hass):
    """The minute ladder tick refreshes ``degraded_for_seconds`` on the state."""
    bt, clock = await _started_with_a_fake_clock(hass, with_outdoor_sensor=True)
    hass.states.async_set(OUTDOOR_ID, "unavailable")
    await hass.async_block_till_done()
    start = dt_util.utcnow()
    shown: list[int | None] = []
    for minute in range(1, 11):
        clock.advance(60.0)
        async_fire_time_changed(hass, start + timedelta(minutes=minute))
        await hass.async_block_till_done()
        shown.append(hass.states.get(BT_ENTITY).attributes["degraded_for_seconds"])

    counted = [value for value in shown if value is not None]
    assert bt.degraded_mode
    assert len(counted) >= 5
    assert counted == sorted(counted)
    assert len(set(counted)) == len(counted)


async def test_the_filter_restores_from_the_saved_runtime_state(hass, hass_storage):
    """A restart takes the filter from the runtime store, not from the recorder.

    The recorder holds no row of the entity and no restored attribute
    carries the filter, yet the average comes back instead of being seeded
    from the live reading.
    """
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV)
    key = f"{DOMAIN}_{entry.entry_id}_state"
    hass_storage[key] = {
        "version": CURRENT_VERSION,
        "minor_version": 1,
        "key": key,
        "data": {
            "version": CURRENT_VERSION,
            "mpc": {},
            "pid": {},
            "tpi": {},
            "thermal": {},
            "filters": {
                "external_temp_ema": 19.37,
                "temp_slope": None,
                "room_temperature_ema_recorded_at": dt_util.utcnow().timestamp(),
            },
        },
    }
    mock_restore_cache(hass, [State(BT_ENTITY, "heat", {})])
    await async_wait_recording_done(hass)
    assert await _recorded_rows(hass, BT_ENTITY) == []

    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert bt.room_temperature_filtered == pytest.approx(19.37, abs=0.01)
