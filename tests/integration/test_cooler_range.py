"""Each channel of a room with a cooler is held to its own device's range.

A radiator head and an air conditioner rarely cover the same span: a head
typically runs 5 to 30 °C, an air conditioner 16 to 35 °C. The thermostat
publishes one range for both targets, and Home Assistant refuses any target
outside it before the thermostat sees the call, so the published range spans
both devices. Each target is then held to the device that carries it: the
heating target to the head's range, the cooling target to the cooler's.

The devices run through the real climate services, so a write outside a
device's own range fails here the way it fails on a real installation.
"""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest

from .conftest import (
    BT_ENTITY,
    COOLER_RESEND,
    DOMAIN,
    WRITE_BUDGET,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import (
    COOLER_ID,
    GENERIC_HEAT_TRV,
    HEAT_ONLY,
    RANGED_AC_COOLER,
    ROOM_AC_COOLER,
    RoleScenario,
)

WIDE_COOLER = replace(ROOM_AC_COOLER, name="wide_cooler", max_temp=35.0)
"""An air conditioner that accepts setpoints the head does not: up to 35 °C."""

HEATER_WITH_A_WIDE_COOLER = RoleScenario(
    name="heater_with_a_wide_cooler",
    trv=GENERIC_HEAT_TRV,
    cooler=WIDE_COOLER,
    cooler_entity_id=COOLER_ID,
)

HEATER_WITH_A_WIDE_RANGED_COOLER = RoleScenario(
    name="heater_with_a_wide_ranged_cooler",
    trv=GENERIC_HEAT_TRV,
    cooler=replace(RANGED_AC_COOLER, name="wide_ranged_cooler", max_temp=35.0),
    cooler_entity_id=COOLER_ID,
)

# Above the head's maximum of 30 °C and inside the cooler's range.
TARGET_ABOVE_THE_HEAD = 33.0


async def _call(hass, service, data):
    """Drive one climate service call on the thermostat to completion.

    Both write throttles are opened, so the write the call causes reaches the
    device within the test instead of after the startup's own write ages out.
    """
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: BT_ENTITY} | data, blocking=True
        )
        await hass.async_block_till_done()


# A room warm enough for the cooler to run at every cooling target these
# tests set. A cooler held off receives no setpoint, so a test that reads the
# write off the cooler needs one that cools.
ROOM_ABOVE_EVERY_COOLING_TARGET = 34.0


async def _started(hass, scenario, room_temperature=22.0):
    """Set up a thermostat on ``scenario`` and return it once started."""
    set_room_sensor(hass, room_temperature)
    entry = make_entry(scenario)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


