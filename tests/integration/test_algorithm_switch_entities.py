"""Changing a thermostat's calibration algorithm leaves no dead entities behind.

Every algorithm brings its own diagnostic sensors, and some bring numbers and
switches. A change in the options reloads the entry; the entities of the
algorithm the thermostat no longer runs must leave the registry then, and
the ones of the algorithm it runs now must be there and alive.
"""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import RELOAD_AFTER_UPDATE_DELAY
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat import climate as climate_module
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationOutput,
)

from .conftest import (
    DOMAIN,
    build_devices,
    click_through_the_options,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, GROUP_OF_THREE, VALVE_TRV

PID = CalibrationMode.PID_CALIBRATION.value
MPC = CalibrationMode.MPC_CALIBRATION.value
DEFAULT = CalibrationMode.DEFAULT.value

_ALGORITHM_SENSOR_SUFFIXES = {
    PID: {"pid_kp", "pid_ki", "pid_kd", "pid_output", "pid_error"},
    MPC: {"virtual_temp", "mpc_gain", "mpc_loss", "mpc_ka"},
    DEFAULT: set(),
}


def _sensor_suffixes(hass, entry) -> set[str]:
    """Return the unique_id suffixes of the entry's algorithm sensors."""
    registry = er.async_get(hass)
    known = set().union(*_ALGORITHM_SENSOR_SUFFIXES.values())
    return {
        reg.unique_id.removeprefix(f"{entry.entry_id}_")
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain == "sensor"
        and reg.unique_id.removeprefix(f"{entry.entry_id}_") in known
    }


def _unavailable(hass, entry, domain: str) -> list[str]:
    """Return the entry's registered ``domain`` entities that are unavailable."""
    registry = er.async_get(hass)
    return sorted(
        reg.entity_id
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain == domain
        and (state := hass.states.get(reg.entity_id)) is not None
        and state.state == STATE_UNAVAILABLE
    )


async def _choose_algorithm(hass, entry, mode: str) -> None:
    """Pick ``mode`` in the options flow and wait for the reloaded thermostat."""
    await click_through_the_options(hass, entry, calibration_mode=mode)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("first", "then"),
    [(PID, MPC), (MPC, PID), (PID, DEFAULT), (MPC, DEFAULT), (DEFAULT, PID)],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_sensors_follow_the_chosen_algorithm(hass, first, then):
    """After an algorithm change the registry holds the new algorithm's sensors only."""
    set_room_sensor(hass, 19.0)
    profile = replace(GENERIC_HEAT_TRV, calibration_mode=first)
    await build_devices(hass, profile)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    assert _sensor_suffixes(hass, entry) == _ALGORITHM_SENSOR_SUFFIXES[first]

    await _choose_algorithm(hass, entry, then)

    assert _sensor_suffixes(hass, entry) == _ALGORITHM_SENSOR_SUFFIXES[then]
    assert _unavailable(hass, entry, "sensor") == []


async def test_a_sensor_the_user_enabled_stays_enabled_across_an_algorithm_change(hass):
    """Switching away from an algorithm and back keeps the user's choice.

    An algorithm's sensors start disabled and leave the registry while no TRV
    uses the algorithm. The one the user turned on comes back on, the one
    they left alone comes back off.
    """
    set_room_sensor(hass, 19.0)
    profile = replace(GENERIC_HEAT_TRV, calibration_mode=PID)
    await build_devices(hass, profile)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    registry = er.async_get(hass)
    output = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_pid_output"
    )
    gain = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_pid_kp")
    assert output is not None
    assert gain is not None
    assert hass.states.get(output) is None

    # Home Assistant reloads the entry a while after an entity is enabled.
    registry.async_update_entity(output, disabled_by=None)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1)
    )
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()
    assert hass.states.get(output) is not None

    await _choose_algorithm(hass, entry, MPC)
    assert registry.async_get(output) is None

    await _choose_algorithm(hass, entry, PID)

    assert registry.async_get(output).disabled_by is None
    assert hass.states.get(output) is not None
    assert registry.async_get(gain).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(gain) is None


def _algorithm_controls(hass, entry) -> set[str]:
    """Return the entity_ids of the entry's per-algorithm numbers and switches."""
    registry = er.async_get(hass)
    return {
        reg.entity_id
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain in ("number", "switch")
        and reg.unique_id.endswith(
            ("_pid_kp", "_pid_ki", "_pid_kd", "_pid_auto_tune", "_valve_max_opening")
        )
    }


