"""The entities of a calibration mode follow the mode the calibration runs.

The calibration matches a stored mode name regardless of case. A hand-edited
entry that stores ``PID_Calibration`` therefore runs PID, and it gets the PID
numbers, switch and sensors an entry storing ``pid_calibration`` gets.
"""

from dataclasses import replace

from homeassistant.helpers import entity_registry as er
import pytest

from .conftest import (
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

_PID_ENTITY_SUFFIXES = {
    ("number", "_pid_kp"),
    ("number", "_pid_ki"),
    ("number", "_pid_kd"),
    ("switch", "_pid_auto_tune"),
    ("sensor", "_pid_output"),
}


def _pid_entities(hass, entry) -> set[tuple[str, str]]:
    """Return the (domain, suffix) pairs of the entry's registered PID entities."""
    registry = er.async_get(hass)
    return {
        (reg.domain, suffix)
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, suffix in _PID_ENTITY_SUFFIXES
        if reg.domain == domain and reg.unique_id.endswith(suffix)
    }


@pytest.mark.parametrize("stored_mode", ["pid_calibration", "PID_Calibration"])
async def test_a_pid_entry_gets_the_pid_entities_however_it_spells_the_mode(
    hass, stored_mode
):
    """Both spellings register the same PID entities."""
    set_room_sensor(hass, 19.0)
    profile = replace(GENERIC_HEAT_TRV, calibration_mode=stored_mode)
    await build_devices(hass, profile)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert _pid_entities(hass, entry) == _PID_ENTITY_SUFFIXES
