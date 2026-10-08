"""The thermostat's entities take their icons from the icon translations.

An icon set on the entity would override ``icons.json`` in the frontend,
including the per-state icons it declares. The entities therefore carry no
icon of their own, and every sensor and switch they register resolves to
one through Home Assistant's icon translations.
"""

from dataclasses import replace

from homeassistant.const import STATE_OFF
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.icon import async_get_icons
import pytest

from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import DOMAIN, make_entry, set_room_sensor, setup_entry, wait_for_startup
from .device_profiles import GENERIC_HEAT_TRV, GROUP_OF_THREE

# The smoothed room temperature sensors keep the icon of their device class.
_DEVICE_CLASS_ICONS = {"external_temp_ema", "external_temp_ema_1h"}


def _state_icon(icons: dict, reg: er.RegistryEntry, state: str) -> str:
    """Return the icon the frontend shows for ``reg`` in ``state``.

    The frontend takes the icon its translation declares for the state and
    falls back to the translation's default.
    """
    translation = icons[reg.domain][reg.translation_key]
    return translation.get("state", {}).get(state, translation["default"])


async def _child_lock_icons(hass, entry) -> dict[str, set[str]]:
    """Return the icons the entry's child lock switches show, by translation key."""
    icons = (await async_get_icons(hass, "entity", integrations={DOMAIN}))[DOMAIN]
    shown: dict[str, set[str]] = {}
    for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        if reg.domain != "switch" or not reg.unique_id.endswith("_child_lock"):
            continue
        state = hass.states.get(reg.entity_id).state
        assert state == STATE_OFF, reg.entity_id
        shown.setdefault(reg.translation_key, set()).add(_state_icon(icons, reg, state))
    return shown


@pytest.mark.parametrize(
    "fake_trv",
    [replace(GENERIC_HEAT_TRV, calibration_mode=CalibrationMode.PID_CALIBRATION.value)],
    indirect=True,
    ids=lambda profile: profile.calibration_mode,
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_every_entity_resolves_its_icon_from_the_translations(hass, fake_trv):
    """No entity sets an icon, and each one's translation key names one."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    icons = (await async_get_icons(hass, "entity", integrations={DOMAIN}))[DOMAIN]

    registered = [
        reg
        for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if reg.domain in {"sensor", "switch"}
    ]
    keys = {(reg.domain, reg.translation_key) for reg in registered}

    assert {("sensor", "pid_kp"), ("switch", "pid_auto_tune_no_trv")} <= keys
    for reg in registered:
        assert "icon" not in hass.states.get(reg.entity_id).attributes, reg.entity_id
        if reg.translation_key in _DEVICE_CLASS_ICONS:
            continue
        assert icons[reg.domain][reg.translation_key]["default"].startswith("mdi:")


async def test_the_single_head_child_lock_shows_an_open_lock_when_off(hass, fake_trv):
    """An unlocked head without a name in its switch shows the open lock."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert await _child_lock_icons(hass, entry) == {
        "child_lock_no_trv": {"mdi:account-lock-open"}
    }


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True)
async def test_every_group_child_lock_shows_an_open_lock_when_off(hass, trv_group):
    """Each unlocked head of a group shows the open lock on its named switch."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert await _child_lock_icons(hass, entry) == {
        "child_lock": {"mdi:account-lock-open"}
    }
