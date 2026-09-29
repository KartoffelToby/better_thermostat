"""End-to-end tests: the room temperature falls back to the TRV sensor.

When the external room sensor stays unavailable, the thermostat controls on
the TRV-internal temperature instead of the last reading the sensor sent. A
frozen room temperature next to a TRV that keeps warming makes the
target-based calibration raise the setpoint step by step up to the TRV's
maximum.
"""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.const import ATTR_TEMPERATURE, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.utils.const import ROOM_SENSOR_FALLBACK_DELAY_S

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    TRV_ID,
    make_entry,
    setup_entry,
    wait_for,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"

# The two startup grace windows. Both are minutes long, so a test that wants
# to see what happens after one has closed shortens it rather than waiting.
CRITICAL_GRACE = (
    "custom_components.better_thermostat.climate.STARTUP_CRITICAL_GRACE_PERIOD"
)
DEGRADED_GRACE = (
    "custom_components.better_thermostat.climate.STARTUP_DEGRADED_GRACE_PERIOD"
)
NO_GRACE = timedelta(seconds=0)


def _room_sensor(hass, value):
    """Set the external room temperature sensor to ``value``."""
    hass.states.async_set(SENSOR_ID, value, {"unit_of_measurement": "°C"})


async def _trv_reports(hass, bt, fake_trv, temperature):
    """Let the fake TRV report a new internal temperature.

    The report waits until BT is between control cycles: while one runs, BT
    ignores TRV reports. It carries a context of its own, as a device report
    does; the entity would otherwise reuse the one of BT's last write, which
    BT ignores as its own echo. The previous report is moved out of the TRV's
    own anti-flicker window, which the test clock does not advance.
    """
    assert await wait_for(hass, lambda: not bt.ignore_states)
    trv = bt.real_trvs[TRV_ID]
    if trv.last_internal_sensor_change is not None:
        trv.last_internal_sensor_change -= timedelta(seconds=60)
    fake_trv._attr_current_temperature = temperature
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()
    await hass.async_block_till_done()


