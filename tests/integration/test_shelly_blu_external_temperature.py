"""A Shelly BLU TRV on its own thermostat regulates on the room temperature BT writes."""

from dataclasses import replace
import logging
from unittest.mock import patch

from homeassistant.const import UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureConverter
import pytest

from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import SHELLY_BLU_THERMOSTAT_TRV, TRV_ID


@pytest.fixture(autouse=True)
def immediate_writes():
    """Exercise the device writes without waiting for ordinary write spacing."""
    with patch(WRITE_BUDGET, 0.0):
        yield


async def _start(hass, profile):
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


@pytest.mark.parametrize(
    "fake_trv", [SHELLY_BLU_THERMOSTAT_TRV], indirect=True, ids=profile_id
)
async def test_the_room_temperature_reaches_the_trv_and_the_target_passes_through(
    hass, fake_trv
):
    """The TRV regulates on the room sensor and on the target the user set.

    Under ``target_temp_based`` with ``no_calibration`` the target reaches
    the TRV unchanged, and the external temperature number follows every
    reading of the room sensor, so the TRV never runs without one.
    """
    number = fake_trv.blu_trv_external_temperature
    assert number is not None
    set_room_sensor(hass, 19.4)
    bt = await _start(hass, fake_trv.profile)
    assert not bt.real_trvs[TRV_ID].uses_valve_only_control

    assert await wait_for(hass, lambda: 19.4 in number.set_value_calls)

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": 22.0},
        blocking=True,
    )
    assert await wait_for(hass, lambda: 22.0 in fake_trv.set_temperature_calls)

    set_room_sensor(hass, 20.6)
    assert await wait_for(hass, lambda: number.set_value_calls[-1] == 20.6)


@pytest.mark.parametrize(
    "fake_trv",
    [replace(SHELLY_BLU_THERMOSTAT_TRV, system_unit=UnitOfTemperature.FAHRENHEIT)],
    indirect=True,
    ids=["fahrenheit_system"],
)
async def test_a_fahrenheit_system_writes_the_same_temperature(hass, fake_trv):
    """Home Assistant publishes the number in °F; the TRV still gets the room's °C."""
    number = fake_trv.blu_trv_external_temperature
    assert number is not None
    room_fahrenheit = TemperatureConverter.convert(
        21.3, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
    )
    set_room_sensor(hass, room_fahrenheit, UnitOfTemperature.FAHRENHEIT)
    await _start(hass, fake_trv.profile)

    assert await wait_for(hass, lambda: bool(number.set_value_calls))
    assert number.set_value_calls[-1] == pytest.approx(21.3, abs=0.01)


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(
            SHELLY_BLU_THERMOSTAT_TRV, calibration_mode=CalibrationMode.DEFAULT.value
        )
    ],
    indirect=True,
    ids=["calibrating"],
)
async def test_a_calibrating_trv_gets_no_external_temperature(hass, fake_trv):
    """A calibration and the TRV's own correction would compensate twice."""
    number = fake_trv.blu_trv_external_temperature
    assert number is not None
    set_room_sensor(hass, 19.4)
    await _start(hass, fake_trv.profile)
    set_room_sensor(hass, 20.6)
    await hass.async_block_till_done()

    assert number.set_value_calls == []


@pytest.mark.parametrize(
    "fake_trv",
    [replace(SHELLY_BLU_THERMOSTAT_TRV, blu_trv_external_temperature=False)],
    indirect=True,
    ids=["without_number"],
)
async def test_a_trv_without_the_number_is_reported_once(hass, fake_trv, caplog):
    """The TRV runs without a room temperature, and the log says so once."""
    caplog.set_level(logging.WARNING)
    set_room_sensor(hass, 19.4)
    await _start(hass, fake_trv.profile)
    set_room_sensor(hass, 20.6)
    await hass.async_block_till_done()
    set_room_sensor(hass, 21.8)
    await hass.async_block_till_done()

    warnings = [
        record
        for record in caplog.records
        if "no enabled external temperature number" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert TRV_ID in warnings[0].getMessage()
