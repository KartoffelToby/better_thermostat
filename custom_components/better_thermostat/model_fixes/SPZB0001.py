"""Model fixes for SPZB0001 devices.

Device-specific quirks for SPZB0001 thermostats handled by the
Better Thermostat integration.
"""

import logging
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    """Clamp local calibration to safe bounds for SPZB0001 devices."""
    if offset > 5:
        offset = 5
    elif offset < -5:
        offset = -5
    return offset


def fix_valve_calibration(self, entity_id, valve):
    """Return a possibly adjusted valve calibration for SPZB0001.

    Currently a no-op.
    """

    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        _LOGGER.debug(
            "better_thermostat %s: SPZB0001 fix_valve_calibration: no registry entity for %s",
            self.device_name,
            entity_id,
        )
        return False
    device_id = reg_entity.device_id
    target_entity = None
    for ent in entity_registry.entities.values():
        if ent.device_id != device_id or ent.domain != "number":
            continue
        en = (ent.entity_id or "").lower()
        uid = (ent.unique_id or "").lower()
        name = (getattr(ent, "original_name", None) or "").lower()
        if "_mode" in en or "_mode" in uid or "mode" in name:
            target_entity = ent.entity_id
    if target_entity is None:
        _LOGGER.debug(
            "better_thermostat %s: SPZB0001 fix_valve_calibration: no target entity for %s",
            self.device_name,
            entity_id,
        )
        return False
    val = self.hass.states.get(target_entity)
    if val is None:
        if val != 1:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "number", "set_value", {"entity_id": target_entity, "value": 1}
                )
            )

    return valve


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return a possibly adjusted target temperature for SPZB0001.

    Currently a no-op.
    """
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """Do not override HVAC mode for SPZB0001 devices."""
    return False


async def override_set_temperature(self, entity_id, temperature):
    """Do not override temperature sets for SPZB0001 devices."""
    return False
