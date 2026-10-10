"""The Silver rules of the Integration Quality Scale, held on a running instance.

Each test carries the rule it holds as a ``quality_rule`` marker;
``quality_scale.yaml`` records the rule's status and ``tests/quality_scale.py``
says how the two work together.
"""

from dataclasses import replace
from datetime import timedelta
import logging
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import flush_store

from custom_components.better_thermostat.utils.const import (
    SERVICE_RUN_VALVE_MAINTENANCE,
    CalibrationOutput,
)

from .conftest import (
    BT_ENTITY,
    CRITICAL_GRACE,
    DEGRADED_GRACE,
    DOMAIN,
    SENSOR_ID,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, VALVE_TRV

NO_GRACE = timedelta(seconds=0)
BT_LOGGER = "custom_components.better_thermostat"
MAINTENANCE_RUN = (
    "custom_components.better_thermostat.climate.BetterThermostat"
    "._run_valve_maintenance"
)


async def _started(hass, profile, **entry_options):
    """Build the device of ``profile``, set up an entry for it, wait for startup.

    Return the simulated device and the entry.
    """
    set_room_sensor(hass, 19.0)
    (device,) = await build_devices(hass, profile)
    entry = make_entry(profile, **entry_options)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    return device, entry


async def _listeners_once_core_saves_are_written(hass) -> dict[str, int]:
    """Return the bus listener counts after Home Assistant's own pending saves.

    A delayed save of the config entries or a registry holds a final-write
    listener until its timer writes the file, one to ten seconds later on the
    wall clock. Writing those saves first keeps the count independent of how
    long the test took; a store of Better Thermostat still counts.
    """
    for store in (
        hass.config_entries._store,
        dr.async_get(hass)._store,
        er.async_get(hass)._store,
    ):
        await flush_store(store)
    return hass.bus.async_listeners()


def _unavailable(hass, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state == STATE_UNAVAILABLE


def _available(hass, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state != STATE_UNAVAILABLE


@pytest.mark.quality_rule("action-exceptions")
async def test_an_action_the_thermostat_cannot_run_is_refused_by_name(hass):
    """A call that cannot apply raises a validation error, not a bare one.

    The thermostat has no valve with maintenance enabled, so a maintenance run
    has nothing to work on; the caller is told so in the error the frontend
    shows.
    """
    await _started(hass, GENERIC_HEAT_TRV)

    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_VALVE_MAINTENANCE,
            {"entity_id": BT_ENTITY},
            blocking=True,
        )

    assert refused.value.translation_domain == DOMAIN
    assert refused.value.translation_key == "valve_maintenance_not_enabled"


@pytest.mark.quality_rule("action-exceptions")
async def test_an_action_that_fails_while_running_raises_a_home_assistant_error(hass):
    """A run that breaks reaches the caller as a translated HomeAssistantError."""
    await _started(hass, replace(GENERIC_HEAT_TRV, valve_maintenance=True))

    with (
        patch(MAINTENANCE_RUN, side_effect=RuntimeError("valve stuck")),
        pytest.raises(HomeAssistantError) as failed,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_VALVE_MAINTENANCE,
            {"entity_id": BT_ENTITY},
            blocking=True,
        )

    assert not isinstance(failed.value, ServiceValidationError)
    assert failed.value.translation_key == "valve_maintenance_failed"


@pytest.mark.quality_rule("config-entry-unloading")
async def test_an_unloaded_entry_stops_driving_its_thermostat(hass):
    """After unloading, nothing the room reports reaches the device any more."""
    device, entry = await _started(hass, GENERIC_HEAT_TRV)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    writes = len(device.set_temperature_calls) + len(device.set_hvac_mode_calls)

    set_room_sensor(hass, 15.0)
    device.set_available(False)
    device.set_available(True)
    await hass.async_block_till_done()

    assert len(device.set_temperature_calls) + len(device.set_hvac_mode_calls) == (
        writes
    )


@pytest.mark.quality_rule("config-entry-unloading")
async def test_an_unloaded_entry_leaves_no_listener_behind(hass):
    """Loading and unloading again subscribes no more than the first time did.

    The first cycle also loads the platforms Better Thermostat depends on,
    which stay; every cycle after it has to give back what it took.
    """
    _, entry = await _started(hass, GENERIC_HEAT_TRV)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    after_first = await _listeners_once_core_saves_are_written(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert await _listeners_once_core_saves_are_written(hass) == after_first


@pytest.mark.quality_rule("entity-unavailable")
@pytest.mark.parametrize(
    ("profile", "entity_id"),
    [
        (GENERIC_HEAT_TRV, "switch.bt_test_child_lock"),
        (
            replace(VALVE_TRV, calibration=CalibrationOutput.DIRECT_VALVE_BASED.value),
            "number.bt_test_valve_max_opening",
        ),
    ],
    ids=["child lock", "valve cap"],
)
async def test_a_control_of_a_thermostat_that_is_gone_is_unavailable(
    hass, profile, entity_id
):
    """A control that writes to one thermostat cannot work while it is away.

    It shows as unavailable for as long as the thermostat does, and comes
    back with it.
    """
    with patch(CRITICAL_GRACE, NO_GRACE):
        device, _ = await _started(hass, profile)
    assert _available(hass, entity_id)

    device.set_available(False)

    assert await wait_for(
        hass, lambda: _unavailable(hass, entity_id), timeout_seconds=2.0
    )

    device.set_available(True)

    assert await wait_for(
        hass, lambda: _available(hass, entity_id), timeout_seconds=2.0
    )


@pytest.mark.quality_rule("entity-unavailable")
async def test_a_room_without_any_reachable_thermostat_is_unavailable(hass):
    """With every thermostat of the room gone there is nothing left to control.

    One reachable thermostat is enough to keep the room available; a room
    whose sensors fail stays available in degraded mode, so only the loss of
    every thermostat takes it down.
    """
    with patch(CRITICAL_GRACE, NO_GRACE):
        device, _ = await _started(hass, GENERIC_HEAT_TRV)
    assert _available(hass, BT_ENTITY)

    device.set_available(False)

    assert await wait_for(
        hass, lambda: _unavailable(hass, BT_ENTITY), timeout_seconds=2.0
    )

    device.set_available(True)

    assert await wait_for(
        hass, lambda: _available(hass, BT_ENTITY), timeout_seconds=2.0
    )


def _reports_about(caplog, entity_id: str) -> list[str]:
    """Return what Better Thermostat logged about ``entity_id``, and forget it.

    Debug lines are not counted: the rule is about what a user sees in the
    log at its default level.
    """
    reports = [
        f"{record.levelname} {record.getMessage()}"
        for record in caplog.records
        if record.name.startswith(BT_LOGGER)
        and record.levelno >= logging.INFO
        and entity_id in record.getMessage()
    ]
    caplog.clear()
    return reports


@pytest.mark.quality_rule("log-when-unavailable")
@pytest.mark.parametrize("lost", ["thermostat", "room sensor"])
async def test_an_outage_is_logged_when_it_starts_and_when_it_ends_only(
    hass, caplog, lost
):
    """A device that goes away is reported when it goes and when it is back.

    Every event in between runs the availability checks again; none of them
    may repeat the report, which a long outage would otherwise fill the log
    with.
    """
    with patch(CRITICAL_GRACE, NO_GRACE), patch(DEGRADED_GRACE, NO_GRACE):
        device, _ = await _started(hass, GENERIC_HEAT_TRV)
    gone = device.entity_id if lost == "thermostat" else SENSOR_ID
    caplog.set_level(logging.INFO, logger=BT_LOGGER)
    caplog.clear()

    if lost == "thermostat":
        device.set_available(False)
    else:
        hass.states.async_set(SENSOR_ID, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    went = _reports_about(caplog, gone)

    for tick in range(3):
        if lost == "thermostat":
            set_room_sensor(hass, 19.0 + tick / 10)
        else:
            hass.states.async_set(SENSOR_ID, STATE_UNAVAILABLE, {"tick": tick})
        await hass.async_block_till_done()
    repeated = _reports_about(caplog, gone)

    if lost == "thermostat":
        device.set_available(True)
    else:
        set_room_sensor(hass, 19.0)
    await hass.async_block_till_done()
    back = _reports_about(caplog, gone)

    assert went
    assert not any("available again" in report for report in went), went
    assert repeated == []
    assert len(back) == 1, back
    assert back[0].startswith("INFO "), back
    assert "available again" in back[0], back
