"""A number that is not a number never reaches a thermostat setting.

``number.set_value`` coerces its input with ``float``, so ``nan`` arrives as
a float, and every comparison with NaN is false, which lets it through Home
Assistant's range check. A setting that takes it on is lost: a NaN preset
clamps to the lowest temperature the next time the preset is activated, a
NaN gain turns the PID output into NaN.
"""

from dataclasses import replace
import math

from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.better_thermostat.utils.calibration.pid import build_pid_key
from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import (
    DOMAIN,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, SEPARATE_COOLER


def _number(hass, bt, suffix) -> str:
    """Return the entity_id of the entry's number whose unique id ends in ``suffix``."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{bt.unique_id}_{suffix}"
    )
    assert entity_id is not None, suffix
    return entity_id


async def _set(hass, entity_id, value) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("active", [True, False], ids=["active", "inactive"])
async def test_a_nan_preset_temperature_is_refused(hass, active):
    """A NaN preset temperature leaves the preset and the target as they were."""
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    if active:
        await bt.async_set_preset_mode("comfort")
        await hass.async_block_till_done()
    before = bt.preset_mgr.get_temperature("comfort")
    target = bt.heat_target_temperature

    await _set(hass, _number(hass, bt, "preset_comfort"), "nan")

    assert bt.preset_mgr.get_temperature("comfort") == before
    assert bt.heat_target_temperature == target


@pytest.mark.parametrize("device_role", [SEPARATE_COOLER], indirect=True)
async def test_a_nan_cooling_preset_temperature_is_refused(hass, device_role):
    """A NaN cooling preset temperature leaves the cooling preset as it was."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(device_role.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await bt.async_set_preset_mode("comfort")
    await hass.async_block_till_done()
    before = bt._preset_cool_temperatures.get("comfort")
    cool_target_temperature = bt.cool_target_temperature

    await _set(hass, _number(hass, bt, "preset_comfort_cool"), "nan")

    assert bt._preset_cool_temperatures.get("comfort") == before
    assert bt.cool_target_temperature == cool_target_temperature


@pytest.mark.parametrize("parameter", ["kp", "ki", "kd"])
async def test_a_nan_pid_gain_is_refused(hass, parameter):
    """A NaN PID gain leaves the gain as it was."""
    set_room_sensor(hass, 19.0)
    profile = replace(
        GENERIC_HEAT_TRV, calibration_mode=CalibrationMode.PID_CALIBRATION.value
    )
    await build_devices(hass, profile)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    number = _number(hass, bt, f"{profile.entity_id}_pid_{parameter}")
    before = float(hass.states.get(number).state)

    await _set(hass, number, "nan")

    assert bt.state_mgr is not None
    pid_state = bt.state_mgr.state.pid.get(build_pid_key(bt, profile.entity_id))
    stored = getattr(pid_state, f"pid_{parameter}") if pid_state else None
    assert stored is None or math.isfinite(stored)
    assert float(hass.states.get(number).state) == before
