"""The thermostat's entities take their icons from the icon translations.

An icon set on the entity would override ``icons.json`` in the frontend,
including the per-state icons it declares. The entities therefore carry no
icon of their own, and every sensor and switch they register resolves to
one through Home Assistant's icon translations.
"""

from dataclasses import replace

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.icon import async_get_icons
import pytest

from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import DOMAIN, make_entry, set_room_sensor, setup_entry, wait_for_startup
from .device_profiles import GENERIC_HEAT_TRV

# The smoothed room temperature sensors keep the icon of their device class.
_DEVICE_CLASS_ICONS = {"external_temp_ema", "external_temp_ema_1h"}


@pytest.mark.parametrize(
    "fake_trv",
    [replace(GENERIC_HEAT_TRV, calibration_mode=CalibrationMode.PID_CALIBRATION.value)],
    indirect=True,
    ids=lambda profile: profile.calibration_mode,
)
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
