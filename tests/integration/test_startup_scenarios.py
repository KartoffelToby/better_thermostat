"""Startup and availability as a course of events, not as an end state.

The rest of the suite sets a device up ready and drives it. These tests drive
the timeline the device actually arrives on: an entity that is not there yet,
one that never turns up, one that disappears after startup and comes back,
and an entity id that is already taken.

What they pin is one line — the one between waiting and reporting. A device
that is merely late must be waited for silently, because a repair issue that
resolves itself teaches users to ignore repair issues. A device that is gone
must be named, because the thermostat that depends on it is doing nothing and
the only other symptom is silence.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.weather import (
    DOMAIN as WEATHER_DOMAIN,
    WeatherEntityFeature,
)
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.better_thermostat.calibration import effective_room_temperature
from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.control_mode import (
    LADDER_TICK_S,
    ControlMode,
    LadderParams,
)
from custom_components.better_thermostat.utils.const import (
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
)

from .conftest import (
    BT_ENTITY,
    CRITICAL_GRACE,
    DEGRADED_GRACE,
    DOMAIN,
    SENSOR_ID,
    WINDOW_ID,
    WRITE_BUDGET,
    assert_profile_adopted,
    assert_write_is,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, MQTT_OFFSET_TRV, TRV_ID

# A grace window that is already over by the time the first check runs, for
# the tests that are about what happens once waiting has to stop.
NO_GRACE = timedelta(seconds=0)

FORECAST_CALL_TIMEOUT = (
    "custom_components.better_thermostat.utils.weather.FORECAST_CALL_TIMEOUT"
)


def bt_issues(hass) -> list[str]:
    """Return the repair issues Better Thermostat currently holds open."""
    return sorted(
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    )


def missing_entity_issue(entity_id: str) -> str:
    """Return the id of the repair issue that names ``entity_id`` as missing."""
    return f"missing_entity_{entity_id}"


async def let_the_wait_loop_run(hass, rounds: int = 20) -> None:
    """Give the startup wait loop room to go round many times.

    Its own sleep is compressed by the harness, so a handful of passes
    through the event loop is a large number of iterations of the loop —
    enough that anything running once per iteration has run repeatedly.
    Used before asserting that something did *not* happen.
    """
    for _ in range(rounds):
        await hass.async_block_till_done()


@pytest.mark.parametrize(
    "fake_trv", [GENERIC_HEAT_TRV, MQTT_OFFSET_TRV], indirect=True, ids=profile_id
)
async def test_a_late_trv_is_waited_for_and_never_reported(hass, fake_trv):
    """A device that is still booting is waited for, not announced.

    A cloud-backed valve is routinely still unavailable by the time Home
    Assistant has finished starting, so a repair issue here would be a false
    one. The thermostat holds in startup, says nothing, and comes up as soon
    as the device does — with the device's own capabilities and setpoint read,
    which is the proof that it waited for the real thing rather than guessing.
    """
    set_room_sensor(hass, 19.0)
    fake_trv.set_available(False)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)

    await let_the_wait_loop_run(hass)
    bt = entry.runtime_data.climate
    assert bt.startup_running
    assert hass.states.get(BT_ENTITY).state == "unavailable"
    assert bt_issues(hass) == []

    fake_trv.set_available(True)

    bt = await wait_for_startup(hass, entry)
    assert bt_issues(hass) == []
    assert hass.states.get(BT_ENTITY).state == "heat"
    assert_profile_adopted(bt, fake_trv.profile)
    assert bt.heat_target_temperature == fake_trv.profile.target_temperature


async def test_a_trv_that_never_arrives_is_reported_once_the_grace_window_closes(
    hass, fake_trv
):
    """A device that stays away is named, and the thermostat keeps waiting.

    The wait loop has no end of its own, so the grace window is what decides
    when a device is late and when it is gone. Past that window the missing
    entity is named — while the loop carries on, because the device may still
    turn up and nothing here is worth giving up on.
    """
    set_room_sensor(hass, 19.0)
    fake_trv.set_available(False)
    entry = make_entry(fake_trv.profile)

    with patch(CRITICAL_GRACE, NO_GRACE):
        await setup_entry(hass, entry)
        assert await wait_for(hass, lambda: bt_issues(hass))

    bt = entry.runtime_data.climate
    assert bt_issues(hass) == [missing_entity_issue(TRV_ID)]
    assert bt.devices_errors == [TRV_ID]
    assert bt.startup_running
    assert hass.states.get(BT_ENTITY).state == "unavailable"


async def test_the_report_for_a_missing_trv_clears_when_it_finally_arrives(
    hass, fake_trv
):
    """A device that turns up after being reported takes its report with it.

    Announcing an outage is only half of it; a repair issue that outlives the
    outage is the thing the grace window exists to avoid, one step later.
    """
    set_room_sensor(hass, 19.0)
    fake_trv.set_available(False)
    entry = make_entry(fake_trv.profile)

    with patch(CRITICAL_GRACE, NO_GRACE):
        await setup_entry(hass, entry)
        assert await wait_for(hass, lambda: bt_issues(hass))

        fake_trv.set_available(True)
        bt = await wait_for_startup(hass, entry)

    assert bt_issues(hass) == []
    assert bt.devices_errors == []
    assert hass.states.get(BT_ENTITY).state == "heat"


async def test_a_trv_lost_after_startup_is_reported_and_cleared_on_return(
    hass, fake_trv
):
    """An outage after a good start is announced and withdrawn again.

    The thermostat is up and controlling when the device drops out, so this
    runs through the state-change path rather than the wait loop — the other
    of the two ways a device can go missing, and the one whose repair issue
    the user sees while the entity still reads ``heat``.
    """
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)

    with patch(CRITICAL_GRACE, NO_GRACE):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        assert bt_issues(hass) == []

        fake_trv.set_available(False)
        assert await wait_for(hass, lambda: bt_issues(hass))
        assert bt_issues(hass) == [missing_entity_issue(TRV_ID)]
        assert bt.devices_errors == [TRV_ID]

        fake_trv.set_available(True)
        assert await wait_for(hass, lambda: not bt_issues(hass))

    assert bt.devices_errors == []


async def test_an_optional_sensor_outage_annunciates_degraded_mode_and_recovers(
    hass, fake_trv
):
    """Losing a window sensor degrades the thermostat instead of stopping it.

    An optional sensor is optional because heating continues without it — so
    the outage has no other symptom, and degraded mode is the whole of what
    the user gets to see. It has to arrive and to leave again.
    """
    set_room_sensor(hass, 19.0)
    hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(fake_trv.profile, with_window=True)

    with patch(DEGRADED_GRACE, NO_GRACE):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        assert bt.degraded_mode is False
        assert bt_issues(hass) == []

        hass.states.async_set(WINDOW_ID, "unavailable")
        assert await wait_for(hass, lambda: bt.degraded_mode)
        assert bt.unavailable_sensors == [WINDOW_ID]
        assert bt_issues(hass) == [f"degraded_mode_{bt.device_name}"]
        assert hass.states.get(BT_ENTITY).attributes["degraded_mode"] is True

        hass.states.async_set(WINDOW_ID, "off")
        assert await wait_for(hass, lambda: not bt.degraded_mode)

    assert bt_issues(hass) == []
    assert bt.unavailable_sensors == []
    assert hass.states.get(BT_ENTITY).attributes["degraded_mode"] is False


async def test_a_rename_into_a_taken_id_lands_on_a_free_one(hass, fake_trv):
    """Renaming towards an id somebody else holds still moves the entity.

    The entity id is rebuilt from the configured name on every reload, so a
    rename can aim at an id another integration already occupies. Asking the
    registry for that id outright fails there, and the failure is quiet: the
    entity keeps its old id, and every automation written against the new
    name misses. Asking for the next free one instead is what makes the
    rename land at all.
    """
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile, name="Livingroom")
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    registry = er.async_get(hass)
    climate_key = ("climate", DOMAIN, entry.entry_id)
    assert registry.async_get_entity_id(*climate_key) == "climate.livingroom"

    squatter = registry.async_get_or_create(
        "climate", "other_integration", "squatter", suggested_object_id="bt_livingroom"
    )
    assert squatter.entity_id == "climate.bt_livingroom"

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "name": "BT Livingroom"}
    )
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)

    assert bt.entity_id not in ("climate.livingroom", squatter.entity_id)
    assert bt.entity_id.startswith("climate.bt_livingroom")
    assert registry.async_get_entity_id(*climate_key) == bt.entity_id
    assert hass.states.get(bt.entity_id).state == "heat"


async def test_a_taken_id_does_not_cost_the_thermostat_its_entity(hass, fake_trv):
    """Setting up against an occupied id still yields a working thermostat.

    An id that looks like Better Thermostat's can already be in the registry,
    held by another integration. Home Assistant's registry resolves that
    collision on its own, so no change in this repository can turn this test
    red today — it guards against Better Thermostat ever taking the choice
    away from the registry, because having no climate entity at all is
    indistinguishable from a setup that failed outright.
    """
    set_room_sensor(hass, 19.0)
    registry = er.async_get(hass)
    squatter = registry.async_get_or_create(
        "climate", "other_integration", "squatter", suggested_object_id="bt_test"
    )
    assert squatter.entity_id == BT_ENTITY

    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert bt.entity_id != BT_ENTITY
    assert registry.async_get_entity_id("climate", DOMAIN, entry.entry_id) == (
        bt.entity_id
    )
    assert hass.states.get(bt.entity_id).state == "heat"
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)


def _on_calibration_mode(profile, mode):
    """The same device, configured for one calibration mode."""
    return replace(profile, name=f"{profile.name}_{mode}", calibration_mode=mode)


@pytest.mark.parametrize(
    "fake_trv",
    [
        _on_calibration_mode(GENERIC_HEAT_TRV, CalibrationMode.PID_CALIBRATION.value),
        _on_calibration_mode(GENERIC_HEAT_TRV, DEFAULT_CALIBRATION_MODE.value),
    ],
    indirect=True,
    ids=profile_id,
)
async def test_a_room_sensor_that_returns_is_trusted_again_within_one_tick(
    hass, fake_trv
):
    """A sensor that comes back is believed at the next periodic evaluation.

    The ladder commits an upgrade only after the reading has been stable for
    ``up_stability_seconds``, which takes a second evaluation once that window has
    passed. A room that has settled publishes no state change to supply one,
    so the evaluation has to come from the periodic tick.

    Both calibration modes are driven, because the mode decides which of the
    two handlers carries the tick. A mode whose registration depended on the
    recompute would leave the entity on the degraded rung until the hourly
    weather tick, regulating on the fallback for up to an hour after the
    sensor was healthy again.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)

    # The ladder reads its own clock, so the test drives that rather than
    # waiting out the debounce windows.
    clock = FakeClock()
    bt.clock = clock
    clock.advance(10_000)
    stability_s = LadderParams().up_stability_seconds

    async def let_a_tick_fire(seconds):
        """Advance both clocks by ``seconds`` and run what falls due."""
        clock.advance(seconds)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds + 1))
        await hass.async_block_till_done()

    hass.states.async_set(SENSOR_ID, "unavailable")
    await hass.async_block_till_done()
    await let_a_tick_fire(stability_s)
    assert bt.kernel_state.control_mode.mode != ControlMode.OPTIMAL

    # One reading, then silence: the room is settled and the sensor has
    # nothing new to publish.
    set_room_sensor(hass, 18.0)
    await hass.async_block_till_done()
    assert bt.kernel_state.control_mode.mode != ControlMode.OPTIMAL

    await let_a_tick_fire(stability_s + 60)

    assert bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL


