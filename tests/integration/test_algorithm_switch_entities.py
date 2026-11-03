"""Changing a thermostat's calibration algorithm leaves no dead entities behind.

Every algorithm brings its own diagnostic sensors, and some bring numbers and
switches. A change of the entry reloads it; the entities of the algorithm the
thermostat no longer runs must leave the registry then, and the ones of the
algorithm it runs now must be there and alive.
"""

import copy
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

from custom_components.better_thermostat import climate as climate_module
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    TRV_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for_startup,
)

PID = CalibrationMode.PID_CALIBRATION.value
MPC = CalibrationMode.MPC_CALIBRATION.value
DEFAULT = CalibrationMode.DEFAULT.value

_ALGORITHM_SENSOR_SUFFIXES = {
    PID: {"pid_kp", "pid_ki", "pid_kd", "pid_output", "pid_error"},
    MPC: {"virtual_temp", "mpc_gain", "mpc_loss", "mpc_ka"},
    DEFAULT: set(),
}


def _entry_in_mode(mode: str) -> MockConfigEntry:
    """Return the harness entry with every TRV on calibration ``mode``."""
    entry = make_entry()
    data = copy.deepcopy(dict(entry.data))
    for trv in data["thermostat"]:
        trv["advanced"]["calibration_mode"] = mode
    return MockConfigEntry(
        domain=DOMAIN, version=entry.version, data=data, title=entry.title
    )


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
    """Store ``mode`` on every TRV, reload, and wait for the thermostat."""
    data = copy.deepcopy(dict(entry.data))
    for trv in data["thermostat"]:
        trv["advanced"]["calibration_mode"] = mode
    hass.config_entries.async_update_entry(entry, data=data)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("first", "then"),
    [(PID, MPC), (MPC, PID), (PID, DEFAULT), (MPC, DEFAULT), (DEFAULT, PID)],
)
async def test_the_sensors_follow_the_chosen_algorithm(hass, fake_trv, first, then):
    """After an algorithm change the registry holds the new algorithm's sensors only."""
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = _entry_in_mode(first)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    assert _sensor_suffixes(hass, entry) == _ALGORITHM_SENSOR_SUFFIXES[first]

    await _choose_algorithm(hass, entry, then)

    assert _sensor_suffixes(hass, entry) == _ALGORITHM_SENSOR_SUFFIXES[then]
    assert _unavailable(hass, entry, "sensor") == []


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
    hass, fake_trv, first, then, controls_before, controls_after
):
    """After an algorithm change only the new algorithm's controls are registered."""
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = _entry_in_mode(first)
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
        (CalibrationType.DIRECT_VALVE_BASED, CalibrationType.TARGET_TEMP_BASED),
        (CalibrationType.TARGET_TEMP_BASED, CalibrationType.DIRECT_VALVE_BASED),
    ],
)
async def test_the_valve_cap_follows_the_calibration_type(hass, fake_trv, first, then):
    """The maximum valve opening number exists exactly while the valve is driven."""
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = make_entry()
    data = copy.deepcopy(dict(entry.data))
    for trv in data["thermostat"]:
        trv["advanced"]["calibration"] = first.value
    entry = MockConfigEntry(
        domain=DOMAIN, version=entry.version, data=data, title=entry.title
    )
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    cap = {"number.bt_test_valve_max_opening"}
    driven = CalibrationType.DIRECT_VALVE_BASED
    assert _algorithm_controls(hass, entry) == (cap if first == driven else set())

    data = copy.deepcopy(dict(entry.data))
    for trv in data["thermostat"]:
        trv["advanced"]["calibration"] = then.value
    hass.config_entries.async_update_entry(entry, data=data)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()

    assert _algorithm_controls(hass, entry) == (cap if then == driven else set())
    assert _unavailable(hass, entry, "number") == []


async def test_a_boot_removes_controls_left_by_an_earlier_algorithm(hass, fake_trv):
    """Controls registered for an algorithm the entry no longer runs leave on setup."""
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = _entry_in_mode(MPC)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    trv = TRV_ID
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


class _Head(FakeTrvEntity):
    """A head with its own name and entity id."""

    def __init__(self, name):
        super().__init__()
        self._attr_name = name


async def test_a_setup_missing_a_trv_removes_none_of_its_entities(hass):
    """A thermostat that could not set up one of its TRVs keeps that TRV's entities.

    The registry entries of a TRV the thermostat failed to build are not
    stale; they come back once it is built.
    """
    heads = [_Head("head a"), _Head("head b"), _Head("head c")]
    setup_test_component_platform(hass, CLIMATE_DOMAIN, heads)
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    template = _entry_in_mode(PID)
    thermostats = [
        {**copy.deepcopy(template.data["thermostat"][0]), "trv": head.entity_id}
        for head in heads
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=template.version,
        data={**template.data, "thermostat": thermostats},
        title=template.title,
    )
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    before = _registered(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    broken = heads[1].entity_id
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
