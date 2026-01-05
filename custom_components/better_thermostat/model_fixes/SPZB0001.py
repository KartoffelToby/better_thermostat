"""Model fixes for SPZB0001 devices.

Device-specific quirks for SPZB0001 thermostats handled by the
Better Thermostat integration.
"""

import logging
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    """Clamp local calibration to safe bounds for SPZB0001 devices."""

    self.hass.async_create_task(check_operation_mode(self, entity_id, "2"))

    if offset > 5:
        offset = 5
    elif offset < -5:
        offset = -5
    return offset


async def check_operation_mode(self, entity_id, goal: str = "1"):
    """Return a possibly adjusted valve calibration for SPZB0001.

    Currently a no-op.
    """

    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        _LOGGER.debug(
            "better_thermostat %s: SPZB0001 check_operation_mode: no registry entity for %s",
            self.device_name,
            entity_id,
        )
        return False
    device_id = reg_entity.device_id
    target_entity = None
    for ent in entity_registry.entities.values():
        if ent.device_id != device_id or ent.domain != "select":
            continue
        en = (ent.entity_id or "").lower()
        uid = (ent.unique_id or "").lower()
        name = (getattr(ent, "original_name", None) or "").lower()
        if "_trv_mode" in en or "_trv_mode" in uid or "Trv mode" in name:
            target_entity = ent.entity_id
    if target_entity is None:
        _LOGGER.debug(
            "better_thermostat %s: SPZB0001 check_operation_mode: no target entity for %s",
            self.device_name,
            entity_id,
        )
        return False
    val = self.hass.states.get(target_entity)
    if val is None:
        return False
    if val.state != goal:
        _LOGGER.debug(
            "better_thermostat %s: SPZB0001 check_operation_mode: setting target entity %s to %s from %s",
            self.device_name,
            target_entity,
            goal,
            val.state,
        )
        await self.hass.services.async_call(
            "select", "select_option", {"entity_id": target_entity, "option": goal}
        )

    return True


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return a possibly adjusted target temperature for SPZB0001.

    Currently a no-op.
    """
    self.hass.async_create_task(check_operation_mode(self, entity_id, "2"))
    return temperature


async def override_set_valve(self, entity_id, percent: int):
    """Override valve setting for SPZB0001 via trv_mode.* entity.

    Returns True if handled (write attempted), False to let adapter fallback run.
    """
    try:
        ok = await check_operation_mode(self, entity_id, "1")
        return bool(ok)
    except Exception:
        return False


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """Do not override HVAC mode for SPZB0001 devices."""
    return False


async def override_set_temperature(self, entity_id, temperature):
    """Do not override temperature sets for SPZB0001 devices."""
    return False