@pytest.mark.parametrize(
    "fake_trv",
    [
        _on_calibration_mode(GENERIC_HEAT_TRV, CalibrationMode.PID_CALIBRATION.value),
        _on_calibration_mode(GENERIC_HEAT_TRV, DEFAULT_CALIBRATION_MODE.value),
    ],
    indirect=True,
    ids=profile_id,
)
async def test_a_silent_room_sensor_moves_the_ladder_one_tick_after_each_window(
    hass, fake_trv
):
    """A sensor that goes quiet moves the ladder without any further event.

    The outage and the return each publish one state change, and that
    evaluation only starts the window. The commit needs a second evaluation
    once the window has passed, and in a settled room nothing but the
    periodic ladder tick supplies it. The rung therefore follows each window
    by at most one ``LADDER_TICK_S``, in both directions and for both kinds
    of calibration mode.

    Time moves in half-tick steps, so every tick that falls due runs at the
    moment it is due rather than all at once after a long jump.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    # The periodic ticks are registered at the very end of startup.
    await hass.async_block_till_done()

    clock = FakeClock(monotonic_value=bt.clock.monotonic())
    bt.clock = clock
    start = dt_util.utcnow()
    elapsed = 0.0

    async def let_time_pass(seconds):
        """Move both clocks on by ``seconds`` in half-tick steps."""
        nonlocal elapsed
        step = LADDER_TICK_S / 2
        target = elapsed + seconds
        while elapsed < target:
            clock.advance(step)
            elapsed += step
            async_fire_time_changed(hass, start + timedelta(seconds=elapsed))
            await hass.async_block_till_done()

    params = LadderParams()

    hass.states.async_set(SENSOR_ID, "unavailable")
    await hass.async_block_till_done()
    await let_time_pass(params.down_debounce_seconds + LADDER_TICK_S)
    assert bt.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK

    set_room_sensor(hass, 18.0)
    await hass.async_block_till_done()
    await let_time_pass(params.up_stability_seconds + LADDER_TICK_S)
    assert bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL


async def test_a_returning_room_sensor_restarts_the_filtered_temperature(
    hass, fake_trv
):
    """The filtered room temperature starts over from the returning reading.

    While the room runs on the TRV temperature, the minute tick keeps feeding
    the filter the last reading from before the outage, which says nothing
    about the room since. Blended into the returning reading it would hold
    the filtered temperature near the old value when the ladder hands the
    room back to its sensor, and show a warming trend that did not happen.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await hass.async_block_till_done()
    assert bt.room_temperature_filtered == 18.0

    clock = FakeClock(monotonic_value=bt.clock.monotonic())
    bt.clock = clock
    start = dt_util.utcnow()
    elapsed = 0.0

    async def let_time_pass(seconds):
        """Move both clocks on by ``seconds`` in half-tick steps."""
        nonlocal elapsed
        step = LADDER_TICK_S / 2
        target = elapsed + seconds
        while elapsed < target:
            clock.advance(step)
            elapsed += step
            async_fire_time_changed(hass, start + timedelta(seconds=elapsed))
            await hass.async_block_till_done()

    with patch(
        "custom_components.better_thermostat.events.temperature.monotonic",
        clock.monotonic,
    ):
        hass.states.async_set(SENSOR_ID, "unavailable")
        fake_trv._attr_current_temperature = 22.0
        fake_trv.async_set_context(Context())
        fake_trv.async_write_ha_state()
        await hass.async_block_till_done()
        await let_time_pass(30 * 60)
        assert bt.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK
        assert effective_room_temperature(bt) == 22.0

        set_room_sensor(hass, 22.0)
        assert await wait_for(hass, lambda: bt.room_temperature == 22.0)
        assert bt.room_temperature_filtered == 22.0

        await let_time_pass(LadderParams().up_stability_seconds + LADDER_TICK_S)
        assert bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL
        assert bt.room_temperature_filtered == 22.0
        assert bt.temperature_slope == 0.0