def _published_range(hass):
    attributes = hass.states.get(BT_ENTITY).attributes
    return attributes["min_temp"], attributes["max_temp"]


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_WIDE_COOLER], indirect=True, ids=profile_id
)
async def test_the_published_range_spans_the_heater_and_the_cooler(hass, device_role):
    """The heater's minimum and the cooler's maximum bound the published range."""
    await _started(hass, device_role.scenario)

    assert _published_range(hass) == (5.0, 35.0)


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_WIDE_COOLER], indirect=True, ids=profile_id
)
async def test_a_cooling_target_above_the_heater_reaches_the_cooler(hass, device_role):
    """A cooling target only the cooler can hold is accepted and written to it."""
    bt = await _started(
        hass, device_role.scenario, room_temperature=ROOM_ABOVE_EVERY_COOLING_TARGET
    )
    await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})

    await _call(
        hass,
        "set_temperature",
        {"target_temp_low": 20.0, "target_temp_high": TARGET_ABOVE_THE_HEAD},
    )

    assert bt.cool_target_temperature == TARGET_ABOVE_THE_HEAD
    assert hass.states.get(BT_ENTITY).attributes["target_temp_high"] == (
        TARGET_ABOVE_THE_HEAD
    )
    cooler = device_role.cooler
    assert await wait_for(
        hass, lambda: TARGET_ABOVE_THE_HEAD in cooler.set_temperature_calls
    )


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_WIDE_COOLER], indirect=True, ids=profile_id
)
async def test_a_heating_target_above_the_heater_is_held_to_its_range(
    hass, device_role
):
    """A heating target the published range allows is clamped to the head's.

    The published range reaches up to the cooler's maximum, so Home Assistant
    lets a heating target of 33 °C through. The head accepts 30 °C at most, and
    the target is held there before anything is written to it.
    """
    bt = await _started(hass, device_role.scenario)
    await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})
    trv = device_role.thermostat
    trv.set_temperature_calls.clear()

    await _call(
        hass,
        "set_temperature",
        {"target_temp_low": TARGET_ABOVE_THE_HEAD, "target_temp_high": 34.0},
    )

    assert bt.heat_target_temperature == GENERIC_HEAT_TRV.max_temp
    assert hass.states.get(BT_ENTITY).attributes["target_temp_low"] == (
        GENERIC_HEAT_TRV.max_temp
    )
    assert await wait_for(hass, lambda: bool(trv.set_temperature_calls))
    assert max(trv.set_temperature_calls) <= GENERIC_HEAT_TRV.max_temp


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_WIDE_COOLER], indirect=True, ids=profile_id
)
async def test_a_cooling_preset_above_the_heater_reaches_the_cooler(hass, device_role):
    """A cooling preset takes the cooler's range, not the head's."""
    bt = await _started(
        hass, device_role.scenario, room_temperature=ROOM_ABOVE_EVERY_COOLING_TARGET
    )
    await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})
    number_id = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{bt.unique_id}_preset_comfort_cool"
    )
    assert number_id is not None

    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: number_id, "value": TARGET_ABOVE_THE_HEAD},
        blocking=True,
    )
    await _call(hass, "set_preset_mode", {"preset_mode": "comfort"})

    assert float(hass.states.get(number_id).state) == TARGET_ABOVE_THE_HEAD
    assert bt.cool_target_temperature == TARGET_ABOVE_THE_HEAD
    cooler = device_role.cooler
    assert await wait_for(
        hass, lambda: TARGET_ABOVE_THE_HEAD in cooler.set_temperature_calls
    )


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_WIDE_RANGED_COOLER], indirect=True, ids=profile_id
)
async def test_a_heating_target_below_the_cooler_keeps_the_band_inside_it(
    hass, device_role
):
    """The lower bound of a band written to the cooler stays inside its range.

    A cooler that takes a band is written the heating target as its lower
    bound. The heating target is held to the head's range, which reaches below
    the cooler's minimum, so the bound is raised onto that minimum: Home
    Assistant refuses a band that leaves the device's range.
    """
    bt = await _started(
        hass, device_role.scenario, room_temperature=ROOM_ABOVE_EVERY_COOLING_TARGET
    )
    await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})
    cooler = device_role.cooler

    await _call(
        hass, "set_temperature", {"target_temp_low": 10.0, "target_temp_high": 26.0}
    )

    assert bt.heat_target_temperature == 10.0
    assert await wait_for(
        hass,
        lambda: (
            {"target_temp_low": 16.0, "target_temp_high": 26.0}
            in cooler.set_temperature_calls
        ),
    )


@pytest.mark.parametrize("device_role", [HEAT_ONLY], indirect=True, ids=profile_id)
async def test_without_a_cooler_the_range_is_the_heaters(hass, device_role):
    """A room without a cooler publishes and holds the head's range alone."""
    await _started(hass, device_role.scenario)

    assert _published_range(hass) == (5.0, 30.0)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "set_temperature", {"temperature": TARGET_ABOVE_THE_HEAD})


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_WIDE_COOLER], indirect=True, ids=profile_id
)
async def test_a_cooling_target_that_lowers_a_preset_heating_target_survives_a_reload(
    hass, device_role
):
    """A cooling target below the preset's heating target leaves the preset.

    Comfort heats to 21 °C. A cooling target of 20 °C pushes the heating
    target one step below it, so the room no longer runs on Comfort's pair: the
    preset is left and the lowered heating target is the manual one. A
    thermostat still on Comfort would put 21 °C back when it restarts.

    Home Assistant's service schema takes the two bounds of a range together,
    so the cooling target alone reaches the thermostat only through a direct
    call of the entity method.
    """
    set_room_sensor(hass, 22.0)
    entry = make_entry(device_role.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})
    await _call(hass, "set_preset_mode", {"preset_mode": "comfort"})
    assert hass.states.get(BT_ENTITY).attributes["target_temp_low"] == 21.0

    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        await bt.async_set_temperature(target_temp_high=20.0)
        await hass.async_block_till_done()

    attributes = hass.states.get(BT_ENTITY).attributes
    assert attributes["preset_mode"] == "none"
    assert (attributes["target_temp_low"], attributes["target_temp_high"]) == (
        19.5,
        20.0,
    )

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    attributes = hass.states.get(BT_ENTITY).attributes
    assert attributes["preset_mode"] == "none"
    assert (attributes["target_temp_low"], attributes["target_temp_high"]) == (
        19.5,
        20.0,
    )
