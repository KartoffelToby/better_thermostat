"""Better Thermostat Switch Platform."""

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

# Import tracking variables from sensor.py
from .sensor import _ACTIVE_SWITCH_ENTITIES
from .utils.calibration.pid import _PID_STATES, DEFAULT_PID_AUTO_TUNE, build_pid_key
from .utils.const import CONF_CALIBRATION_MODE, CalibrationMode

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
    switch_unique_ids = []

    has_multiple_trvs = len(bt_climate.real_trvs) > 1
    for trv_entity_id, trv_data in bt_climate.real_trvs.items():
        advanced = trv_data.get("advanced", {})
        calibration_mode = advanced.get(CONF_CALIBRATION_MODE)

        # Normalize string values to CalibrationMode enum
        try:
            if isinstance(calibration_mode, str):
                calibration_mode = CalibrationMode(calibration_mode)
        except (ValueError, TypeError):
            # Invalid or unknown calibration mode, skip PID creation
            calibration_mode = None

        if calibration_mode == CalibrationMode.PID_CALIBRATION:
            pid_switch = BetterThermostatPIDAutoTuneSwitch(
                bt_climate, trv_entity_id, has_multiple_trvs
            )
            switches.append(pid_switch)
            switch_unique_ids.append(pid_switch._attr_unique_id)

        child_lock_switch = BetterThermostatChildLockSwitch(
            bt_climate, trv_entity_id, has_multiple_trvs
        )
        switches.append(child_lock_switch)
        switch_unique_ids.append(child_lock_switch._attr_unique_id)

    # Track created switch entities for cleanup
    _ACTIVE_SWITCH_ENTITIES[entry.entry_id] = switch_unique_ids

    _LOGGER.debug(
        "Better Thermostat %s: Created %d switch entities",
        bt_climate.device_name,
        len(switch_unique_ids),
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

        self._bt_climate.schedule_save_pid_state()
        self.async_write_ha_state()


class BetterThermostatChildLockSwitch(SwitchEntity, RestoreEntity):
    """Switch for Child Lock."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:account-lock"

    def __init__(self, bt_climate, trv_entity_id, show_trv_name=True):
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._trv_entity_id = trv_entity_id
        self._attr_unique_id = f"{bt_climate.unique_id}_{trv_entity_id}_child_lock"
        self._attr_name = "Child Lock"
        if show_trv_name:
            trv_state = bt_climate.hass.states.get(trv_entity_id)
            trv_name = trv_state.name if trv_state and trv_state.name else trv_entity_id
            self._attr_translation_key = "child_lock"
            self._attr_translation_placeholders = {"trv_name": trv_name}
        else:
            self._attr_translation_key = "child_lock_no_trv"

    @property
    def device_info(self):
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if switch is on."""
        return (
            self._bt_climate.real_trvs[self._trv_entity_id]
            .get("advanced", {})
            .get("child_lock", False)
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the switch on."""
        self._update_state(True)
        await self._set_child_lock(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the switch off."""
        self._update_state(False)
        await self._set_child_lock(False)

    def _update_state(self, state: bool):
        """Update the state."""
        if "advanced" not in self._bt_climate.real_trvs[self._trv_entity_id]:
            self._bt_climate.real_trvs[self._trv_entity_id]["advanced"] = {}
        self._bt_climate.real_trvs[self._trv_entity_id]["advanced"]["child_lock"] = (
            state
        )
        self.async_write_ha_state()

    async def _set_child_lock(self, state: bool):
        """Set the child lock on the real device."""
        entity_registry = er.async_get(self._bt_climate.hass)
        reg_entity = entity_registry.async_get(self._trv_entity_id)

        if reg_entity is None or reg_entity.device_id is None:
            return

        device_id = reg_entity.device_id

        def find_entity(domains, keywords):
            for ent in entity_registry.entities.values():
                if ent.device_id != device_id or ent.domain not in domains:
                    continue
                name = (getattr(ent, "original_name", "") or "").lower()
                uid = (ent.unique_id or "").lower()
                eid = (ent.entity_id or "").lower()

                if (
                    any(k in name for k in keywords)
                    or any(k in uid for k in keywords)
                    or any(k in eid for k in keywords)
                ):
                    return ent.entity_id
            return None

        # Look for switch (Z2M) or lock
        cl_entity = find_entity(
            ["switch", "lock"], ["child_lock", "child lock", "lock"]
        )

        if cl_entity:
            target_state = STATE_ON if state else STATE_OFF
            domain = cl_entity.split(".")[0]

            try:
                if domain == "switch":
                    cur = self._bt_climate.hass.states.get(cl_entity)
                    if cur and cur.state != target_state:
                        _LOGGER.debug(
                            "Better Thermostat Child Lock: Setting child lock (switch) for %s to %s",
                            cl_entity,
                            target_state,
                        )
                        service = "turn_on" if state else "turn_off"
                        await self._bt_climate.hass.services.async_call(
                            "switch", service, {"entity_id": cl_entity}
                        )
                elif domain == "lock":
                    target_lock = "locked" if state else "unlocked"
                    cur = self._bt_climate.hass.states.get(cl_entity)
                    if cur and cur.state != target_lock:
                        _LOGGER.debug(
                            "Better Thermostat Child Lock: Setting child lock (lock) for %s to %s",
                            cl_entity,
                            target_lock,
                        )
                        service = "lock" if state else "unlock"
                        await self._bt_climate.hass.services.async_call(
                            "lock", service, {"entity_id": cl_entity}
                        )
            except Exception as e:
                _LOGGER.warning(
                    "Better Thermostat Child Lock: Failed to set child lock for %s: %s",
                    cl_entity,
                    e,
                )
