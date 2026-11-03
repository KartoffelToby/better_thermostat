"""Quirks and helpers for Sonoff TRVZB (Zigbee TRV) devices.

Provides Sonoff TRVZB specific helper functions such as writing valve
percentages and mirroring external temperature into the TRV when supported.
"""

from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    MaintenanceIntervalQuirk,
    ModelFixHost,
    ModelFixTrv,
    ModelQuirks,
    ValveChannelQuirk,
    ValveQuirk,
    ValveReading,
    ValveReadingsQuirk,
)
from custom_components.better_thermostat.utils.helpers import (
    device_released,
    external_temperature_input_value,
    find_keyed_device_entity,
    is_sibling_entry,
    keeps_sensor_selection,
    state_is_assumed,
    state_is_live,
)

_LOGGER = logging.getLogger(__name__)

VALVE_MAINTENANCE_INTERVAL_HOURS = 84

# The Sonoff TRV Gen2 reports model_id "TRV-ZBT", which neither Sonoff match
# below takes in. It shares the external temperature input and the sensor
# selector with the TRVZB, but not the valve numbers.
_TRV_ZBT_MODEL = "trv-zbt"

# Some users report that the TRVZB motor can occasionally lose its calibration and
# fail to fully close the valve when commanded to very small openings.
#
# Workaround: when requesting a further close (target_percent < last_percent), briefly
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


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Return unchanged local calibration for TRVZB by default."""
    return calibration_offset


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


# Translation keys Sonoff TRVZB valve entities carry. They are stable,
# language-independent identifiers set by the integration.
_TK_OPENING = frozenset(
    {
        "valve_opening_degree",
        "valve_position",
        "pi_heating_demand",
        "heating_demand",
        "valve",
    }
)
_TK_CLOSING = frozenset({"valve_closing_degree"})


class _ValveNumberCandidates(NamedTuple):
    """The enabled number entities a TRVZB's valve can be written to."""

    opening: list[str]
    """Numbers that take the opening degree, in registry order."""
    closing: list[str]
    """Numbers that take the closing degree, in registry order."""
    generic: list[str]
    """Other valve-like numbers, in registry order."""


def _valve_number_candidates(
    self: ModelFixHost, entity_id: str
) -> _ValveNumberCandidates | None:
    """Collect the enabled valve number entities on the TRV's device.

    Returns the opening, closing and generic candidates, or ``None`` when
    the TRV is no Sonoff model or has no registry entry.
    """
    model = self.real_trvs[entity_id].model
    # The TRV-ZBT's valve numbers configure its own controller and position
    # nothing, so it is ruled out before the Sonoff match below takes it in.
    if _TRV_ZBT_MODEL in model.lower():
        _LOGGER.debug(
            "better_thermostat %s: TRVZB valve lookup skipped for the TRV-ZBT (model=%s)",
            self.device_name,
            model,
        )
        return None
    # Only attempt for Sonoff TRVZB
    if not ("sonoff" in model.lower() or "trvzb" in model.lower() or model == "TRVZB"):
        _LOGGER.debug(
            "better_thermostat %s: TRVZB valve lookup skipped (model=%s)",
            self.device_name,
            model,
        )
        return None
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        _LOGGER.debug(
            "better_thermostat %s: TRVZB valve lookup: no registry entity for %s",
            self.device_name,
            entity_id,
        )
        return None
    device_id = reg_entity.device_id
    opening_candidates: list[str] = []
    closing_candidates: list[str] = []
    generic_candidates: list[str] = []

    for ent in entity_registry.entities.values():
        if not is_sibling_entry(ent, device_id) or ent.domain != "number":
            continue
        # Prefer translation_key (stable, language-independent)
        tk = ent.translation_key
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
        name = (ent.original_name or "").lower()
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
    return _ValveNumberCandidates(
        opening_candidates, closing_candidates, generic_candidates
    )


class _ValveWrite(NamedTuple):
    """One number a TRVZB valve position is written to."""

    entity_id: str
    """The number entity."""
    value: int
    """The value that number takes for the position."""
    label: str
    """What the number is, for log lines."""


def _valve_writes(
    candidates: _ValveNumberCandidates, percent: int
) -> list[_ValveWrite]:
    """Return the numbers a valve position goes to, with the value of each.

    The opening degree takes the position and the closing degree its
    complement; a device with neither takes the position on the generic
    number that names a valve or a position most plainly.
    """
    writes: list[_ValveWrite] = []
    if candidates.opening:
        writes.append(
            _ValveWrite(candidates.opening[0], percent, "valve_opening_degree")
        )
    if candidates.closing:
        writes.append(
            _ValveWrite(candidates.closing[0], 100 - percent, "valve_closing_degree")
        )
    if not writes and candidates.generic:
        generic = sorted(
            candidates.generic, key=lambda x: ("valve" not in x, "position" not in x)
        )
        writes.append(_ValveWrite(generic[0], percent, "generic valve percent"))
    return writes


