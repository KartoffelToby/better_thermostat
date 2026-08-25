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

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.control_mode import (
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
    assert_profile_adopted,
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
    as the device does — with the device's own capabilities read, which is the
    proof that it waited for the real thing rather than guessing.
    """
    set_room_sensor(hass, 19.0)
    fake_trv.set_available(False)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)

    await let_the_wait_loop_run(hass)
    bt = hass.data[DOMAIN][entry.entry_id]["climate"]
    assert bt.startup_running
    assert hass.states.get(BT_ENTITY).state == "unavailable"
    assert bt_issues(hass) == []

    fake_trv.set_available(True)

    bt = await wait_for_startup(hass, entry)
    assert bt_issues(hass) == []
    assert hass.states.get(BT_ENTITY).state == "heat"
    assert_profile_adopted(bt, fake_trv.profile)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)


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

    bt = hass.data[DOMAIN][entry.entry_id]["climate"]
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
        entry, data={**entry.data, "name": "BT Livingroom"}
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
    ``up_stability_s``, which takes a second evaluation once that window has
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
    stability_s = LadderParams().up_stability_s

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
