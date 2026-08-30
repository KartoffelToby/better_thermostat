"""Quirks and helpers for Sonoff TRVZB (Zigbee TRV) devices.

Provides Sonoff TRVZB specific helper functions such as writing valve
percentages and mirroring external temperature into the TRV when supported.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.better_thermostat.model_fixes.types import (
    ModelFixHost,
    ModelFixTrv,
)

_LOGGER = logging.getLogger(__name__)

VALVE_MAINTENANCE_INTERVAL_HOURS = 84

# Some users report that the TRVZB motor can occasionally lose its calibration and
# fail to fully close the valve when commanded to very small openings.
#
# Workaround: when requesting a further close (target_pct < last_pct), briefly
# command the valve to open a bit more and then to the requested target. A
# close that arrives while that delayed write is still due is written straight
# away instead of bumping again, so the requested position always reaches the
# device.
_TRVZB_CLOSE_BUMP_OPEN_DELTA_PCT = 10
_TRVZB_CLOSE_BUMP_DELAY_S = 5.0


def _cancel_pending_valve_bump(trv_state: ModelFixTrv) -> bool:
    """Cancel a scheduled valve write and report whether one was still due.

    A task that has already run is not a pending write; it is only the
    reference the last completed bump left behind.

    Parameters
    ----------
    trv_state : ModelFixTrv
        Domain object of the TRV whose pending write is to be dropped.

    Returns
    -------
    bool
        True when a write was still due and has been cancelled, False when
        there was none or it had already run.
    """
    task = trv_state.extra.pop("_trvzb_valve_bump_task", None)
    if task is None:
        return False
    try:
        if task.done():
            return False
        task.cancel()
    except asyncio.CancelledError, RuntimeError:
        return False
    return True


def fix_local_calibration(self: ModelFixHost, entity_id: str, offset: float) -> float:
    """Return unchanged local calibration for TRVZB by default."""
    return offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Return unchanged setpoint temperature for TRVZB by default."""
    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """No special HVAC mode handling for TRVZB; the generic adapter performs the write.

    Parameters
    ----------
    self : ModelFixHost
            Better Thermostat host providing device state and HA access
    entity_id : str
            entity_id of the TRV
    hvac_mode : str
            the HVAC mode to set

    Returns
    -------
    bool
            False, always: the generic adapter fallback performs the
            service call, including its retry handling
    """
    return False


async def override_set_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """No special setpoint handling for TRVZB; the generic adapter performs the write.

    Parameters
    ----------
    self : ModelFixHost
            Better Thermostat host providing device state and HA access
    entity_id : str
            entity_id of the TRV
    temperature : float
            the target temperature to set

    Returns
    -------
    bool
            False, always: the generic adapter fallback performs the
            service call, including step rounding and system-unit
            conversion
    """
    return False


