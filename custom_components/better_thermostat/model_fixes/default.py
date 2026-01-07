"""Default model quirks passthrough for unknown devices.

These helpers implement safe no-op defaults for devices that do not
require specific quirks.
"""

import logging

from homeassistant.const import STATE_LOCKED, STATE_OFF, STATE_ON, STATE_UNLOCKED
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    """Return the given local calibration offset unchanged."""
    return offset


def fix_valve_calibration(self, entity_id, valve):
    """Return the given valve calibration unchanged."""
    return valve


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return the given target temperature unchanged."""
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """Do not override HVAC mode by default."""
    return False


async def override_set_temperature(self, entity_id, temperature):
    """Do not override set temperature by default."""
    return False


async def inital_tweak(self, entity_id):
    """Run initial tweaks for the device."""
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)

    if reg_entity is not None and reg_entity.device_id is not None:
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

        # 1. Local calibration -> 0
        cal_entity = find_entity(
            ["number"],
            ["local_temperature_calibration", "local_calibration", "calibration"],
        )
        if cal_entity:
            try:
                _LOGGER.debug(
                    "better_thermostat %s: Resetting local calibration for %s to 0",
                    self.device_name,
                    cal_entity,
                )
                await self.hass.services.async_call(
                    "number", "set_value", {"entity_id": cal_entity, "value": 0}
                )
            except Exception as e:
                _LOGGER.warning(
                    "better_thermostat %s: Failed to reset calibration for %s: %s",
                    self.device_name,
                    cal_entity,
                    e,
                )

        # 2. Child lock sync setting
        child_lock_setting = self.real_trvs[entity_id]["advanced"].get("child_lock")
        if child_lock_setting is not None:
            # Look for switch (Z2M) or lock
            cl_entity = find_entity(
                ["switch", "lock"], ["child_lock", "child lock", "lock"]
            )
            if cl_entity:
                target_state = STATE_ON if child_lock_setting else STATE_OFF
                domain = cl_entity.split(".")[0]

                try:
                    if domain == "switch":
                        cur = self.hass.states.get(cl_entity)
                        if cur and cur.state != target_state:
                            _LOGGER.debug(
                                "better_thermostat %s: Setting child lock (switch) for %s to %s",
                                self.device_name,
                                cl_entity,
                                target_state,
                            )
                            service = "turn_on" if child_lock_setting else "turn_off"
                            await self.hass.services.async_call(
                                "switch", service, {"entity_id": cl_entity}
                            )
                    elif domain == "lock":
                        target_lock = (
                            STATE_LOCKED if child_lock_setting else STATE_UNLOCKED
                        )
                        cur = self.hass.states.get(cl_entity)
                        if cur and cur.state != target_lock:
                            _LOGGER.debug(
                                "better_thermostat %s: Setting child lock (lock) for %s to %s",
                                self.device_name,
                                cl_entity,
                                target_lock,
                            )
                            service = "lock" if child_lock_setting else "unlock"
                            await self.hass.services.async_call(
                                "lock", service, {"entity_id": cl_entity}
                            )
                except Exception as e:
                    _LOGGER.warning(
                        "better_thermostat %s: Failed to set child lock for %s: %s",
                        self.device_name,
                        cl_entity,
                        e,
                    )

        # 3. Away / Window detection -> Off
        # Window detection disable the interal trv window detection, its handled by better_thermostat
        win_entity = find_entity(
            ["switch"],
            ["window_detection", "window_open", "window open", "open_window"],
        )
        if win_entity:
            try:
                cur = self.hass.states.get(win_entity)
                if cur and cur.state != STATE_OFF:
                    _LOGGER.debug(
                        "better_thermostat %s: Disabling window detection for %s",
                        self.device_name,
                        win_entity,
                    )
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": win_entity}
                    )
            except Exception as e:
                _LOGGER.warning(
                    "better_thermostat %s: Failed to disable window detection for %s: %s",
                    self.device_name,
                    win_entity,
                    e,
                )

        # Away mode -> Off
        # Disable the away mode on the device if available
        away_entity = find_entity(
            ["switch"], ["away_mode", "away mode", "holiday_mode", "holiday"]
        )
        if away_entity:
            try:
                cur = self.hass.states.get(away_entity)
                if cur and cur.state != STATE_OFF:
                    _LOGGER.debug(
                        "better_thermostat %s: Disabling away mode for %s",
                        self.device_name,
                        away_entity,
                    )
                    await self.hass.services.async_call(
                        "switch", "turn_off", {"entity_id": away_entity}
                    )
            except Exception as e:
                _LOGGER.warning(
                    "better_thermostat %s: Failed to disable away mode for %s: %s",
                    self.device_name,
                    away_entity,
                    e,
                )
