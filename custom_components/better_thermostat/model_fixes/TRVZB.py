"""Quirks and helpers for Sonoff TRVZB (Zigbee TRV) devices.

Provides Sonoff TRVZB specific helper functions such as writing valve
percentages and mirroring external temperature into the TRV when supported.
"""

import asyncio
import logging

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

VALVE_MAINTENANCE_INTERVAL_HOURS = 12

# Some users report that the TRVZB motor can occasionally lose its calibration and
# fail to fully close the valve when commanded to very small openings.
#
# Workaround: when requesting a further close (target_pct < last_pct), briefly
# command the valve to open a bit more and then to the requested target.
_TRVZB_CLOSE_BUMP_OPEN_DELTA_PCT = 10
_TRVZB_CLOSE_BUMP_DELAY_S = 5.0


def _cancel_pending_valve_bump(trv_state: dict) -> None:
    task = trv_state.pop("_trvzb_valve_bump_task", None)
    if task is not None:
        try:
            task.cancel()
        except Exception:
            pass


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
        target_pct = max(0, min(100, int(percent)))

        trv_state = self.real_trvs.get(entity_id)
        if not isinstance(trv_state, dict):
            return False

        # During valve maintenance we don't want to add additional delayed steps.
        if getattr(self, "in_maintenance", False):
            ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
            return bool(ok)

        # Cancel any previous pending delayed "bump then set".
        _cancel_pending_valve_bump(trv_state)

        last_pct_raw = trv_state.get("last_valve_percent")
        try:
            last_pct = None if last_pct_raw is None else int(last_pct_raw)
        except Exception:
            last_pct = None

        # If we don't know the last commanded percent, just set directly.
        if last_pct is None:
            ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
            return bool(ok)

        # Only apply workaround when closing further.
        if target_pct < last_pct:
            bump_pct = min(100, int(last_pct) + _TRVZB_CLOSE_BUMP_OPEN_DELTA_PCT)

            # If we can't "bump open", fall back to direct set.
            ok_bump = await maybe_set_sonoff_valve_percent(self, entity_id, bump_pct)
            if not ok_bump:
                ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
                return bool(ok)

            seq = int(trv_state.get("_trvzb_valve_bump_seq", 0)) + 1
            trv_state["_trvzb_valve_bump_seq"] = seq

            async def _delayed_set():
                try:
                    await asyncio.sleep(float(_TRVZB_CLOSE_BUMP_DELAY_S))
                    cur_state = self.real_trvs.get(entity_id, {}) or {}
                    if int(cur_state.get("_trvzb_valve_bump_seq", 0)) != seq:
                        return
                    await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
                except asyncio.CancelledError:
                    return
                except Exception as ex:
                    _LOGGER.debug(
                        "better_thermostat %s: TRVZB delayed valve set exception: %s",
                        getattr(self, "device_name", "unknown"),
                        ex,
                    )

            trv_state["_trvzb_valve_bump_task"] = self.hass.async_create_task(
                _delayed_set()
            )
            return True

        # Opening (or same) => set directly.
        ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
        return bool(ok)
    except Exception:
        return False


async def maybe_set_external_temperature(self, entity_id, temperature: float) -> bool:
    """Set Sonoff TRVZB external temperature input via a number entity on the same device.

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
