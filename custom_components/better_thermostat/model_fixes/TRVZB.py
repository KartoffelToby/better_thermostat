"""Quirks and helpers for Sonoff TRVZB (Zigbee TRV) devices.

Provides Sonoff TRVZB specific helper functions such as writing valve
percentages and mirroring external temperature into the TRV when supported.
"""

import logging

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    """Return unchanged local calibration for TRVZB by default."""
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return unchanged setpoint temperature for TRVZB by default."""
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
    Uses translation_key for language-agnostic detection, with fallback to name matching.
    Prefers explicit Sonoff entities:
      - number.*.valve_opening_degree = percent
      - number.*.valve_closing_degree = 100 - percent
    Returns True if at least one write succeeds, False otherwise.
    """
    try:
        model = str(self.real_trvs[entity_id].get("model", ""))
        # Only attempt for Sonoff TRVZB
        if not (
            "sonoff" in model.lower() or "trvzb" in model.lower() or model == "TRVZB"
        ):
            _LOGGER.debug(
                "better_thermostat %s: TRVZB maybe_set_sonoff_valve_percent skipped (model=%s)",
                self.device_name,
                model,
            )
            return False
        entity_registry = er.async_get(self.hass)
        reg_entity = entity_registry.async_get(entity_id)
        if reg_entity is None:
            _LOGGER.debug(
                "better_thermostat %s: TRVZB maybe_set_sonoff_valve_percent: no registry entity for %s",
                self.device_name,
                entity_id,
            )
            return False
        device_id = reg_entity.device_id
        opening_candidates = []
        closing_candidates = []
        generic_candidates = []
        
        for ent in entity_registry.entities.values():
            if ent.device_id != device_id or ent.domain != "number":
                continue
            
            # Prefer translation_key for language-agnostic detection
            translation_key = getattr(ent, "translation_key", None)
            if translation_key:
                tk = translation_key.lower().strip()
                if tk in ("valve_opening_degree", "valve_opening", "opening_degree"):
                    opening_candidates.append(ent.entity_id)
                    _LOGGER.debug(
                        "better_thermostat %s: Found valve opening via translation_key=%s",
                        self.device_name,
                        translation_key,
                    )
                    continue
                if tk in ("valve_closing_degree", "valve_closing", "closing_degree"):
                    closing_candidates.append(ent.entity_id)
                    _LOGGER.debug(
                        "better_thermostat %s: Found valve closing via translation_key=%s",
                        self.device_name,
                        translation_key,
                    )
                    continue
            
            # Fallback to name-based matching for backward compatibility
            en = (ent.entity_id or "").lower()
            uid = (ent.unique_id or "").lower()
            name = (getattr(ent, "original_name", None) or "").lower()
            
            # Check for valve opening patterns
            if (
                "valve_opening_degree" in en
                or "valve_opening_degree" in uid
                or "valve opening degree" in name
            ):
                opening_candidates.append(ent.entity_id)
                continue
            # Check for valve closing patterns
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
        _LOGGER.debug(
            "better_thermostat %s: TRVZB valve write candidates (open=%s, close=%s, generic=%s) target=%s%% for %s",
            self.device_name,
            opening_candidates,
            closing_candidates,
            generic_candidates,
            pct,
            entity_id,
        )
        wrote = False

        # If we have explicit opening, set it
        if opening_candidates:
            target_open = opening_candidates[0]
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": target_open, "value": pct},
                blocking=True,
                context=self.context,
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
                "number",
                "set_value",
                {"entity_id": target_close, "value": comp},
                blocking=True,
                context=self.context,
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
            generic_candidates.sort(
                key=lambda x: ("valve" not in x, "position" not in x)
            )
            target = generic_candidates[0]
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": target, "value": pct},
                blocking=True,
                context=self.context,
            )
            _LOGGER.debug(
                "better_thermostat %s: set TRVZB generic valve percent %s%% on %s (for %s)",
                self.device_name,
                pct,
                target,
                entity_id,
            )
            wrote = True

        if not wrote:
            _LOGGER.debug(
                "better_thermostat %s: TRVZB valve percent write had no matching number entity (target=%s%%, %s)",
                self.device_name,
                pct,
                entity_id,
            )
        return wrote
    except Exception as ex:
        _LOGGER.debug(
            "better_thermostat %s: TRVZB maybe_set_sonoff_valve_percent exception: %s",
            self.device_name,
            ex,
        )
        return False


async def override_set_valve(self, entity_id, percent: int):
    """Override valve setting for TRVZB via number.* entity.

    Returns True if handled (write attempted), False to let adapter fallback run.
    """
    try:
        ok = await maybe_set_sonoff_valve_percent(self, entity_id, percent)
        return bool(ok)
    except Exception:
        return False


async def maybe_set_external_temperature(self, entity_id, temperature: float) -> bool:
    """Set Sonoff TRVZB external temperature input via a number entity on the same device.

    Uses translation_key for language-agnostic detection, with fallback to name matching.
    Looks for number.* entity matching external_temperature_input and writes the
    given temperature (clamped to 0..99.9, rounded to one decimal).
    Returns True on success, False otherwise.
    """
    try:
        model = str(self.real_trvs[entity_id].get("model", ""))
        if not (
            "sonoff" in model.lower() or "trvzb" in model.lower() or model == "TRVZB"
        ):
            _LOGGER.debug(
                "better_thermostat %s: TRVZB maybe_set_external_temperature skipped (model=%s)",
                self.device_name,
                model,
            )
            return False
        entity_registry = er.async_get(self.hass)
        reg_entity = entity_registry.async_get(entity_id)
        if reg_entity is None:
            _LOGGER.debug(
                "better_thermostat %s: TRVZB maybe_set_external_temperature: no registry entity for %s",
                self.device_name,
                entity_id,
            )
            return False
        device_id = reg_entity.device_id
        target_entities = []
        
        for ent in entity_registry.entities.values():
            if ent.device_id != device_id or ent.domain != "number":
                continue
            
            # Prefer translation_key for language-agnostic detection
            translation_key = getattr(ent, "translation_key", None)
            if translation_key:
                tk = translation_key.lower().strip()
                if tk in ("external_temperature_input", "external_temperature", "external_temp"):
                    target_entities.append(ent.entity_id)
                    _LOGGER.debug(
                        "better_thermostat %s: Found external temperature entity via translation_key=%s",
                        self.device_name,
                        translation_key,
                    )
                    continue
            
            # Fallback to name-based matching for backward compatibility
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
            _LOGGER.debug(
                "better_thermostat %s: TRVZB external_temperature_input number entity not found for %s",
                self.device_name,
                entity_id,
            )
            return False

        # Clamp and round
        try:
            val = float(temperature)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "better_thermostat %s: TRVZB maybe_set_external_temperature got non-float: %s",
                self.device_name,
                temperature,
            )
            return False
        val = max(0.0, min(99.9, round(val, 1)))

        target = target_entities[0]
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": target, "value": val},
            blocking=True,
            context=self.context,
        )
        _LOGGER.debug(
            "better_thermostat %s: set TRVZB external_temperature_input=%.1f on %s (for %s)",
            self.device_name,
            val,
            target,
            entity_id,
        )
        return True
    except Exception as ex:
        _LOGGER.debug(
            "better_thermostat %s: TRVZB maybe_set_external_temperature exception: %s",
            self.device_name,
            ex,
        )
        return False