def has_valve_channel(self: ModelFixHost, entity_id: str) -> bool:
    """Whether the TRV's device carries an enabled number to write the valve to.

    Without one, ``override_set_valve`` declines every position, and that
    lasts until the user enables or adds such an entity, so the valve is
    not pursued through this quirk meanwhile.
    """
    candidates = _valve_number_candidates(self, entity_id)
    return candidates is not None and any(candidates)


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
            number entity matched, the device refused one of the writes or
            the device is released
    """
    # A position that follows an earlier write, the close after a bump or
    # a fallback after a refused bump, is not sent once the device is the
    # user's. The opening and closing degree of one position go out together.
    if device_released(self, entity_id):
        return False
    try:
        candidates = _valve_number_candidates(self, entity_id)
        if candidates is None:
            return False
        clamped_percent = max(0, min(100, percent))
        _LOGGER.debug(
            "better_thermostat %s: TRVZB valve write candidates (open=%s, close=%s, generic=%s) target=%s%% for %s",
            self.device_name,
            candidates.opening,
            candidates.closing,
            candidates.generic,
            clamped_percent,
            entity_id,
        )
        writes = _valve_writes(candidates, clamped_percent)
        for write in writes:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": write.entity_id, "value": write.value},
                blocking=True,
                context=self.context,
            )
            _LOGGER.debug(
                "better_thermostat %s: set TRVZB %s=%s on %s (for %s)",
                self.device_name,
                write.label,
                write.value,
                write.entity_id,
                entity_id,
            )
        wrote = bool(writes)

        if not wrote:
            _LOGGER.debug(
                "better_thermostat %s: TRVZB valve percent write had no matching number entity (target=%s%%, %s)",
                self.device_name,
                clamped_percent,
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
        target_percent = max(0, min(100, percent))

        trv_state = self.real_trvs.get(entity_id)
        if trv_state is None:
            return False

        # During valve maintenance we don't want to add additional delayed steps,
        # and a delayed step still pending from before would overwrite the
        # position maintenance drives the valve to.
        if self.in_maintenance:
            _cancel_pending_valve_bump(trv_state)
            trv_state.extra["_trvzb_valve_bump_seq"] = (
                trv_state.extra.get("_trvzb_valve_bump_seq", 0) + 1
            )
            ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_percent)
            return ok

        # Cancel any previous pending delayed "bump then set".
        bump_pending = _cancel_pending_valve_bump(trv_state)

        # The delegate records the position only once it is a finite int.
        last_percent_raw = trv_state.last_valve_percent
        last_percent = None if last_percent_raw is None else int(last_percent_raw)

        # If we don't know the last commanded percent, just set directly.
        if last_percent is None:
            ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_percent)
            return ok

        # Only apply workaround when closing further, and only when the motor
        # was not already driven open by a bump whose write is still due.
        if target_percent < last_percent and not bump_pending:
            bump_percent = min(100, last_percent + _TRVZB_CLOSE_BUMP_OPEN_DELTA_PCT)

            # If we can't "bump open", fall back to direct set.
            ok_bump = await maybe_set_sonoff_valve_percent(
                self, entity_id, bump_percent
            )
            if not ok_bump:
                ok = await maybe_set_sonoff_valve_percent(
                    self, entity_id, target_percent
                )
                return ok

            seq = trv_state.extra.get("_trvzb_valve_bump_seq", 0) + 1
            trv_state.extra["_trvzb_valve_bump_seq"] = seq

            async def _delayed_set() -> None:
                try:
                    await asyncio.sleep(float(_TRVZB_CLOSE_BUMP_DELAY_S))
                    if self.is_removed:
                        return
                    cur_state = self.real_trvs.get(entity_id)
                    if cur_state is None or (
                        cur_state.extra.get("_trvzb_valve_bump_seq", 0) != seq
                    ):
                        return
                    await maybe_set_sonoff_valve_percent(
                        self, entity_id, target_percent
                    )
                except asyncio.CancelledError:
                    return
                except (RuntimeError, ValueError, KeyError) as ex:
                    _LOGGER.debug(
                        "better_thermostat %s: TRVZB delayed valve set exception: %s",
                        self.device_name,
                        ex,
                    )

            # The thermostat owns the deferred write, so unloading or removing
            # it cancels the write along with the rest of its work.
            bump_task = self.task_manager.create_task(
                _delayed_set(), name=f"bt_trvzb_valve_bump_{entity_id}"
            )
            if bump_task is None:
                # The thermostat is being removed and starts no deferred
                # work. The bump has opened the valve already, so the target
                # goes out now rather than leaving it open.
                ok = await maybe_set_sonoff_valve_percent(
                    self, entity_id, target_percent
                )
                return ok
            trv_state.extra["_trvzb_valve_bump_task"] = bump_task
            return True

        # Opening, unchanged, or a close following a bump that has not run yet:
        # write the requested position. Bumping again would drive the valve
        # further open on every closing step while the target the cancelled
        # write was carrying never reaches the device.
        ok = await maybe_set_sonoff_valve_percent(self, entity_id, target_percent)
        return ok
    except TypeError, ValueError, KeyError, AttributeError:
        return False


def valve_readings(
    self: ModelFixHost, entity_id: str, percent: int
) -> list[ValveReading]:
    """Name the numbers a write of ``percent`` set, with the value of each.

    While the write that follows a close bump is still due, the device
    holds the bump's opening on purpose, so nothing is offered to compare
    against until that write has gone out.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access
    entity_id : str
        entity_id of the TRV
    percent : int
        the valve position that was written, in percent

    Returns
    -------
    list[ValveReading]
        Each number the write went to, with its current state and the value
        it holds once the device took the position; empty when no number
        matched or a bump is under way
    """
    trv_state = self.real_trvs.get(entity_id)
    if trv_state is None:
        return []
    bump_task = trv_state.extra.get("_trvzb_valve_bump_task")
    if bump_task is not None and not bump_task.done():
        return []
    candidates = _valve_number_candidates(self, entity_id)
    if candidates is None:
        return []
    return [
        ValveReading(self.hass.states.get(write.entity_id), float(write.value))
        for write in _valve_writes(candidates, max(0, min(100, percent)))
    ]


# Translation keys for the input the room temperature is mirrored into:
# Zigbee2MQTT's, and ZHA's ``external_temperature_sensor_value``.
_TK_EXTERNAL_TEMP = frozenset(
    {
        "external_temperature_input",
        "external_temperature",
        "external_temperature_sensor_value",
    }
)

# Translation keys Zigbee2MQTT uses for the selector that decides which sensor
# the TRVZB regulates on.
_TK_SENSOR_SELECT = frozenset({"temperature_sensor_select", "temperature_sensor"})

# Translation key ZHA uses for the same choice, which it exposes as a switch:
# on regulates on the external input, off on the device's own sensor.
_TK_SENSOR_SWITCH = frozenset({"external_temperature_sensor"})

# The option that hands regulation to the value BT writes, under each name
# Zigbee2MQTT has given it: ``external`` up to 2.14.1, ``remote_temperature``
# from 2.14.2 on.
_EXTERNAL_SENSOR_OPTIONS = ("external", "remote_temperature")

# The option that regulates on the device's own sensor: ``internal`` up to
# Zigbee2MQTT 2.14.1, ``local_temperature`` from 2.14.2 on.
_INTERNAL_SENSOR_OPTIONS = ("internal", "local_temperature")

# Selections that already regulate on a remote value. Devices offer more than
# one option naming an external sensor, so a device on any of them is left as
# its owner set it. ``remote_source_offline`` is the device reporting that it
# fell back to its own sensor while the remote one is gone; it switches back
# by itself once the remote value returns.
_ON_A_REMOTE_SENSOR = ("external", "remote_")

# The device's report that it fell back to its own sensor.
_REMOTE_SOURCE_OFFLINE = "remote_source_offline"


def _sensor_selector(self: ModelFixHost, entity_id: str) -> tuple[str, State] | None:
    """Return the TRV's sensor selector and its state, or ``None``.

    Zigbee2MQTT offers the choice as a select. ZHA offers it as a switch,
    which stands in for the select on a device that carries none.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass``.
    entity_id : str
        The TRV whose device carries the selector.

    Returns
    -------
    tuple[str, State] | None
        The entity id of the select or the switch and its state, or ``None``
        when the device has neither or it is not reporting.
    """
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        return None
    target = find_keyed_device_entity(
        entity_registry,
        reg_entity.device_id,
        "select",
        _TK_SENSOR_SELECT,
        "temperature_sensor_select",
    )
    if target is None:
        target = find_keyed_device_entity(
            entity_registry,
            reg_entity.device_id,
            "switch",
            _TK_SENSOR_SWITCH,
            "external_temperature_sensor",
        )
    if target is None:
        _LOGGER.debug(
            "better_thermostat %s: TRVZB temperature sensor selector not found for %s",
            self.device_name,
            entity_id,
        )
        return None
    state = self.hass.states.get(target)
    if not state_is_live(state):
        # A selector that is not reporting names no option, and the device
        # behind it is in no state to take one either.
        return None
    return target, state


def _is_sensor_switch(target: str) -> bool:
    """Whether the sensor choice ``target`` is ZHA's switch, not a select."""
    return target.split(".", 1)[0] == "switch"


