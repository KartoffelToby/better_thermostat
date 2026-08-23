"""Preset configuration across a restart and an options round trip.

A preset's temperature is edited on its number entity, but the target that
temperature produces is chosen by the climate entity — and the two platforms
come up in that order. Which preset modes exist at all comes from a third
place, the config entry the options flow writes. Every test here spans one of
those gaps: it configures presets, takes the entry through a restart or the
options form, and asks what the thermostat runs on when it comes back.
"""

import json

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import State
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)
import voluptuous as vol

from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_PRESET_HEAT_TEMPERATURES,
    CONF_PRESETS,
)
from custom_components.better_thermostat.utils.preset_manager import (
    DEFAULT_ENABLED_PRESETS,
)

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup

BT_ENTITY = "climate.bt_test"
COMFORT_NUMBER = "number.bt_test_comfort"

# The built-in comfort temperature, and one the user would have to have set.
COMFORT_DEFAULT = 21.0
COMFORT_CONFIGURED = 22.5

# Enough steps for any entry this suite builds; a flow that wants more is stuck.
_MAX_FLOW_STEPS = 10


def _entry(hass, presets=("comfort", "eco")):
    """Build an entry offering ``presets``, without setting it up yet."""
    hass.states.async_set(SENSOR_ID, "18.0", {"unit_of_measurement": "°C"})
    data = dict(make_entry().data)
    if presets is None:
        data.pop(CONF_PRESETS, None)
    else:
        data[CONF_PRESETS] = list(presets)
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


def _field_default(form, key):
    """Return the value a step's form pre-fills ``key`` with."""
    for marker in form["data_schema"].schema:
        if marker == key:
            assert marker.default is not vol.UNDEFINED, f"{key} carries no default"
            return marker.default()
    raise AssertionError(f"step {form['step_id']} publishes no field {key!r}")


def _submission(form, **overrides):
    """Return the submission a user accepting every pre-filled value sends."""
    out = {}
    for marker in form["data_schema"].schema:
        name = str(marker.schema)
        description = getattr(marker, "description", None)
        if isinstance(description, dict) and "suggested_value" in description:
            out[name] = description["suggested_value"]
            continue
        default = getattr(marker, "default", None)
        if default is None or default is vol.UNDEFINED:
            continue
        value = default()
        if value is not vol.UNDEFINED:
            out[name] = value
    return out | overrides


async def _click_through_the_options(hass, entry):
    """Accept every pre-filled value on every step of the options flow."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    forms = []
    # A flow that keeps handing back the same step would otherwise hang the
    # suite instead of failing it. No step of this flow repeats, so the bound
    # is generous.
    for _ in range(_MAX_FLOW_STEPS):
        if result["type"] != "form":
            break
        forms.append(result)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _submission(result)
        )
    else:
        raise AssertionError(f"options flow did not finish: {result}")
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    return forms


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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

    await _click_through_the_options(hass, entry)

    assert entry.options == {}
    assert hass.states.get(BT_ENTITY).attributes["preset_mode"] == "comfort"
    assert _target(hass) == COMFORT_CONFIGURED


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_options_flow_offers_the_presets_an_untouched_entry_runs_on(
    hass, fake_trv
):
    """An entry carrying no preset list keeps its presets through the form.

    A preset list that was never written is not an empty one: the thermostat
    comes up on the full default set. So that is the set the update form has to
    pre-fill, or a pass through the form that changes nothing submits a
    narrower list and takes every other preset away.
    """
    entry = _entry(hass, presets=None)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    running_on = set(hass.states.get(BT_ENTITY).attributes["preset_modes"])

    (user_step, _advanced) = await _click_through_the_options(hass, entry)

    assert set(_field_default(user_step, CONF_PRESETS)) == set(DEFAULT_ENABLED_PRESETS)
    assert set(hass.states.get(BT_ENTITY).attributes["preset_modes"]) == running_on


@pytest.mark.asyncio
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
