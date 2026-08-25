"""Preset configuration across a restart and an options round trip.

A preset's temperature is edited on its number entity, but the target that
temperature produces is chosen by the climate entity — and the two platforms
come up in that order. Every test here spans that gap: it configures a preset,
takes the entry through a restart or the options form, and asks what the
thermostat runs on when it comes back.
"""

import json

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import State
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)
import voluptuous as vol

from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_PRESET_HEAT_TEMPERATURES,
)

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    counting_reloads,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

COMFORT_NUMBER = "number.bt_test_comfort"

# The built-in comfort temperature, and one the user would have to have set.
COMFORT_DEFAULT = 21.0
COMFORT_CONFIGURED = 22.5

# Enough steps for any entry this suite builds; a flow that wants more is stuck.
_MAX_FLOW_STEPS = 10


def _entry(hass, presets=("comfort", "eco")):
    """Build an entry offering ``presets``, without setting it up yet."""
    set_room_sensor(hass, 18.0)
    data = dict(make_entry(GENERIC_HEAT_TRV).data)
    data["presets"] = list(presets)
    return MockConfigEntry(domain=DOMAIN, version=18, data=data, title=data["name"])


async def _set_preset_temperature(hass, entity_id, value):
    """Edit a preset's temperature the way the number entity exposes it."""
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: entity_id, "value": value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _activate(hass, preset):
    """Switch the thermostat to ``preset``."""
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_preset_mode",
        {ATTR_ENTITY_ID: BT_ENTITY, "preset_mode": preset},
        blocking=True,
    )
    await hass.async_block_till_done()


def _target(hass):
    """Return the temperature the thermostat is currently driving to."""
    return hass.states.get(BT_ENTITY).attributes.get("temperature")


async def test_active_preset_keeps_its_temperature_across_a_reload(hass, fake_trv):
    """A reload leaves the thermostat on the preset temperature it was given.

    The climate platform is set up before the number platform that owns the
    preset temperatures, so the preset target is chosen while only the built-in
    defaults are loaded. A reload that does not carry the configured value over
    silently drops the thermostat onto the default.
    """
    entry = _entry(hass)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    await _set_preset_temperature(hass, COMFORT_NUMBER, COMFORT_CONFIGURED)
    await _activate(hass, "comfort")
    assert _target(hass) == COMFORT_CONFIGURED

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    assert hass.states.get(COMFORT_NUMBER).state == str(COMFORT_CONFIGURED)
    assert _target(hass) == COMFORT_CONFIGURED


async def test_active_preset_keeps_its_temperature_on_a_cold_start(hass, fake_trv):
    """A restart brings the thermostat back on the configured preset target.

    The state the entry comes up from is the one Home Assistant persisted
    before it went down, which is the only place the configured temperature
    exists at the moment the climate entity picks its target.
    """
    mock_restore_cache(
        hass,
        (
            State(
                BT_ENTITY,
                "heat",
                {
                    "temperature": COMFORT_CONFIGURED,
                    "preset_mode": "comfort",
                    "current_temperature": 18.0,
                    ATTR_STATE_PRESET_HEAT_TEMPERATURES: (
                        '{"comfort": 22.5, "eco": 19.0}'
                    ),
                },
            ),
            State(
                COMFORT_NUMBER, str(COMFORT_CONFIGURED), {"unit_of_measurement": "°C"}
            ),
        ),
    )
    entry = _entry(hass)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert _target(hass) == COMFORT_CONFIGURED


async def test_active_preset_survives_a_state_without_the_preset_map(hass, fake_trv):
    """A state predating the persisted preset map still restores the target.

    An entry coming up from a state that carries no preset map has the
    configured temperature only in the number entity, which is restored after
    the climate entity has already chosen its target.
    """
    mock_restore_cache(
        hass,
        (
            State(
                BT_ENTITY,
                "heat",
                {
                    "temperature": COMFORT_CONFIGURED,
                    "preset_mode": "comfort",
                    "current_temperature": 18.0,
                },
            ),
            State(
                COMFORT_NUMBER, str(COMFORT_CONFIGURED), {"unit_of_measurement": "°C"}
            ),
        ),
    )
    entry = _entry(hass)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert _target(hass) == COMFORT_CONFIGURED


@pytest.mark.parametrize(
    "carried",
    ["not json at all", '{"nosuchpreset": 17.0}'],
    ids=["unreadable", "unknown_preset"],
)
async def test_an_unusable_preset_map_falls_back_to_the_defaults(
    hass, fake_trv, carried
):
    """A map that cannot be read, or names a preset that does not exist.

    The state comes from the previous run and is not validated on the way in,
    so the restore has to survive both without taking the startup with it.
    """
    mock_restore_cache(
        hass,
        (
            State(
                BT_ENTITY,
                "heat",
                {
                    "preset_mode": "comfort",
                    "current_temperature": 18.0,
                    ATTR_STATE_PRESET_HEAT_TEMPERATURES: carried,
                },
            ),
        ),
    )
    entry = _entry(hass)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert _target(hass) == COMFORT_DEFAULT