def degraded_issue_sensors(hass, bt) -> str | None:
    """Return the sensors the degraded-mode repair issue names, if it is open."""
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"degraded_mode_{bt.device_name}"
    )
    if issue is None or issue.translation_placeholders is None:
        return None
    return issue.translation_placeholders["sensors"]


def publish_room_sensor_state(hass, state: str | None) -> None:
    """Leave the room sensor out of the state machine or publish ``state``."""
    if state is not None:
        hass.states.async_set(SENSOR_ID, state)


async def start_without_room_sensor(hass, fake_trv, state: str | None = None):
    """Start a room whose sensor is missing until the grace window has closed.

    Both grace windows are over before the first check, so the room starts
    as soon as startup sees that the sensor is still missing, and the
    degraded-mode report is not held back either.
    """
    publish_room_sensor_state(hass, state)
    entry = make_entry(fake_trv.profile)
    with patch(CRITICAL_GRACE, NO_GRACE), patch(DEGRADED_GRACE, NO_GRACE):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
    return bt


async def set_room_target(hass: HomeAssistant, value: float) -> None:
    """Set a room target the TRV does not hold, so reaching it takes a write."""
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": value},
        blocking=True,
    )


@pytest.mark.parametrize("sensor_state", [None, "unavailable", "unknown"])
async def test_a_room_sensor_missing_at_boot_is_replaced_by_the_trv_temperature(
    hass, fake_trv, sensor_state
):
    """A dead room sensor at boot does not keep the room from starting.

    A battery sensor that died while Home Assistant was down, or a cloud
    sensor during an internet outage, is as gone at boot as it would be an
    hour later, and an hour later the room controls on the TRV's internal
    temperature. Once the grace window has closed, startup does the same:
    the room comes up, controls on the TRV temperature from the first cycle
    and names the missing sensor. With the room on the TRV temperature there
    is no offset between the two, so a new room target reaches the TRV as
    it is.
    """
    trv_temperature = fake_trv.profile.current_temperature

    bt = await start_without_room_sensor(hass, fake_trv, sensor_state)

    assert hass.states.get(BT_ENTITY).state == "heat"
    assert bt.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK
    assert bt.room_temperature == trv_temperature
    assert effective_room_temperature(bt) == trv_temperature
    assert hass.states.get(BT_ENTITY).attributes["current_temperature"] == (
        trv_temperature
    )
    writes_before = len(fake_trv.set_temperature_calls)
    with patch(WRITE_BUDGET, 0.0):
        await set_room_target(hass, 22.0)
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > writes_before
        )
    assert_write_is(fake_trv.set_temperature_calls[-1], 22.0, fake_trv.profile)
    assert bt.unavailable_sensors == [SENSOR_ID]
    assert await wait_for(hass, lambda: degraded_issue_sensors(hass, bt))
    assert degraded_issue_sensors(hass, bt) == SENSOR_ID