async def maybe_set_sonoff_valve_percent(
    self: ModelFixHost, entity_id: str, percent: int
) -> bool:
    """Try to set Sonoff TRVZB valve percent via a number entity on the same device.

    Scans the device of the given climate entity for a ``number.*`` entity
    that represents valve opening/position and writes the provided
    percentage. Prefers explicit Sonoff entities:
      - ``number.*.valve_opening_degree`` = percent
      - ``number.*.valve_closing_degree`` = 100 - percent

    Parameters
    ----------
    self : ModelFixHost
            Better Thermostat host providing device state and HA access
    entity_id : str
            entity_id of the TRV
    percent : int
            the valve position to request, in percent

    Returns
    -------
    bool
            True when the requested position went out, False when no
            number entity matched or the device refused one of the writes
    """
    try:
        model = str(self.real_trvs[entity_id].model or "")
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
        opening_candidates: list[str] = []
        closing_candidates: list[str] = []
        generic_candidates: list[str] = []

        # Known translation_key values for Sonoff TRVZB valve entities.
        # These are stable, language-independent identifiers set by the integration.
        _TK_OPENING = {
            "valve_opening_degree",
            "valve_position",
            "pi_heating_demand",
            "heating_demand",
            "valve",
        }
        _TK_CLOSING = {"valve_closing_degree"}

        for ent in entity_registry.entities.values():
            if ent.device_id != device_id or ent.domain != "number":
                continue
            # Prefer translation_key (stable, language-independent)
            tk = getattr(ent, "translation_key", None)
            if tk:
                if tk in _TK_CLOSING:
                    closing_candidates.append(ent.entity_id)
                    continue
                if tk in _TK_OPENING:
                    opening_candidates.append(ent.entity_id)
                    continue
            # Fallback: string matching on entity_id / unique_id / original_name
            en = (ent.entity_id or "").lower()
            uid = (ent.unique_id or "").lower()
            name = (getattr(ent, "original_name", None) or "").lower()
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
    except (
        HomeAssistantError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ) as ex:
        # The device did not take the position: it is asleep, out of reach,
        # its integration is reloading, or the number entity declares a
        # narrower range than the clamp above. Reporting the refused write as
        # a declined one keeps the caller from recording a position the valve
        # never reached, and lets it fall back to its own valve channel.
        _LOGGER.warning(
            "better_thermostat %s: TRVZB valve write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False


async def override_set_valve(self: ModelFixHost, entity_id: str, percent: int) -> bool:
    """Override valve setting for TRVZB via number.* entity.

    Returns True if handled (write attempted), False to let adapter fallback run.
    """
    try:
        target_pct = max(0, min(100, int(percent)))

        trv_state = self.real_trvs.get(entity_id)
        if trv_state is None:
            return False

        # During valve maintenance we don't want to add additional delayed steps.
        if getattr(self, "in_maintenance", False):
            ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
            return bool(ok)

        # Cancel any previous pending delayed "bump then set".
        bump_pending = _cancel_pending_valve_bump(trv_state)

        last_pct_raw = trv_state.last_valve_percent
        try:
            last_pct = None if last_pct_raw is None else int(last_pct_raw)
        except TypeError, ValueError:
            last_pct = None

        # If we don't know the last commanded percent, just set directly.
        if last_pct is None:
            ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
            return bool(ok)

        # Only apply workaround when closing further, and only when the motor
        # was not already driven open by a bump whose write is still due.
        if target_pct < last_pct and not bump_pending:
            bump_pct = min(100, int(last_pct) + _TRVZB_CLOSE_BUMP_OPEN_DELTA_PCT)

            # If we can't "bump open", fall back to direct set.
            ok_bump = await maybe_set_sonoff_valve_percent(self, entity_id, bump_pct)
            if not ok_bump:
                ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
                return bool(ok)

            seq = int(trv_state.extra.get("_trvzb_valve_bump_seq", 0)) + 1
            trv_state.extra["_trvzb_valve_bump_seq"] = seq

            async def _delayed_set() -> None:
                try:
                    await asyncio.sleep(float(_TRVZB_CLOSE_BUMP_DELAY_S))
                    cur_state = self.real_trvs.get(entity_id)
                    if cur_state is None or (
                        int(cur_state.extra.get("_trvzb_valve_bump_seq", 0)) != seq
                    ):
                        return
                    await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
                except asyncio.CancelledError:
                    return
                except (RuntimeError, ValueError, KeyError) as ex:
                    _LOGGER.debug(
                        "better_thermostat %s: TRVZB delayed valve set exception: %s",
                        getattr(self, "device_name", "unknown"),
                        ex,
                    )

            trv_state.extra["_trvzb_valve_bump_task"] = (
                self.hass.async_create_background_task(
                    _delayed_set(), name=f"bt_trvzb_valve_bump_{entity_id}"
                )
            )
            return True

        # Opening, unchanged, or a close following a bump that has not run yet:
        # write the requested position. Bumping again would drive the valve
        # further open on every closing step while the target the cancelled
        # write was carrying never reaches the device.
        ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_pct)
        return bool(ok)
    except TypeError, ValueError, KeyError, AttributeError:
        return False


# Translation keys Zigbee2MQTT uses for the input the room temperature is
# mirrored into.
_TK_EXTERNAL_TEMP = frozenset({"external_temperature_input", "external_temperature"})

# Translation keys Zigbee2MQTT uses for the selector that decides which sensor
# the TRVZB regulates on.
_TK_SENSOR_SELECT = frozenset({"temperature_sensor_select", "temperature_sensor"})

# The option that hands regulation to the value BT writes. Devices offer more
# than one option naming an external sensor, so the ones already on such an
# option are left as their owner set them.
_EXTERNAL_SENSOR_OPTION = "external"


def _find_device_entity(
    entity_registry: er.EntityRegistry,
    device_id: str | None,
    domain: str,
    translation_keys: frozenset[str],
    id_fragment: str,
) -> str | None:
    """Return a sibling entity of ``device_id`` in ``domain``, or ``None``.

    The translation key is the stable, language-independent handle and is
    tried first; the id fragment is the fallback for a registry entry that
    carries none.

    Parameters
    ----------
    entity_registry : er.EntityRegistry
        The registry to search.
    device_id : str | None
        The device the sibling has to belong to. ``None`` is no device and
        matches nothing: every entity that belongs to no device would
        otherwise be a candidate.
    domain : str
        The entity domain to search, ``number`` or ``select`` here.
    translation_keys : frozenset[str]
        The translation keys that name the wanted entity.
    id_fragment : str
        Matched against the entity id, unique id and original name of a
        registry entry that carries no translation key.

    Returns
    -------
    str | None
        The entity id of the first match, or ``None`` when the device has
        no such entity.
    """
    if device_id is None:
        return None
    for ent in entity_registry.entities.values():
        if ent.device_id != device_id or ent.domain != domain:
            continue
        if getattr(ent, "translation_key", None) in translation_keys:
            return ent.entity_id
        haystacks = (
            (ent.entity_id or "").lower(),
            (ent.unique_id or "").lower(),
            (getattr(ent, "original_name", None) or "").lower().replace(" ", "_"),
        )
        if any(id_fragment in haystack for haystack in haystacks):
            return ent.entity_id
    return None