async def test_a_preset_value_outside_the_range_keeps_the_preset_active(hass, fake_trv):
    """A stored preset the current range cannot hold is bounded, not abandoned.

    The stored value belongs to the user and stays as configured; only the
    target it produces is bounded. Reading that bound as a manual override
    would switch off the very preset that asked for it.
    """
    mock_restore_cache(
        hass,
        (
            State(
                BT_ENTITY,
                "heat",
                {
                    "temperature": 20.0,
                    "preset_mode": "comfort",
                    "current_temperature": 18.0,
                },
            ),
            State(COMFORT_NUMBER, "35.0", {"unit_of_measurement": "\u00b0C"}),
        ),
    )
    entry = _entry(hass)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert hass.states.get(BT_ENTITY).attributes["preset_mode"] == "comfort"
    assert _target(hass) == bt.max_temp
    assert hass.states.get(COMFORT_NUMBER).state == "35.0"


async def test_an_entry_carrying_legacy_options_reloads_once(hass, fake_trv):
    """An entry whose options were written by an earlier version reloads once.

    Every update of the entry reloads it, so clearing stale options in a second
    update costs a second reload — and the second one lands in the first one's
    startup, before it has restored what the thermostat was running on.
    """
    entry = _entry(hass)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options=dict(entry.data))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await _set_preset_temperature(hass, COMFORT_NUMBER, COMFORT_CONFIGURED)
    await _activate(hass, "comfort")

    async with counting_reloads(hass, entry) as reloads:
        await _click_through_the_options(hass, entry)

    assert reloads == [entry.entry_id]
    assert entry.options == {}
    assert hass.states.get(BT_ENTITY).attributes["preset_mode"] == "comfort"
    assert _target(hass) == COMFORT_CONFIGURED


async def test_preset_temperatures_are_carried_in_the_climate_state(hass, fake_trv):
    """The thermostat state carries the preset temperatures it runs on.

    The map is what a restart restores from, so an edit that does not reach the
    state is an edit the next start cannot see.
    """
    entry = _entry(hass)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    await _set_preset_temperature(hass, COMFORT_NUMBER, COMFORT_CONFIGURED)

    carried = hass.states.get(BT_ENTITY).attributes[ATTR_STATE_PRESET_HEAT_TEMPERATURES]

    assert json.loads(carried)["comfort"] == COMFORT_CONFIGURED


async def test_an_untouched_preset_still_uses_its_default(hass, fake_trv):
    """A preset nobody edited comes up on the built-in temperature."""
    entry = _entry(hass)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    await _activate(hass, "comfort")

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    assert _target(hass) == COMFORT_DEFAULT


async def _click_through_the_options(hass, entry):
    """Accept every pre-filled value on every step of the options flow."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    # A flow that keeps handing back the same step would otherwise hang the
    # suite instead of failing it. No step of this flow repeats, so the bound
    # is generous.
    for _ in range(_MAX_FLOW_STEPS):
        if result["type"] != "form":
            break
        submission = {}
        for marker in result["data_schema"].schema:
            description = getattr(marker, "description", None)
            if isinstance(description, dict) and "suggested_value" in description:
                submission[str(marker.schema)] = description["suggested_value"]
                continue
            default = getattr(marker, "default", None)
            if default is None or default is vol.UNDEFINED:
                continue
            value = default()
            if value is not vol.UNDEFINED:
                submission[str(marker.schema)] = value
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], submission
        )
    else:
        raise AssertionError(f"options flow did not finish: {result}")
    # A step that aborts also leaves the loop, and the caller would then read
    # the state the entry had before the flow ran.
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)


async def test_options_flow_keeps_the_active_preset_and_its_temperature(hass, fake_trv):
    """Passing through the options leaves the running preset alone.

    Writing the entry is what reloads it, and a reload that lands while the
    previous one is still starting up arrives before the preset has been
    restored. So the number of writes one pass costs is part of what this pins.
    """
    entry = _entry(hass)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    await _set_preset_temperature(hass, COMFORT_NUMBER, COMFORT_CONFIGURED)
    await _activate(hass, "comfort")

    await _click_through_the_options(hass, entry)

    assert hass.states.get(BT_ENTITY).attributes["preset_mode"] == "comfort"
    assert hass.states.get(COMFORT_NUMBER).state == str(COMFORT_CONFIGURED)
    assert _target(hass) == COMFORT_CONFIGURED