async def test_a_room_sensor_that_arrives_within_the_grace_window_starts_normally(
    hass, fake_trv
):
    """A room sensor that is only late is waited for, not replaced.

    A slow sensor integration looks exactly like a dead sensor at boot.
    Inside the grace window the room keeps waiting and commands nothing, so
    a sensor that turns up in time is the one the room starts on.
    """
    hass.states.async_set(SENSOR_ID, "unavailable")
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)

    await let_the_wait_loop_run(hass)
    bt = entry.runtime_data.climate
    assert bt.startup_running
    assert hass.states.get(BT_ENTITY).state == "unavailable"
    assert fake_trv.set_temperature_calls == []

    set_room_sensor(hass, 17.0)

    bt = await wait_for_startup(hass, entry)
    assert hass.states.get(BT_ENTITY).state == "heat"
    assert bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL
    assert bt.room_temperature == 17.0
    assert effective_room_temperature(bt) == 17.0
    assert bt.unavailable_sensors == []


async def test_a_room_sensor_that_reports_after_a_fallback_start_takes_over(
    hass, fake_trv
):
    """A room sensor that comes back after a fallback start is used again.

    The room started on the TRV temperature, so from then on it is in the
    same place as a room whose sensor dropped out at runtime: the reading is
    taken at once, and the ladder climbs back to the sensor once it has been
    stable for ``up_stability_seconds``.
    """
    bt = await start_without_room_sensor(hass, fake_trv, "unavailable")
    assert bt.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK

    clock = FakeClock()
    bt.clock = clock
    clock.advance(10_000)
    stability_s = LadderParams().up_stability_seconds

    set_room_sensor(hass, 17.0)
    assert await wait_for(hass, lambda: bt.room_temperature == 17.0)

    clock.advance(stability_s + 60)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=stability_s + 61)
    )
    await hass.async_block_till_done()

    assert bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL
    assert effective_room_temperature(bt) == 17.0
    assert await wait_for(hass, lambda: degraded_issue_sensors(hass, bt) is None)
    assert bt.unavailable_sensors == []