async def maybe_select_external_sensor(self: ModelFixHost, entity_id: str) -> bool:
    """Point the TRV's sensor selector at the value BT writes.

    Writing the external temperature input achieves nothing while the device
    regulates on its own sensor, and it lands there on its own: a TRVZB that
    is re-paired comes back on the internal sensor. So the selector is checked
    alongside every write of the input it belongs to.

    A device already on an option naming an external sensor is left alone,
    whichever of them it is: the choice between them is its owner's.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass`` and the context
        the service call is made under.
    entity_id : str
        The TRV whose device carries the selector.

    Returns
    -------
    bool
        True when the selector is on an external option, whether this call
        put it there or found it there.
    """
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        return False
    target = _find_device_entity(
        entity_registry,
        reg_entity.device_id,
        "select",
        _TK_SENSOR_SELECT,
        "temperature_sensor_select",
    )
    if target is None:
        _LOGGER.debug(
            "better_thermostat %s: TRVZB temperature sensor selector not found for %s",
            self.device_name,
            entity_id,
        )
        return False
    state = self.hass.states.get(target)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        # A selector that is not reporting names no option, and the device
        # behind it is in no state to take one either.
        return False
    if str(state.state).startswith(_EXTERNAL_SENSOR_OPTION):
        return True
    options = state.attributes.get("options")
    if not isinstance(options, (list, tuple)) or _EXTERNAL_SENSOR_OPTION not in options:
        _LOGGER.debug(
            "better_thermostat %s: TRVZB selector %s offers no '%s' option (%s)",
            self.device_name,
            target,
            _EXTERNAL_SENSOR_OPTION,
            options,
        )
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": target, "option": _EXTERNAL_SENSOR_OPTION},
        blocking=True,
        context=self.context,
    )
    _LOGGER.debug(
        "better_thermostat %s: set TRVZB %s from '%s' to '%s' (for %s)",
        self.device_name,
        target,
        state.state,
        _EXTERNAL_SENSOR_OPTION,
        entity_id,
    )
    return True


async def maybe_set_external_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Set Sonoff TRVZB external temperature input via a number entity on the same device.

    Looks for number.* entity matching external_temperature_input and writes the
    given temperature (clamped to 0..99.9, rounded to one decimal). The sensor
    selector is pointed at that input alongside the write, because a device
    regulating on its own sensor never reads it.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass``, the TRV registry
        and the context the service calls are made under.
    entity_id : str
        The TRV whose device carries the input.
    temperature : float
        The room temperature to mirror into the device, in degrees Celsius.

    Returns
    -------
    bool
        True when the input was written, False when the device is not a
        TRVZB, names no such input, or the value is not a number.
    """
    try:
        model = str(self.real_trvs[entity_id].model or "")
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
        target = _find_device_entity(
            entity_registry,
            reg_entity.device_id,
            "number",
            _TK_EXTERNAL_TEMP,
            "external_temperature_input",
        )
        if target is None:
            _LOGGER.debug(
                "better_thermostat %s: TRVZB external_temperature_input number entity not found for %s",
                self.device_name,
                entity_id,
            )
            return False

        # Clamp and round
        try:
            val = float(temperature)
        except TypeError, ValueError:
            _LOGGER.debug(
                "better_thermostat %s: TRVZB maybe_set_external_temperature got non-float: %s",
                self.device_name,
                temperature,
            )
            return False
        val = max(0.0, min(99.9, round(val, 1)))

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
        # The value just written only reaches the control loop of a device
        # that is regulating on it.
        await maybe_select_external_sensor(self, entity_id)
        return True
    except (
        HomeAssistantError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ) as ex:
        # The device did not take the value: it is asleep, out of reach, its
        # integration is reloading, or it declares a narrower range than the
        # clamp above. Reporting the refused write as a declined one leaves
        # the caller free to serve the remaining TRVs and to control on the
        # new reading; the next write retries.
        _LOGGER.warning(
            "better_thermostat %s: TRVZB external temperature write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False
