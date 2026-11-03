"""A number that is not a number never reaches a thermostat setting.

``number.set_value`` coerces its input with ``float``, so ``nan`` arrives as
a float, and every comparison with NaN is false, which lets it through Home
Assistant's range check. A setting that takes it on is lost: a NaN preset
clamps to the lowest temperature the next time the preset is activated, a
NaN gain turns the PID output into NaN, a NaN valve cap stops capping.
"""

import copy
import math

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

from custom_components.better_thermostat.utils.calibration.pid import build_pid_key

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    TRV_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for_startup,
)


def _entry(**advanced) -> MockConfigEntry:
    """Return the harness entry with ``advanced`` merged into the TRV's options."""
    entry = make_entry()
    data = copy.deepcopy(dict(entry.data))
    data["thermostat"][0]["advanced"].update(advanced)
    return MockConfigEntry(
        domain=DOMAIN, version=entry.version, data=data, title=entry.title
    )


async def _started(hass, entry):
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


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
async def test_a_nan_preset_temperature_is_refused(hass, fake_trv, active):
    """A NaN preset temperature leaves the preset and the target as they were."""
    bt = await _started(hass, _entry())
    if active:
        await bt.async_set_preset_mode("comfort")
        await hass.async_block_till_done()
    before = bt.preset_mgr.get_temperature("comfort")
    target = bt.bt_target_temp

    await _set(hass, _number(hass, bt, "preset_comfort"), "nan")

    assert bt.preset_mgr.get_temperature("comfort") == before
    assert bt.bt_target_temp == target


class _Cooler(FakeTrvEntity):
    _attr_name = "fake cooler"
    _attr_hvac_modes = [HVACMode.COOL, HVACMode.OFF]

    def __init__(self):
        super().__init__()
        self._attr_hvac_mode = HVACMode.COOL


async def test_a_nan_cooling_preset_temperature_is_refused(hass):
    """A NaN cooling preset temperature leaves the cooling preset as it was."""
    cooler = _Cooler()
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [FakeTrvEntity(), cooler])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    entry = _entry()
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=entry.version,
        data={**entry.data, "cooler": cooler.entity_id},
        title=entry.title,
    )
    bt = await _started(hass, entry)
    await bt.async_set_preset_mode("comfort")
    await hass.async_block_till_done()
    before = bt._preset_cool_temperatures.get("comfort")
    cool_target = bt.bt_target_cooltemp

    await _set(hass, _number(hass, bt, "preset_comfort_cool"), "nan")

    assert bt._preset_cool_temperatures.get("comfort") == before
    assert bt.bt_target_cooltemp == cool_target


@pytest.mark.parametrize("parameter", ["kp", "ki", "kd"])
async def test_a_nan_pid_gain_is_refused(hass, fake_trv, parameter):
    """A NaN PID gain leaves the gain as it was."""
    bt = await _started(hass, _entry(calibration_mode="pid_calibration"))
    number = _number(hass, bt, f"{TRV_ID}_pid_{parameter}")
    before = float(hass.states.get(number).state)

    await _set(hass, number, "nan")

    pid_state = bt.state_mgr.state.pid.get(build_pid_key(bt, TRV_ID))
    stored = getattr(pid_state, f"pid_{parameter}") if pid_state else None
    assert stored is None or math.isfinite(stored)
    assert float(hass.states.get(number).state) == before


async def test_a_nan_valve_cap_is_refused(hass, fake_trv):
    """A NaN maximum valve opening leaves the cap as it was."""
    bt = await _started(hass, _entry(calibration="direct_valve_based"))
    number = _number(hass, bt, f"{TRV_ID}_valve_max_opening")
    await _set(hass, number, 40)

    await _set(hass, number, "nan")

    assert bt.real_trvs[TRV_ID].valve_max_opening == 40
