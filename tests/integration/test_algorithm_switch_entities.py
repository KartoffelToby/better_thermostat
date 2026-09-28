"""Changing a thermostat's calibration algorithm leaves no dead entities behind.

Every algorithm brings its own diagnostic sensors, and some bring numbers and
switches. A change of the entry reloads it; the entities of the algorithm the
thermostat no longer runs must leave the registry then, and the ones of the
algorithm it runs now must be there and alive.
"""

import copy

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup

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
