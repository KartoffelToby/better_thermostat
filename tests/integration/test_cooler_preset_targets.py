"""The cooling target of a room with a cooler, while a preset is active.

A preset carries a heating and a cooling temperature. A manual change of
either target that does not match the active preset leaves the preset, so the
thermostat never reports a preset it is not running, and the target the user
set is the one that comes back after a reload. The cooling target saved on
entering a preset is given back on the way out to PRESET_NONE only while the
user has not set one of their own since.
"""

from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.const import ATTR_ENTITY_ID
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
from .device_profiles import SEPARATE_COOLER

pytestmark = pytest.mark.parametrize(
    "device_role", [SEPARATE_COOLER], indirect=True, ids=profile_id
)


async def _call(hass, service, data):
    """Drive one climate service call on the thermostat to completion."""
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: BT_ENTITY} | data, blocking=True
        )
        await hass.async_block_till_done()


async def _started_in_heat_cool(hass, scenario):
    """Set up a thermostat offering comfort and eco, running in HEAT_COOL."""
    set_room_sensor(hass, 22.0)
    data = dict(make_entry(scenario).data)
    data["presets"] = ["comfort", "eco"]
    entry = MockConfigEntry(domain=DOMAIN, version=18, data=data, title=data["name"])
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})
    return entry


def _read_published(hass):
    """Return the published preset and target pair."""
    attributes = hass.states.get(BT_ENTITY).attributes
    return (
        attributes["preset_mode"],
        attributes["target_temp_low"],
        attributes["target_temp_high"],
    )


def _cooler_targets(cooler):
    """Return every cooling setpoint written to the cooler, in order."""
    return [
        call.get("target_temp_high") if isinstance(call, dict) else call
        for call in cooler.set_temperature_calls
    ]


async def test_a_cooling_target_set_in_a_preset_survives_a_reload(hass, device_role):
    """A cooling target off the preset's own leaves the preset and persists.

    A card sends both bounds; the heating one unchanged at the preset's
    temperature. Only the cooling target departs from the preset, and that is
    a manual change like a heating one: the preset is left, and a reload
    brings the thermostat back on the target the user set, not on the
    preset's cooling temperature.
    """
    entry = await _started_in_heat_cool(hass, device_role.scenario)
    await _call(hass, "set_preset_mode", {"preset_mode": "comfort"})
    preset, heating_target, cooling_target = _read_published(hass)
    assert preset == "comfort"
    assert cooling_target != 22.0

    await _call(
        hass,
        "set_temperature",
        {"target_temp_low": heating_target, "target_temp_high": 22.0},
    )

    assert _read_published(hass) == ("none", heating_target, 22.0)

    cooler = device_role.cooler
    cooler.set_temperature_calls.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)

    assert _read_published(hass) == ("none", heating_target, 22.0)
    assert bt.cool_target_temperature == 22.0
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        set_room_sensor(hass, 23.5)
        await hass.async_block_till_done()
        assert await wait_for(hass, lambda: bool(cooler.set_temperature_calls))
    assert set(_cooler_targets(cooler)) == {22.0}


@pytest.mark.parametrize(
    "stored_pair",
    [None, (25.0, 24.0)],
    ids=["configured", "cooling_stored_below_heating"],
)
async def test_the_preset_pair_sent_back_keeps_the_preset(
    hass, device_role, stored_pair
):
    """A card resending the preset's own pair leaves the preset active.

    A preset whose stored cooling temperature lies at or below its heating
    temperature applies its cooling target one step above the heating one.
    That ordered pair is the preset's own as well.
    """
    entry = await _started_in_heat_cool(hass, device_role.scenario)
    if stored_pair is not None:
        bt = entry.runtime_data.climate
        assert bt is not None
        heating, cooling = stored_pair
        bt.preset_mgr.update_temperature("comfort", heating)
        bt._preset_cool_temperatures["comfort"] = cooling
    await _call(hass, "set_preset_mode", {"preset_mode": "comfort"})
    published = _read_published(hass)
    _, heating_target, cooling_target = published
    if stored_pair is not None:
        assert heating_target == stored_pair[0]
        assert cooling_target > heating_target

    await _call(
        hass,
        "set_temperature",
        {"target_temp_low": heating_target, "target_temp_high": cooling_target},
    )

    assert _read_published(hass) == published


async def test_the_stored_cooling_value_below_the_heating_one_is_a_manual_change(
    hass, device_role
):
    """A cooling target sent alone at the preset's stored value leaves the preset.

    The preset stores 24 °C for cooling below its 25 °C heating temperature,
    so selecting it applies a cooling target one step above 25 °C. A caller
    passing 24 °C alone lowers the heating target beneath it, which moves the
    heating target off the preset's own and leaves the preset. The climate
    service takes both bounds together, so the entity is called directly.
    """
    entry = await _started_in_heat_cool(hass, device_role.scenario)
    bt = entry.runtime_data.climate
    assert bt is not None
    bt.preset_mgr.update_temperature("comfort", 25.0)
    bt._preset_cool_temperatures["comfort"] = 24.0
    await _call(hass, "set_preset_mode", {"preset_mode": "comfort"})
    preset, heating_target, cooling_target = _read_published(hass)
    assert (preset, heating_target) == ("comfort", 25.0)
    assert cooling_target > 25.0

    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        await bt.async_set_temperature(target_temp_high=24.0)
        await hass.async_block_till_done()

    preset, heating_target, cooling_target = _read_published(hass)
    assert preset == "none"
    assert cooling_target == 24.0
    assert heating_target < 24.0


async def test_a_manual_pair_set_in_a_preset_outlasts_the_next_preset(
    hass, device_role
):
    """Leaving a preset by hand drops the cooling target saved on entering it.

    The pair the user sets while a preset is active is the manual pair from
    then on. A later preset and the return to PRESET_NONE give back that pair,
    not the cooling target that was in force before the first preset.
    """
    await _started_in_heat_cool(hass, device_role.scenario)
    await _call(
        hass, "set_temperature", {"target_temp_low": 20.0, "target_temp_high": 25.0}
    )
    await _call(hass, "set_preset_mode", {"preset_mode": "eco"})
    await _call(
        hass, "set_temperature", {"target_temp_low": 21.0, "target_temp_high": 26.0}
    )
    assert _read_published(hass) == ("none", 21.0, 26.0)

    await _call(hass, "set_preset_mode", {"preset_mode": "comfort"})
    await _call(hass, "set_preset_mode", {"preset_mode": "none"})

    assert _read_published(hass)[0] == "none"
    assert _read_published(hass)[2] == 26.0


async def test_reselecting_none_keeps_the_manual_cooling_target(hass, device_role):
    """Choosing PRESET_NONE while already in it does not move the cooling target."""
    await _started_in_heat_cool(hass, device_role.scenario)
    await _call(
        hass, "set_temperature", {"target_temp_low": 20.0, "target_temp_high": 25.0}
    )
    await _call(hass, "set_preset_mode", {"preset_mode": "eco"})
    await _call(
        hass, "set_temperature", {"target_temp_low": 21.0, "target_temp_high": 26.0}
    )
    await _call(
        hass, "set_temperature", {"target_temp_low": 21.0, "target_temp_high": 23.0}
    )

    await _call(hass, "set_preset_mode", {"preset_mode": "none"})

    assert _read_published(hass) == ("none", 21.0, 23.0)
