"""Model fixes for SPZB0001 devices.

Device-specific quirks for SPZB0001 thermostats handled by the
Better Thermostat integration.
"""

from __future__ import annotations

import logging

from homeassistant.helpers import entity_registry as er

from custom_components.better_thermostat.model_fixes.types import ModelFixHost

from ..utils.const import CalibrationType

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self: ModelFixHost, entity_id: str, offset: float) -> float:
    """Clamp local calibration to safe bounds for SPZB0001 devices."""
    if offset > 5:
        offset = 5
    elif offset < -5:
        offset = -5
    return offset


async def check_operation_mode(
    self: ModelFixHost, entity_id: str, goal: str = "1"
) -> bool:
    """Put the device's TRV mode select onto ``goal``.

    Finds the ``select`` entity carrying the TRV mode on the same device as
    ``entity_id`` and selects ``goal`` when it reads anything else. Returns
    True once the mode reads ``goal`` or the switch to it has been requested,
    and False when the registry entry, the mode select or its state is
    missing.
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


async def initial_tweak(self: ModelFixHost, entity_id: str) -> None:
    """Run initial tweaks for the device."""
    _calibration_type = self.real_trvs[entity_id].advanced.get(
        "calibration", CalibrationType.TARGET_TEMP_BASED
    )
    if _calibration_type == CalibrationType.DIRECT_VALVE_BASED:
        await check_operation_mode(self, entity_id, goal="1")
    else:
        await check_operation_mode(self, entity_id, goal="2")


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Return a possibly adjusted target temperature for SPZB0001.

    Currently a no-op.
    """
    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """Do not override HVAC mode for SPZB0001 devices."""
    return False


async def override_set_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Do not override temperature sets for SPZB0001 devices."""
    return False