async def test_a_room_sensor_with_an_implausible_reading_at_boot_hands_the_room_to_the_trv(
    hass, fake_trv
):
    """A room sensor that reports nonsense at boot is no room temperature.

    Once the grace window has closed, startup takes the TRV temperature in
    its place, and the room keeps following the TRV rather than the one
    value it started with: a sensor that stays available with an implausible
    reading gives the ladder no more reason to climb back than a missing one
    does.
    """
    trv_temperature = fake_trv.profile.current_temperature
    set_room_sensor(hass, 126.5)
    entry = make_entry(fake_trv.profile)
    with patch(CRITICAL_GRACE, NO_GRACE), patch(DEGRADED_GRACE, NO_GRACE):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)

    assert bt.room_temperature == trv_temperature
    assert bt.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK

    # The ladder has been stepped on the real clock since startup, so the
    # controlled one continues from where that one stands.
    clock = FakeClock(monotonic_value=bt.clock.monotonic())
    bt.clock = clock
    stability_s = LadderParams().up_stability_seconds
    # The sensor keeps reporting nonsense for longer than the ladder takes
    # to trust a recovered sensor again.
    set_room_sensor(hass, 126.4)
    await hass.async_block_till_done()
    clock.advance(stability_s + 60)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=stability_s + 61)
    )
    await hass.async_block_till_done()
    assert bt.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK

    fake_trv._attr_current_temperature = trv_temperature + 2.0
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()
    assert await wait_for(
        hass, lambda: effective_room_temperature(bt) == trv_temperature + 2.0
    )