async def _set_sensor_switch(
    self: ModelFixHost, entity_id: str, target: str, state: State, on: bool
) -> bool:
    """Turn ZHA's sensor switch ``target`` on or off unless it already is.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass`` and the context
        the service call is made under.
    entity_id : str
        The TRV whose device carries the switch, for log lines.
    target : str
        The switch entity.
    state : State
        The state the switch reports.
    on : bool
        True to regulate on the external input, False on the own sensor.

    Returns
    -------
    bool
        True when this call switched it.
    """
    if state.state == (STATE_ON if on else STATE_OFF):
        return False
    await self.hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {"entity_id": target},
        blocking=True,
        context=self.context,
    )
    _LOGGER.debug(
        "better_thermostat %s: turned TRVZB %s %s from '%s' (for %s)",
        self.device_name,
        target,
        "on" if on else "off",
        state.state,
        entity_id,
    )
    return True


async def maybe_select_external_sensor(self: ModelFixHost, entity_id: str) -> bool:
    """Point the TRV's sensor selector at the value BT writes.

    Writing the external temperature input achieves nothing while the device
    regulates on its own sensor, and it lands there on its own: a TRVZB that
    is re-paired comes back on the internal sensor. So the selector is checked
    alongside every write of the input it belongs to.

    A device already on an option naming an external sensor is left alone,
    whichever of them it is: the choice between them is its owner's. ZHA's
    switch is turned on unless it is on already. A selector whose state is
    assumed shows the option last sent to it, not the device's, so that
    option is sent again; an assumed ``remote_source_offline`` is no option
    anyone chose and is replaced by the external option.

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
        True when the selector is on an external option, or the switch is
        on, whether this call put it there or found it there.
    """
    selector = _sensor_selector(self, entity_id)
    if selector is None:
        return False
    target, state = selector
    if _is_sensor_switch(target):
        await _set_sensor_switch(self, entity_id, target, state, on=True)
        return True
    on_a_remote_sensor = state.state.startswith(_ON_A_REMOTE_SENSOR)
    if on_a_remote_sensor and not state_is_assumed(state):
        return True
    options = state.attributes.get("options")
    offered = options if isinstance(options, (list, tuple)) else ()
    if (
        on_a_remote_sensor
        and state.state != _REMOTE_SOURCE_OFFLINE
        and state.state in offered
    ):
        option: str | None = state.state
    else:
        option = next(
            (name for name in _EXTERNAL_SENSOR_OPTIONS if name in offered), None
        )
    if option is None:
        _LOGGER.debug(
            "better_thermostat %s: TRVZB selector %s offers none of %s (%s)",
            self.device_name,
            target,
            _EXTERNAL_SENSOR_OPTIONS,
            options,
        )
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": target, "option": option},
        blocking=True,
        context=self.context,
    )
    _LOGGER.debug(
        "better_thermostat %s: set TRVZB %s from '%s' to '%s' (for %s)",
        self.device_name,
        target,
        state.state,
        option,
        entity_id,
    )
    return True


