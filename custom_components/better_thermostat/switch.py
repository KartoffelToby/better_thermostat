"""Better Thermostat Switch Platform."""

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .utils.calibration.pid import _PID_STATES, build_pid_key, DEFAULT_PID_AUTO_TUNE
from .utils.const import CalibrationMode

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Better Thermostat switches."""
    bt_climate = hass.data[DOMAIN][entry.entry_id].get("climate")
    if not bt_climate:
        return

    switches = []
    has_multiple_trvs = len(bt_climate.real_trvs) > 1
    for trv_entity_id, trv_data in bt_climate.real_trvs.items():
        advanced = trv_data.get("advanced", {})
        if advanced.get("calibration_mode") == CalibrationMode.PID_CALIBRATION:
            switches.append(
                BetterThermostatPIDAutoTuneSwitch(
                    bt_climate, trv_entity_id, has_multiple_trvs
                )
            )

    async_add_entities(switches)


class BetterThermostatPIDAutoTuneSwitch(SwitchEntity, RestoreEntity):
    """Switch for PID Auto Tune."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:magic-staff"

    def __init__(self, bt_climate, trv_entity_id, show_trv_name=True):
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._trv_entity_id = trv_entity_id
        self._attr_unique_id = f"{bt_climate.unique_id}_{trv_entity_id}_pid_auto_tune"

        if show_trv_name:
            trv_state = bt_climate.hass.states.get(trv_entity_id)
            trv_name = trv_state.name if trv_state and trv_state.name else trv_entity_id
            self._attr_translation_key = "pid_auto_tune"
            self._attr_translation_placeholders = {"trv_name": trv_name}
        else:
            self._attr_translation_key = "pid_auto_tune_no_trv"

    @property
    def device_info(self):
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if switch is on."""
        # Try to get the value from the current active PID state
        key = build_pid_key(self._bt_climate, self._trv_entity_id)
        pid_state = _PID_STATES.get(key)

        if pid_state is not None and pid_state.auto_tune is not None:
            return pid_state.auto_tune

        return DEFAULT_PID_AUTO_TUNE

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the switch on."""
        self._update_state(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the switch off."""
        self._update_state(False)

    def _update_state(self, state: bool):
        """Update the state."""
        # Update persistent PID states (if any exist for this TRV)
        # Use the same unique_id logic as build_pid_key to ensure matching
        uid = self._bt_climate.unique_id or "bt"
        prefix = f"{uid}:{self._trv_entity_id}:"

        for key, pid_state in _PID_STATES.items():
            if key.startswith(prefix):
                pid_state.auto_tune = state

        self._bt_climate._schedule_save_pid_state()
        self.async_write_ha_state()