def publish_room_sensor_while_trvs_initialise(hass, state: str):
    """Publish ``state`` for the room sensor while startup writes to the TRVs.

    That is after startup has read the sensor and before it listens to it,
    so the state change itself is never handed to the room.
    """
    initialise_trvs = BetterThermostat._initialize_trvs

    async def publishing_first(bt, *args, **kwargs):
        hass.states.async_set(SENSOR_ID, state)
        return await initialise_trvs(bt, *args, **kwargs)

    return patch.object(BetterThermostat, "_initialize_trvs", publishing_first)


async def tick_until(hass, clock, seconds: float, predicate) -> bool:
    """Move time on in steps of ``seconds`` until ``predicate()`` holds.

    The ladder's clock and Home Assistant's timers move together, and the
    timers see the time step by step, so every periodic tick that falls
    due runs. The periodic ticks are registered at the very end of startup,
    after the point ``wait_for_startup`` waits for, so the first steps may
    find none registered yet.
    """
    start = dt_util.utcnow()
    elapsed = 0.0
    for _ in range(10):
        if predicate():
            return True
        clock.advance(seconds)
        elapsed += seconds
        async_fire_time_changed(hass, start + timedelta(seconds=elapsed + 1))
        await hass.async_block_till_done()
    return predicate()


async def test_a_room_sensor_that_reports_during_a_fallback_start_takes_over(
    hass, fake_trv
):
    """A reading published while startup runs is not waited out.

    Startup reads the sensor, finds nothing and takes the TRV temperature;
    the sensor reports while the TRVs are still being initialised, before
    the room listens to it. A settled sensor may not publish again for a
    long time, so the room has to take that reading once it listens.
    """
    trv_temperature = fake_trv.profile.current_temperature
    assert trv_temperature != 17.0
    hass.states.async_set(SENSOR_ID, "unavailable")
    entry = make_entry(fake_trv.profile)
    with (
        patch(CRITICAL_GRACE, NO_GRACE),
        patch(DEGRADED_GRACE, NO_GRACE),
        publish_room_sensor_while_trvs_initialise(hass, "17.0"),
    ):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)

    assert await wait_for(hass, lambda: bt.room_temperature == 17.0)
    assert hass.states.get(BT_ENTITY).attributes["current_temperature"] == 17.0

    clock = FakeClock(monotonic_value=bt.clock.monotonic())
    bt.clock = clock
    stability_s = LadderParams().up_stability_seconds
    assert await tick_until(
        hass,
        clock,
        stability_s + 60,
        lambda: bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL,
    )
    assert effective_room_temperature(bt) == 17.0


