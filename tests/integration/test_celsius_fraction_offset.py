"""A calibration number that counts tenths of a degree Celsius.

A Z-Wave configuration parameter publishes its raw value in the unit the
parameter defines. The Eurotronic Spirit's temperature offset counts tenths
of a degree from -128 to 50 and reserves -128 for "use the external sensor",
so an offset of 2.5 K is the raw value 25. Home Assistant converts no value
of such a number, on a Celsius system or a Fahrenheit one.
"""

from dataclasses import replace

from homeassistant.const import UnitOfTemperature
import pytest

from .conftest import (
    assert_profile_adopted,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import TRV_ID, ZWAVE_SPIRIT_OFFSET_TRV, offset_number_id

SPIRIT_ON_A_FAHRENHEIT_SYSTEM = replace(
    ZWAVE_SPIRIT_OFFSET_TRV,
    name="zwave_spirit_offset_trv_on_a_fahrenheit_system",
    system_unit=UnitOfTemperature.FAHRENHEIT,
    precision=0.1,
)
"""The same Spirit on an instance that publishes temperatures in Fahrenheit.

Its climate entity publishes tenths of a degree, so its 19 °C reaches Better
Thermostat as 66.2 °F rather than rounded to 66 °F.
"""


@pytest.mark.parametrize(
    ("fake_trv", "room_reading", "room_unit"),
    [
        pytest.param(
            ZWAVE_SPIRIT_OFFSET_TRV, 21.5, UnitOfTemperature.CELSIUS, id="celsius"
        ),
        pytest.param(
            SPIRIT_ON_A_FAHRENHEIT_SYSTEM,
            70.7,
            UnitOfTemperature.FAHRENHEIT,
            id="fahrenheit",
        ),
    ],
    indirect=["fake_trv"],
)
async def test_the_offset_reaches_the_parameter_in_tenths_of_a_degree(
    hass, fake_trv, room_reading, room_unit
):
    """The device receives ten counts per Kelvin, whole, inside its own range.

    The device reads 19 °C and the room 21.5 °C, so the head is corrected by
    2.5 K and the parameter receives 25. The range is the parameter's, -127
    to 50 counts, since -128 stands for no offset, and the step is one count,
    a tenth of a Kelvin. The zero the startup writes is a zero count.
    """
    profile = fake_trv.profile
    set_room_sensor(hass, room_reading, room_unit)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, profile)
    trv = bt.real_trvs[TRV_ID]
    offset_number = fake_trv.offset_number
    assert offset_number is not None

    assert trv.local_calibration_step == pytest.approx(0.1)
    assert trv.min_local_calibration == pytest.approx(-12.7)
    assert trv.max_local_calibration == pytest.approx(5.0)
    assert await wait_for(hass, lambda: trv.last_calibration not in (None, 0.0)), (
        offset_number.set_value_calls
    )

    assert trv.last_calibration == pytest.approx(2.5, abs=0.05)
    written = offset_number.set_value_calls[-1]
    assert written == round(written)
    assert written == pytest.approx(trv.last_calibration * 10)
    assert offset_number.set_value_calls[:-1] == [0.0] * (
        len(offset_number.set_value_calls) - 1
    )
    assert float(hass.states.get(offset_number_id(profile)).state) == written


@pytest.mark.parametrize("fake_trv", [ZWAVE_SPIRIT_OFFSET_TRV], indirect=True)
async def test_a_parameter_set_to_its_reserved_value_reads_as_no_offset(hass, fake_trv):
    """The reserved -128 is no correction of -12.8 K, and no write carries it.

    A Spirit told to regulate on its external sensor holds -128. Better
    Thermostat reads that as no offset and writes the correction the room
    needs, which leaves the reserved value behind.
    """
    profile = fake_trv.profile
    offset_number = fake_trv.offset_number
    assert offset_number is not None
    offset_number._attr_native_value = -128.0
    offset_number.async_write_ha_state()
    set_room_sensor(hass, 21.5)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    trv = bt.real_trvs[TRV_ID]

    assert await wait_for(hass, lambda: trv.last_calibration not in (None, 0.0)), (
        offset_number.set_value_calls
    )

    assert trv.last_calibration == pytest.approx(2.5, abs=0.05)
    assert -128.0 not in offset_number.set_value_calls
    assert offset_number.set_value_calls[-1] == pytest.approx(25.0)
