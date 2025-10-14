# Quirks for Sonoff TRVZB (Zigbee thermostatic radiator valve)
import logging
from homeassistant.components.climate.const import HVACMode
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """No special handling required for TRVZB when setting hvac mode."""
    await self.hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": entity_id, "hvac_mode": hvac_mode},
        blocking=True,
        context=self.context,
    )
    return True


async def override_set_temperature(self, entity_id, temperature):
    """No special setpoint handling required; ensure manual preset if needed."""
    await self.hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id, "temperature": temperature},
        blocking=True,
        context=self.context,
    )
    return True


async def maybe_set_sonoff_valve_percent(self, entity_id, percent: int) -> bool:
    """Try to set Sonoff TRVZB valve percent via a number entity on the same device.

    Scans the device of the given climate entity for a `number.*` entity that
    represents valve opening/position and writes the provided percentage.
    Prefers explicit Sonoff entities:
      - number.*.valve_opening_degree = percent
      - number.*.valve_closing_degree = 100 - percent
    Returns True if at least one write succeeds, False otherwise.
    """
    try:
        model = str(self.real_trvs[entity_id].get("model", ""))
        # Only attempt for Sonoff TRVZB
        if not ("sonoff" in model.lower() or "trvzb" in model.lower() or model == "TRVZB"):
            return False
        entity_registry = er.async_get(self.hass)
        reg_entity = entity_registry.async_get(entity_id)
        if reg_entity is None:
            return False
        device_id = reg_entity.device_id
        opening_candidates = []
        closing_candidates = []
        generic_candidates = []
        for ent in entity_registry.entities.values():
            if ent.device_id != device_id or ent.domain != "number":
                continue
            en = (ent.entity_id or "").lower()
            uid = (ent.unique_id or "").lower()
            name = (getattr(ent, "original_name", None) or "").lower()
            # Prefer explicit Sonoff names first
            if (
                "valve_opening_degree" in en
                or "valve_opening_degree" in uid
                or "valve opening degree" in name
            ):
                opening_candidates.append(ent.entity_id)
                continue
            if (
                "valve_closing_degree" in en
                or "valve_closing_degree" in uid
                or "valve closing degree" in name
            ):
                closing_candidates.append(ent.entity_id)
                continue
            # Generic fallbacks
            if (
                "valve" in en
                or "position" in en
                or "opening" in en
                or "degree" in en
                or "valve" in uid
                or "position" in uid
                or "opening" in uid
                or "degree" in uid
                or "valve" in name
                or "position" in name
                or "opening" in name
                or "degree" in name
            ):
                generic_candidates.append(ent.entity_id)

        pct = max(0, min(100, int(percent)))
        wrote = False

        # If we have explicit opening, set it
        if opening_candidates:
            target_open = opening_candidates[0]
            await self.hass.services.async_call(
                "number", "set_value", {"entity_id": target_open, "value": pct}, blocking=True, context=self.context
            )
            _LOGGER.debug(
                "better_thermostat %s: set TRVZB valve_opening_degree=%s on %s (for %s)",
                self.device_name,
                pct,
                target_open,
                entity_id,
            )
            wrote = True

        # If we have explicit closing, set complement 100 - pct
        if closing_candidates:
            target_close = closing_candidates[0]
            comp = 100 - pct
            await self.hass.services.async_call(
                "number", "set_value", {"entity_id": target_close, "value": comp}, blocking=True, context=self.context
            )
            _LOGGER.debug(
                "better_thermostat %s: set TRVZB valve_closing_degree=%s on %s (for %s)",
                self.device_name,
                comp,
                target_close,
                entity_id,
            )
            wrote = True

        # Fallback: if neither explicit entity exists, try a generic candidate
        if not wrote and generic_candidates:
            # Prefer entities with 'valve' then 'position'
            generic_candidates.sort(key=lambda x: (
                "valve" not in x, "position" not in x))
            target = generic_candidates[0]
            await self.hass.services.async_call(
                "number", "set_value", {"entity_id": target, "value": pct}, blocking=True, context=self.context
            )
            _LOGGER.debug(
                "better_thermostat %s: set TRVZB generic valve percent %s%% on %s (for %s)",
                self.device_name,
                pct,
                target,
                entity_id,
            )
            wrote = True

        return wrote
    except Exception:  # noqa: BLE001
        return False


async def maybe_set_external_temperature(self, entity_id, temperature: float) -> bool:
    """Set Sonoff TRVZB external temperature input via a number entity on the same device.

    Looks for number.* entity matching external_temperature_input and writes the
    given temperature (clamped to 0..99.9, rounded to one decimal).
    Returns True on success, False otherwise.
    """
    try:
        model = str(self.real_trvs[entity_id].get("model", ""))
        if not ("sonoff" in model.lower() or "trvzb" in model.lower() or model == "TRVZB"):
            return False
        entity_registry = er.async_get(self.hass)
        reg_entity = entity_registry.async_get(entity_id)
        if reg_entity is None:
            return False
        device_id = reg_entity.device_id
        target_entities = []
        for ent in entity_registry.entities.values():
            if ent.device_id != device_id or ent.domain != "number":
                continue
            en = (ent.entity_id or "").lower()
            uid = (ent.unique_id or "").lower()
            name = (getattr(ent, "original_name", None) or "").lower()
            if (
                "external_temperature_input" in en
                or "external_temperature_input" in uid
                or "external temperature input" in name
            ):
                target_entities.append(ent.entity_id)

        if not target_entities:
            return False

        # Clamp and round
        try:
            val = float(temperature)
        except (TypeError, ValueError):
            return False
        val = max(0.0, min(99.9, round(val, 1)))

        target = target_entities[0]
        await self.hass.services.async_call(
            "number", "set_value", {"entity_id": target, "value": val}, blocking=True, context=self.context
        )
        _LOGGER.debug(
            "better_thermostat %s: set TRVZB external_temperature_input=%.1f on %s (for %s)",
            self.device_name,
            val,
            target,
            entity_id,
        )
        return True
    except Exception:  # noqa: BLE001
        return False
