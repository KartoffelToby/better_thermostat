"""The outdoor threshold and the tolerance are numbers that apply at once.

Both live in the entry's settings, the ones the options flow edits. Their
numbers show what the thermostat runs on and write a new value to the
settings; the thermostat takes it without a reload. Whichever way a value is
changed, and across a restart, the settings, the number and the thermostat
agree.
"""

import math
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest

from .conftest import (
    OUTDOOR_ID,
    WRITE_BUDGET,
    build_devices,
    click_through_the_options,
    counting_reloads,
    form_default,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import FAHRENHEIT_TRV, GENERIC_HEAT_TRV


@pytest.fixture(autouse=True)
def _no_write_budget():
    """Let a write follow the previous one without waiting out the budget."""
    with patch(WRITE_BUDGET, 0.0):
        yield


def _set_outdoor(hass, value: float, unit=UnitOfTemperature.CELSIUS) -> None:
    hass.states.async_set(OUTDOOR_ID, str(value), {"unit_of_measurement": unit})


async def _room(hass, *, unit_system=METRIC_SYSTEM, outdoor=15.0, off_temperature=20):
    """Set up a heated room with an outdoor sensor and let it settle."""
    hass.config.units = unit_system
    unit = unit_system.temperature_unit
    profile = (
        FAHRENHEIT_TRV if unit == UnitOfTemperature.FAHRENHEIT else GENERIC_HEAT_TRV
    )
    (device,) = await build_devices(hass, profile)
    set_room_sensor(hass, 66.0 if unit == UnitOfTemperature.FAHRENHEIT else 19.0, unit)
    _set_outdoor(hass, outdoor, unit)
    entry = make_entry(
        profile, with_outdoor_sensor=True, off_temperature=off_temperature
    )
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    # The entry is saved through the options flow once, so it holds every
    # setting in the form the flow writes, as an entry the flows created does.
    await click_through_the_options(hass, entry)
    bt = await wait_for_startup(hass, entry)
    return entry, bt, device


def _number_id(hass, entry, setting: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        NUMBER_DOMAIN, "better_thermostat", f"{entry.entry_id}_{setting}"
    )
    assert entity_id is not None, f"no {setting} number"
    return entity_id


def _shown(hass, entity_id: str) -> float:
    return float(hass.states.get(entity_id).state)


async def _set(hass, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _options_form_default(hass, entry, key: str):
    """Return what the options flow pre-fills ``key`` with, then abort the flow."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    try:
        return form_default(result, key)
    finally:
        hass.config_entries.options.async_abort(result["flow_id"])


async def test_lowering_the_threshold_stops_the_heating_without_a_reload(hass):
    """15 °C outside heats below 20 °C and stops at a threshold of 10 °C."""
    entry, bt, device = await _room(hass, outdoor=15.0, off_temperature=20)
    heating: bool = bt.call_for_heat
    assert heating is True
    threshold = _number_id(hass, entry, "off_temperature")
    assert _shown(hass, threshold) == 20.0

    async with counting_reloads(hass, entry) as reloads:
        await _set(hass, threshold, 10)
        assert await wait_for(
            hass,
            lambda: bt.call_for_heat is False and device.hvac_mode == HVACMode.OFF,
            timeout_seconds=3.0,
        ), f"heating did not stop: call_for_heat={bt.call_for_heat}"

        # Back above the outdoor temperature by more than the hysteresis band.
        await _set(hass, threshold, 20)
        assert await wait_for(
            hass,
            lambda: bt.call_for_heat is True and device.hvac_mode == HVACMode.HEAT,
            timeout_seconds=3.0,
        ), f"heating did not resume: call_for_heat={bt.call_for_heat}"

    assert reloads == []
    assert entry.runtime_data.climate is bt
    assert entry.options["off_temperature"] == 20


async def test_the_tolerance_number_and_the_options_flow_share_one_value(hass):
    """Number, options flow and restart: each one shows the value the last one set."""
    entry, bt, _ = await _room(hass)
    tolerance = _number_id(hass, entry, "tolerance")
    assert _shown(hass, tolerance) == 0.3

    async with counting_reloads(hass, entry) as reloads:
        await _set(hass, tolerance, 0.6)
        assert entry.options["tolerance"] == 0.6
        assert bt.tolerance == pytest.approx(0.6)
        assert await _options_form_default(hass, entry, "tolerance") == 0.6

        await click_through_the_options(hass, entry, tolerance=0.4)
        assert entry.options["tolerance"] == 0.4
        assert bt.tolerance == pytest.approx(0.4)
        assert _shown(hass, tolerance) == 0.4

    assert reloads == []
    assert entry.runtime_data.climate is bt

    await _set(hass, tolerance, 0.8)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    restarted = await wait_for_startup(hass, entry)

    assert restarted is not bt
    assert restarted.tolerance == pytest.approx(0.8)
    assert _shown(hass, tolerance) == 0.8
    assert await _options_form_default(hass, entry, "tolerance") == 0.8


async def test_the_options_flow_threshold_applies_without_a_reload(hass):
    """A threshold saved in the options flow stops the heating and shows on the number."""
    entry, bt, device = await _room(hass, outdoor=15.0, off_temperature=20)
    threshold = _number_id(hass, entry, "off_temperature")

    async with counting_reloads(hass, entry) as reloads:
        await click_through_the_options(hass, entry, off_temperature=12)
        assert await wait_for(
            hass,
            lambda: bt.call_for_heat is False and device.hvac_mode == HVACMode.OFF,
            timeout_seconds=3.0,
        )

    assert reloads == []
    assert _shown(hass, threshold) == 12.0


async def test_a_change_beside_a_live_setting_still_reloads(hass):
    """A tolerance saved together with another setting reloads the entry once."""
    entry, bt, _ = await _room(hass)
    tolerance = _number_id(hass, entry, "tolerance")

    async with counting_reloads(hass, entry) as reloads:
        await click_through_the_options(
            hass, entry, tolerance=0.5, target_temp_step="step_1_0"
        )
        restarted = await wait_for_startup(hass, entry)

    assert reloads == [entry.entry_id]
    assert restarted is not bt
    assert restarted.tolerance == pytest.approx(0.5)
    assert _shown(hass, tolerance) == 0.5


async def test_fahrenheit_values_are_stored_and_shown_in_fahrenheit(hass):
    """On a Fahrenheit system both numbers and the settings are in °F."""
    entry, bt, _ = await _room(
        hass, unit_system=US_CUSTOMARY_SYSTEM, outdoor=59.0, off_temperature=68
    )
    threshold = _number_id(hass, entry, "off_temperature")
    tolerance = _number_id(hass, entry, "tolerance")
    assert _shown(hass, threshold) == 68.0
    assert _shown(hass, tolerance) == 0.3
    assert bt.tolerance == pytest.approx(0.3 * 5 / 9)

    await _set(hass, threshold, 50)
    await _set(hass, tolerance, 0.9)

    assert entry.options["off_temperature"] == 50
    assert entry.options["tolerance"] == 0.9
    assert bt.off_temperature == pytest.approx(10.0)
    assert bt.tolerance == pytest.approx(0.5)
    assert _shown(hass, threshold) == 50.0
    assert _shown(hass, tolerance) == 0.9
    assert hass.states.get(tolerance).attributes["max"] == 18.0
    assert await wait_for(hass, lambda: bt.call_for_heat is False, timeout_seconds=3.0)


async def test_a_threshold_between_whole_degrees_is_stored_as_the_options_flow_does(
    hass,
):
    """The options flow stores whole degrees, so the number rounds to one."""
    entry, bt, _ = await _room(hass)
    threshold = _number_id(hass, entry, "off_temperature")

    await _set(hass, threshold, 17.6)

    assert entry.options["off_temperature"] == 18
    assert bt.off_temperature == 18.0
    assert _shown(hass, threshold) == 18.0
    assert await _options_form_default(hass, entry, "off_temperature") == 18


async def test_values_outside_the_range_or_not_a_number_change_nothing(hass):
    """Home Assistant refuses a value out of range; the number refuses NaN."""
    entry, bt, _ = await _room(hass)
    tolerance = _number_id(hass, entry, "tolerance")
    threshold = _number_id(hass, entry, "off_temperature")

    with pytest.raises(ServiceValidationError):
        await _set(hass, tolerance, -0.1)
    with pytest.raises(ServiceValidationError):
        await _set(hass, tolerance, 10.5)
    await _set(hass, tolerance, math.nan)
    await _set(hass, threshold, math.nan)

    assert entry.options["tolerance"] == 0.3
    assert entry.options["off_temperature"] == 20
    assert bt.tolerance == pytest.approx(0.3)
    assert bt.off_temperature == 20.0
    assert entry.state is ConfigEntryState.LOADED


async def test_the_threshold_number_exists_only_with_an_outdoor_source(hass):
    """Without an outdoor sensor or weather entity the threshold does nothing."""
    (device,) = await build_devices(hass, GENERIC_HEAT_TRV)
    set_room_sensor(hass, 19.0)
    entry = make_entry(GENERIC_HEAT_TRV)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    registry = er.async_get(hass)

    assert (
        registry.async_get_entity_id(
            NUMBER_DOMAIN, "better_thermostat", f"{entry.entry_id}_off_temperature"
        )
        is None
    )
    assert _number_id(hass, entry, "tolerance")
    assert device is not None