async def maybe_select_internal_sensor(self: ModelFixHost, entity_id: str) -> bool:
    """Point the TRV's sensor selector back at its own sensor.

    A room sensor on the TRV's own device is that device's reading. A device
    regulating on a remote value it was fed from that reading reports the
    fed value as its own and stops following the room, so a selector found on
    a remote option is moved back, and ZHA's switch found on is turned off.

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
        True when this call moved the selector onto the internal option or
        turned the switch off.
    """
    selector = _sensor_selector(self, entity_id)
    if selector is None:
        return False
    target, state = selector
    if _is_sensor_switch(target):
        return await _set_sensor_switch(self, entity_id, target, state, on=False)
    if not state.state.startswith(_ON_A_REMOTE_SENSOR):
        return False
    options = state.attributes.get("options")
    option = next(
        (
            name
            for name in _INTERNAL_SENSOR_OPTIONS
            if isinstance(options, (list, tuple)) and name in options
        ),
        None,
    )
    if option is None:
        _LOGGER.debug(
            "better_thermostat %s: TRVZB selector %s offers none of %s (%s)",
            self.device_name,
            target,
            _INTERNAL_SENSOR_OPTIONS,
            options,
        )
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": target, "option": option},
        blocking=True,
        context=self.context,
    )
    _LOGGER.debug(
        "better_thermostat %s: set TRVZB %s from '%s' back to '%s' (for %s)",
        self.device_name,
        target,
        state.state,
        option,
        entity_id,
    )
    return True