async def _advance(hass, seconds):
    """Run the timers that come due within ``seconds``."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _started_at_target(hass, fake_trv, target):
    """Start BT on a live room sensor and set the target temperature."""
    _room_sensor(hass, "18.0")
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": BT_ENTITY, ATTR_TEMPERATURE: target},
        blocking=True,
    )
    await hass.async_block_till_done()
    return bt


async def test_lost_sensor_hands_the_room_temperature_to_the_trv(hass, fake_trv):
    """After the debounce the TRV temperature drives the calibration."""
    bt = await _started_at_target(hass, fake_trv, 22.0)
    assert bt.cur_temp == 18.0

    _room_sensor(hass, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    await _advance(hass, ROOM_SENSOR_FALLBACK_DELAY_S + 1)

    # The room warmed up while the sensor was gone; the TRV sees it.
    fake_trv.set_temperature_calls.clear()
    await _trv_reports(hass, bt, fake_trv, 26.0)

    # Target minus room plus TRV is the target itself once both readings
    # are the TRV's; with the frozen 18 °C it would be 30 °C, the TRV maximum.
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    assert fake_trv.set_temperature_calls[-1] <= 22.0
    assert bt.cur_temp == 26.0
    assert hass.states.get(BT_ENTITY).attributes["current_temperature"] == 26.0


async def test_returning_sensor_takes_over_immediately(hass, fake_trv):
    """The first valid reading after the outage replaces the TRV value."""
    bt = await _started_at_target(hass, fake_trv, 22.0)

    _room_sensor(hass, STATE_UNKNOWN)
    await hass.async_block_till_done()
    await _advance(hass, ROOM_SENSOR_FALLBACK_DELAY_S + 1)
    await _trv_reports(hass, bt, fake_trv, 24.0)
    assert await wait_for(hass, lambda: bt.cur_temp == 24.0)

    # Well inside the external sensor's own debounce window.
    _room_sensor(hass, "21.0")
    await hass.async_block_till_done()
    assert await wait_for(hass, lambda: bt.cur_temp == 21.0)

    # The TRV no longer speaks for the room.
    await _advance(hass, 10)
    await _trv_reports(hass, bt, fake_trv, 25.0)
    assert bt.cur_temp == 21.0


async def test_brief_blip_keeps_the_room_sensor(hass, fake_trv):
    """A sensor that is back within the debounce never hands over."""
    bt = await _started_at_target(hass, fake_trv, 22.0)

    _room_sensor(hass, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    await _advance(hass, ROOM_SENSOR_FALLBACK_DELAY_S / 2)
    _room_sensor(hass, "18.0")
    await hass.async_block_till_done()

    await _advance(hass, ROOM_SENSOR_FALLBACK_DELAY_S)
    await _trv_reports(hass, bt, fake_trv, 26.0)
    assert bt.cur_temp == 18.0


async def test_outage_shorter_than_the_debounce_keeps_the_last_reading(hass, fake_trv):
    """Until the debounce has elapsed the last room reading stays in use."""
    bt = await _started_at_target(hass, fake_trv, 22.0)

    _room_sensor(hass, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    await _advance(hass, ROOM_SENSOR_FALLBACK_DELAY_S - 10)
    await _trv_reports(hass, bt, fake_trv, 26.0)
    assert bt.cur_temp == 18.0


def _degraded_issue_sensors(hass, bt) -> str | None:
    """Return the sensors the degraded-mode repair issue names, if it is open."""
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"degraded_mode_{bt.device_name}"
    )
    if issue is None or issue.translation_placeholders is None:
        return None
    return issue.translation_placeholders["sensors"]


async def _started_without_room_sensor(hass, sensor_state):
    """Start BT with a room sensor that is missing past the grace window.

    Both grace windows are over before the first check, so the room starts
    as soon as startup sees that the sensor is still missing, and the
    degraded-mode report is not held back either.
    """
    if sensor_state is not None:
        hass.states.async_set(SENSOR_ID, sensor_state)
    entry = make_entry()
    with patch(CRITICAL_GRACE, NO_GRACE), patch(DEGRADED_GRACE, NO_GRACE):
        await setup_entry(hass, entry)
        return await wait_for_startup(hass, entry)


@pytest.mark.parametrize("sensor_state", [None, STATE_UNAVAILABLE, STATE_UNKNOWN])
async def test_a_room_sensor_missing_at_boot_is_replaced_by_the_trv(
    hass, fake_trv, sensor_state
):
    """A dead room sensor at boot does not keep the room from starting.

    Once the grace window has closed, the room starts on the TRV's internal
    temperature, follows it as the TRV reports, and names the missing
    sensor, as it does when the sensor drops out at runtime.
    """
    bt = await _started_without_room_sensor(hass, sensor_state)

    assert hass.states.get(BT_ENTITY).state == "heat"
    assert bt.room_sensor_fallback is True
    assert bt.cur_temp == 19.5
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    assert bt.unavailable_sensors == [SENSOR_ID]
    assert _degraded_issue_sensors(hass, bt) == SENSOR_ID

    await _trv_reports(hass, bt, fake_trv, 24.0)
    assert await wait_for(hass, lambda: bt.cur_temp == 24.0)
    assert hass.states.get(BT_ENTITY).attributes["current_temperature"] == 24.0


async def test_a_room_sensor_that_arrives_within_the_grace_window_starts_normally(
    hass, fake_trv
):
    """A room sensor that is only late is waited for, not replaced.

    A slow sensor integration looks exactly like a dead sensor at boot.
    Inside the grace window the room keeps waiting and commands nothing, so
    a sensor that turns up in time is the one the room starts on.
    """
    hass.states.async_set(SENSOR_ID, STATE_UNAVAILABLE)
    entry = make_entry()
    await setup_entry(hass, entry)

    for _ in range(20):
        await hass.async_block_till_done()
    bt = hass.data[DOMAIN][entry.entry_id]["climate"]
    assert bt.startup_running
    assert hass.states.get(BT_ENTITY).state == "unavailable"
    assert fake_trv.set_temperature_calls == []

    _room_sensor(hass, "17.0")

    bt = await wait_for_startup(hass, entry)
    assert hass.states.get(BT_ENTITY).state == "heat"
    assert bt.room_sensor_fallback is False
    assert bt.cur_temp == 17.0
    assert bt.unavailable_sensors == []


async def test_a_room_sensor_that_reports_after_a_fallback_start_takes_over(
    hass, fake_trv
):
    """A room sensor that comes back after a fallback start is used at once.

    From then on the TRV no longer speaks for the room.
    """
    bt = await _started_without_room_sensor(hass, STATE_UNAVAILABLE)
    assert bt.room_sensor_fallback is True

    _room_sensor(hass, "21.0")
    await hass.async_block_till_done()
    assert await wait_for(hass, lambda: bt.cur_temp == 21.0)
    assert bt.room_sensor_fallback is False

    await _advance(hass, 10)
    await _trv_reports(hass, bt, fake_trv, 25.0)
    assert bt.cur_temp == 21.0
    assert await wait_for(hass, lambda: _degraded_issue_sensors(hass, bt) is None)


async def test_removed_sensor_entity_hands_the_room_temperature_to_the_trv(
    hass, fake_trv
):
    """A sensor entity that disappears is as lost as an unavailable one."""
    bt = await _started_at_target(hass, fake_trv, 22.0)

    hass.states.async_remove(SENSOR_ID)
    await hass.async_block_till_done()
    await _advance(hass, ROOM_SENSOR_FALLBACK_DELAY_S + 1)
    await _trv_reports(hass, bt, fake_trv, 26.0)

    assert bt.room_sensor_fallback is True
    assert bt.cur_temp == 26.0


async def test_a_room_that_starts_on_the_trv_follows_it(hass, fake_trv):
    """A room sensor with an implausible reading at boot hands the room to the TRV.

    Startup takes the TRV temperature in its place, and the room follows
    the TRV from then on until the sensor reports a usable value.
    """
    _room_sensor(hass, "126.5")
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.cur_temp == 19.5

    await _trv_reports(hass, bt, fake_trv, 24.0)
    assert await wait_for(hass, lambda: bt.cur_temp == 24.0)

    _room_sensor(hass, "21.0")
    await hass.async_block_till_done()
    assert await wait_for(hass, lambda: bt.cur_temp == 21.0)
    assert bt.room_sensor_fallback is False
