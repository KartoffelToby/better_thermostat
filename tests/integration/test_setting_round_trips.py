"""A setting the user chose is still that setting after a lifecycle event.

The question here is a round trip: does what the user configured survive being
taken down and brought back up? The three events a setting has to come through
are a reload, a cold start, and a pass through the options form that changes
nothing. All three restart the thermostat, and each restores from a different
place.

Each setting is configured to a value that is *not* the production default, so
a thermostat that falls back to the default is distinguishable from one that
restored correctly. A fixture on the default value cannot tell the two apart.

Two of the settings are not a user's direct choice and are here anyway. The
learned heating power is the thermostat's own accumulated knowledge, and a
restart that drops it costs a day of relearning. The window state is read back
off the sensor rather than persisted, and a thermostat that comes up believing
a standing-open window is shut heats into it until the sensor next changes.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.const import ATTR_ENTITY_ID
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    WINDOW_ID,
    click_through_the_options,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

PRESETS = ["comfort", "eco"]

# A learned power far enough from the 0.01 the tracker starts on that a
# thermostat falling back cannot be mistaken for one that restored.
LEARNED_HEATING_POWER = 0.042


async def _call(hass, service, data):
    """Drive one climate service call to completion."""
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: BT_ENTITY} | data, blocking=True
    )
    await hass.async_block_till_done()


async def _configure_target(hass, bt):
    """Set a target that is neither the default nor any preset temperature."""
    await _call(hass, "set_temperature", {"temperature": 23.5})


async def _configure_hvac_mode(hass, bt):
    """Turn the thermostat off, which is not the mode it starts in."""
    await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.OFF})


async def _configure_preset(hass, bt):
    """Put the thermostat on a preset, which it does not start on."""
    await _call(hass, "set_preset_mode", {"preset_mode": "comfort"})


async def _configure_heating_power(hass, bt):
    """Give the thermostat a learned heating power and persist it.

    The learning itself is unit-tested; what the round trip asks is whether
    a value the tracker arrived at is still there after a restart. The
    entity's own setter and its own save are used, so the value travels the
    path a learned one travels.
    """
    bt.heating_power = LEARNED_HEATING_POWER
    bt.schedule_save_state(delay_s=0)
    bt.async_write_ha_state()
    await hass.async_block_till_done()


async def _configure_window_open(hass, bt):
    """Open the window and wait for the thermostat to commit to it."""
    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(
        hass, lambda: hass.states.get(BT_ENTITY).attributes.get("window_open") is True
    )


def _close_the_window(hass):
    """Publish the closed window the entry is configured to watch."""
    hass.states.async_set(WINDOW_ID, "off")


def _read(attribute):
    """Return a reader for one attribute of the thermostat state."""
    return lambda hass: hass.states.get(BT_ENTITY).attributes.get(attribute)


@dataclass(frozen=True)
class Setting:
    """One user-configurable setting, and how to set and read it back.

    Attributes
    ----------
    name
        Identifier for the parametrized case.
    configure
        Coroutine putting the setting on a non-default value.
    read
        Callable returning the setting's current value from the state machine.
    expected
        The value ``read`` has to return once the thermostat is back up.
    default
        The value the thermostat comes up on when nobody configured the
        setting. Measured against a real entry below, so this cannot drift into
        fiction, and held apart from ``expected`` so a case cannot go blind.
    entry_options
        Keyword arguments for ``make_entry``, for a setting that needs a device
        wired to the entry before it can be set at all.
    entry_data
        Entry data written on top of what ``make_entry`` builds.
    prepare
        Callable publishing world state the entry needs before it is set up.
    """

    name: str
    configure: Callable
    read: Callable
    expected: Any
    default: Any
    entry_options: dict[str, Any] = field(default_factory=dict)
    entry_data: dict[str, Any] = field(default_factory=dict)
    prepare: Callable | None = None


SETTINGS = [
    Setting(
        name="target_temperature",
        configure=_configure_target,
        read=_read("temperature"),
        expected=23.5,
        default=5.0,
    ),
    Setting(
        name="hvac_mode",
        configure=_configure_hvac_mode,
        read=lambda hass: hass.states.get(BT_ENTITY).state,
        expected=HVACMode.OFF,
        default=HVACMode.HEAT,
    ),
    Setting(
        name="preset_mode",
        configure=_configure_preset,
        read=_read("preset_mode"),
        expected="comfort",
        default="none",
        entry_data={"presets": list(PRESETS)},
    ),
    Setting(
        name="learned_heating_power",
        configure=_configure_heating_power,
        read=_read("heating_power"),
        expected=LEARNED_HEATING_POWER,
        default=0.01,
    ),
    Setting(
        name="window_open",
        configure=_configure_window_open,
        read=_read("window_open"),
        expected=True,
        default=False,
        entry_options={"with_window": True},
        prepare=_close_the_window,
    ),
]


def setting_id(setting: Setting) -> str:
    """Name a parametrized case after the setting it round-trips."""
    return setting.name


async def _entry_up(hass, setting: Setting):
    """Bring up an entry wired for ``setting`` and return it with its entity."""
    set_room_sensor(hass, 18.0)
    if setting.prepare is not None:
        setting.prepare(hass)
    data = dict(make_entry(GENERIC_HEAT_TRV, **setting.entry_options).data)
    data.update(setting.entry_data)
    entry = MockConfigEntry(domain=DOMAIN, version=18, data=data, title=data["name"])
    await setup_entry(hass, entry)
    return entry, await wait_for_startup(hass, entry)


async def _thermostat_on(hass, setting: Setting):
    """Bring an entry up and put ``setting`` on its configured value."""
    entry, bt = await _entry_up(hass, setting)
    await setting.configure(hass, bt)
    assert setting.read(hass) == setting.expected
    return entry


@pytest.mark.parametrize("setting", SETTINGS, ids=setting_id)
async def test_the_case_can_tell_a_restore_from_a_default(
    hass, fake_trv, setting: Setting
):
    """A case configured to the default value proves nothing, and is caught here.

    This is the guard behind every other test in the file. A round-trip test
    whose fixture uses the value the thermostat would have come up on anyway
    cannot tell "restored correctly" from "fell back to the default": it goes
    blind without failing. So the default is read off a thermostat nobody
    configured, rather than written down here and left to age.
    """
    await _entry_up(hass, setting)

    assert setting.read(hass) == setting.default
    assert setting.expected != setting.default


@pytest.mark.parametrize("setting", SETTINGS, ids=setting_id)
async def test_setting_survives_a_reload(hass, fake_trv, setting: Setting):
    """Reloading the entry leaves the setting where the user put it."""
    entry = await _thermostat_on(hass, setting)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    assert setting.read(hass) == setting.expected


@pytest.mark.parametrize("setting", SETTINGS, ids=setting_id)
async def test_setting_survives_an_unload_and_setup(hass, fake_trv, setting: Setting):
    """A restart brings the setting back from what was persisted before it.

    Unloading and setting the entry up again is the restart path: the entity is
    torn down and the one that replaces it has only the persisted state to go
    on.
    """
    entry = await _thermostat_on(hass, setting)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    assert setting.read(hass) == setting.expected


@pytest.mark.parametrize("setting", SETTINGS, ids=setting_id)
async def test_setting_survives_the_options_form(hass, fake_trv, setting: Setting):
    """Opening the settings and changing nothing leaves the setting alone."""
    entry = await _thermostat_on(hass, setting)

    await click_through_the_options(hass, entry)
    await wait_for_startup(hass, entry)

    assert setting.read(hass) == setting.expected