_PID_CONTROLS = {
    "number.bt_test_pid_kp_proportional",
    "number.bt_test_pid_ki_integral",
    "number.bt_test_pid_kd_derivative",
    "switch.bt_test_pid_auto_tune",
}


@pytest.mark.parametrize(
    ("first", "then", "controls_before", "controls_after"),
    [
        (PID, MPC, _PID_CONTROLS, set()),
        (PID, DEFAULT, _PID_CONTROLS, set()),
        (MPC, PID, set(), _PID_CONTROLS),
    ],
)
async def test_the_numbers_and_switches_follow_the_chosen_algorithm(
    hass, first, then, controls_before, controls_after
):
    """After an algorithm change only the new algorithm's controls are registered."""
    set_room_sensor(hass, 19.0)
    profile = replace(GENERIC_HEAT_TRV, calibration_mode=first)
    await build_devices(hass, profile)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    assert _algorithm_controls(hass, entry) == controls_before

    await _choose_algorithm(hass, entry, then)

    assert _algorithm_controls(hass, entry) == controls_after
    assert _unavailable(hass, entry, "number") == []
    assert _unavailable(hass, entry, "switch") == []
    assert hass.states.get("switch.bt_test_child_lock") is not None


@pytest.mark.parametrize(
    ("first", "then"),
    [
        (CalibrationOutput.DIRECT_VALVE_BASED, CalibrationOutput.TARGET_TEMP_BASED),
        (CalibrationOutput.TARGET_TEMP_BASED, CalibrationOutput.DIRECT_VALVE_BASED),
    ],
)
async def test_the_valve_cap_follows_the_calibration_type(hass, first, then):
    """The maximum valve opening number exists exactly while the valve is driven."""
    set_room_sensor(hass, 19.0)
    profile = replace(VALVE_TRV, calibration=first.value)
    await build_devices(hass, profile)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    cap = {"number.bt_test_valve_max_opening"}
    driven = CalibrationOutput.DIRECT_VALVE_BASED
    assert _algorithm_controls(hass, entry) == (cap if first == driven else set())

    await click_through_the_options(hass, entry, calibration=then.value)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()

    assert _algorithm_controls(hass, entry) == (cap if then == driven else set())
    assert _unavailable(hass, entry, "number") == []


async def test_a_boot_removes_controls_left_by_an_earlier_algorithm(hass):
    """Controls registered for an algorithm the entry no longer runs leave on setup."""
    set_room_sensor(hass, 19.0)
    profile = replace(GENERIC_HEAT_TRV, calibration_mode=MPC)
    await build_devices(hass, profile)
    entry = make_entry(profile)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    trv = profile.entity_id
    stale = {
        ("number", f"{entry.entry_id}_{trv}_pid_kp"),
        ("number", f"{entry.entry_id}_{trv}_valve_max_opening"),
        ("switch", f"{entry.entry_id}_{trv}_pid_auto_tune"),
        ("sensor", f"{entry.entry_id}_pid_output"),
    }
    for domain, unique_id in stale:
        registry.async_get_or_create(domain, DOMAIN, unique_id, config_entry=entry)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    left = {
        (domain, unique_id)
        for domain, unique_id in stale
        if registry.async_get_entity_id(domain, DOMAIN, unique_id)
    }
    assert left == set()
    assert _sensor_suffixes(hass, entry) == _ALGORITHM_SENSOR_SUFFIXES[MPC]


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True)
async def test_a_setup_missing_a_trv_removes_none_of_its_entities(hass, trv_group):
    """A thermostat that could not set up one of its TRVs keeps that TRV's entities.

    The registry entries of a TRV the thermostat failed to build are not
    stale; they come back once it is built.
    """
    set_room_sensor(hass, 18.0)
    profiles = [replace(p, calibration_mode=PID) for p in trv_group.scenario.profiles]
    entry = make_entry(replace(trv_group.scenario, profiles=tuple(profiles)))
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    before = _registered(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    broken = trv_group.entities[1].entity_id
    load = climate_module.load_model_quirks

    async def _failing_for_one(bt, model, entity_id):
        if entity_id == broken:
            raise RuntimeError("quirk module broken")
        return await load(bt, model, entity_id)

    with patch.object(
        climate_module, "load_model_quirks", autospec=True, side_effect=_failing_for_one
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert before <= _registered(hass, entry)


def _registered(hass, entry) -> set[str]:
    """Return the unique_ids of the entry's registered entities."""
    registry = er.async_get(hass)
    return {
        reg.unique_id
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