# Range of the external temperature input, in degrees Celsius, for an input
# that publishes no bounds of its own.
_EXTERNAL_TEMPERATURE_MIN_C = 0.0
_EXTERNAL_TEMPERATURE_MAX_C = 99.9


async def maybe_set_external_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Set Sonoff TRVZB external temperature input via a number entity on the same device.

    Looks for the number.* entity of the external temperature input, under
    Zigbee2MQTT's or ZHA's translation key, and writes the given temperature, rounded to a tenth of a degree Celsius and converted into
    the unit the input publishes, held inside the range it publishes. The sensor
    selector is pointed at that input alongside the write, because a device
    regulating on its own sensor never reads it. A TRV whose
    ``keep_sensor_selection`` option is on keeps the option the user picked;
    its input is written all the same.

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
        TRVZB or TRV-ZBT, names no such input, or the value is not a number.
    """
    try:
        model = self.real_trvs[entity_id].model
        if not (
            "sonoff" in model.lower()
            or "trvzb" in model.lower()
            or model == "TRVZB"
            or _TRV_ZBT_MODEL in model.lower()
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
        # A room sensor on the TRV's own device is the TRV's reading: mirroring
        # it back would freeze it at the value of the first write. A selector
        # an earlier write left on the remote option is moved back, unless the
        # user keeps the selection.
        room_sensor = entity_registry.async_get(self.sensor_entity_id)
        if (
            room_sensor is not None
            and room_sensor.device_id is not None
            and room_sensor.device_id == reg_entity.device_id
        ):
            _LOGGER.debug(
                "better_thermostat %s: TRVZB external temperature not mirrored: room sensor %s belongs to %s",
                self.device_name,
                self.sensor_entity_id,
                entity_id,
            )
            if not keeps_sensor_selection(self, entity_id):
                await maybe_select_internal_sensor(self, entity_id)
            return False
        target = find_keyed_device_entity(
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

        try:
            celsius = float(temperature)
        except TypeError, ValueError:
            _LOGGER.debug(
                "better_thermostat %s: TRVZB maybe_set_external_temperature got non-float: %s",
                self.device_name,
                temperature,
            )
            return False
        value = external_temperature_input_value(
            self.hass.states.get(target),
            celsius,
            _EXTERNAL_TEMPERATURE_MIN_C,
            _EXTERNAL_TEMPERATURE_MAX_C,
        )

        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": target, "value": value},
            blocking=True,
            context=self.context,
        )
        _LOGGER.debug(
            "better_thermostat %s: set TRVZB external_temperature_input=%.2f on %s (for %s)",
            self.device_name,
            value,
            target,
            entity_id,
        )
        # The value just written only reaches the control loop of a device
        # that is regulating on it. The selector is a write of its own, and
        # none goes out once the device is the user's or the user keeps the
        # selection. The input is written either way, so a device that is
        # switched to it later starts from the current room temperature.
        if device_released(self, entity_id) or keeps_sensor_selection(self, entity_id):
            return True
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


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    VALVE_MAINTENANCE_INTERVAL_HOURS = VALVE_MAINTENANCE_INTERVAL_HOURS
    maybe_set_external_temperature = staticmethod(maybe_set_external_temperature)
    override_set_valve = staticmethod(override_set_valve)
    has_valve_channel = staticmethod(has_valve_channel)
    valve_readings = staticmethod(valve_readings)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_EXTERNAL_TEMPERATURE_QUIRK: ExternalTemperatureQuirk = _Surface()
_MAINTENANCE_INTERVAL_QUIRK: MaintenanceIntervalQuirk = _Surface()
_VALVE_QUIRK: ValveQuirk = _Surface()
_VALVE_CHANNEL_QUIRK: ValveChannelQuirk = _Surface()
_VALVE_READINGS_QUIRK: ValveReadingsQuirk = _Surface()