async def test_a_room_sensor_that_drops_out_during_startup_hands_the_room_to_the_trv(
    hass, fake_trv
):
    """A sensor lost while startup runs is noticed without a further report.

    Startup read a usable temperature, and the sensor went unavailable while
    the TRVs were still being initialised, before the room listened to it.
    The periodic evaluation of the ladder sees the loss on its own and moves
    the room onto the TRV temperature.
    """
    trv_temperature = fake_trv.profile.current_temperature
    set_room_sensor(hass, 17.0)
    entry = make_entry(fake_trv.profile)
    with publish_room_sensor_while_trvs_initialise(hass, "unavailable"):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
    assert bt.room_temperature == 17.0
    assert bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL

    clock = FakeClock(monotonic_value=bt.clock.monotonic())
    bt.clock = clock
    down_s = LadderParams().down_debounce_seconds
    assert await tick_until(
        hass,
        clock,
        down_s + 60,
        lambda: bt.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK,
    )
    assert effective_room_temperature(bt) == trv_temperature


async def test_a_weather_service_that_never_answers_does_not_hold_up_startup(
    hass, fake_trv
):
    """A stalled forecast call costs startup a bounded wait, not the entity.

    Startup asks the weather entity for its forecast before it reports the
    thermostat available. A cloud weather integration whose request hangs
    while the internet is down would otherwise keep the thermostat
    unavailable for as long as the request hangs.
    """
    set_room_sensor(hass, 19.0)
    weather_id = "weather.home"
    hass.states.async_set(
        weather_id,
        "sunny",
        {"temperature": 4.0, "supported_features": WeatherEntityFeature.FORECAST_DAILY},
    )

    async def get_forecasts_that_hang(call):
        await asyncio.Event().wait()

    hass.services.async_register(
        WEATHER_DOMAIN,
        "get_forecasts",
        get_forecasts_that_hang,
        supports_response=SupportsResponse.ONLY,
    )
    base = make_entry(fake_trv.profile)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=base.version,
        data={**base.data, "weather": weather_id},
        title=base.title,
    )

    with patch(FORECAST_CALL_TIMEOUT, timedelta(seconds=0.01)):
        await setup_entry(hass, entry)
        await wait_for_startup(hass, entry)

    assert hass.states.get(BT_ENTITY).state == "heat"
