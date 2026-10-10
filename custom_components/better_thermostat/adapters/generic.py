"""Generic adapter helpers used by multiple TRV integrations.

This module implements the generic, default behaviour for TRV adapters
used by Better Thermostat when a device-specific adapter does not exist.
"""

from __future__ import annotations

import asyncio
from itertools import pairwise
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..utils.helpers import (
    celsius_to_system_temperature,
    find_local_calibration_entity,
    normalize_hvac_mode,
    supports_single_target_temperature,
    supports_temperature_range,
)
from .base import (
    AdapterCapabilities,
    offset_scale,
    offset_to_published,
    published_step_to_offset,
    published_to_offset,
    wait_for_calibration_entity_or_timeout,
)
from .delegate import set_hvac_mode as delegate_set_hvac_mode
from .types import AdapterHost, AdapterProbeHost

if TYPE_CHECKING:
    from homeassistant.core import State

_LOGGER = logging.getLogger(__name__)

# Generic HA climate entities: offset via a discovered number entity,
# no valve-position channel.
CAPABILITIES = AdapterCapabilities(offset_write=True, valve_write=False)

# What a calibration entity is taken to offer while it declares nothing
# itself. An entity that publishes no ``min``, ``max`` or ``step`` and an
# entity that has not reported a state yet leave a caller in the same
# position, so both get the same answer: the widest span common TRV
# firmware accepts, leaving the device's own limits to do the rest.
# Every adapter whose offset rides on a discovered entity shares this
# table, so the same undeclared device cannot come out with two different
# ranges depending on which ecosystem found it.
DEFAULT_OFFSET_MIN: Final = -10.0
DEFAULT_OFFSET_MAX: Final = 10.0
DEFAULT_OFFSET_STEP: Final = 1.0


def _option_to_offset(option: str) -> float | None:
    """Read the offset an option of a select-backed calibration entity carries.

    Parameters
    ----------
    option : str
        Option as the entity publishes it, with or without the Kelvin
        suffix (e.g. ``"-1.5k"``).

    Returns
    -------
    float or None
        Offset in Kelvin, or None when the option carries no number.
    """
    try:
        return float(option.replace("k", ""))
    except ValueError, TypeError:
        return None


async def get_info(self: AdapterProbeHost, entity_id: str) -> dict[str, bool]:
    """Get info from TRV."""
    support_offset = False

    calibration_entity_id = await find_local_calibration_entity(self, entity_id)
    if calibration_entity_id is not None:
        support_offset = True
    return {"support_offset": support_offset, "support_valve": False}


async def discover_calibration_entity(self: AdapterHost, entity_id: str) -> None:
    """Adopt the TRV's local calibration entity and wait for it to report.

    A TRV that already carries a calibration entity, and one that is not
    calibrated through such an entity at all, is left alone. Otherwise the
    lookup runs once and its result is stored on the TRV record: an entity
    that was found is waited for until it reports a state, and a TRV for
    which the lookup found none is named in the log, because local
    calibration is what it is configured for and it has nothing to write
    to.

    Every adapter whose calibration rides on a discovered entity shares
    this step, so a TRV without one is reported the same way whichever
    ecosystem it belongs to.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to run the lookup for.
    """
    trv = self.real_trvs[entity_id]
    if trv.local_temperature_calibration_entity is not None or trv.calibration == 1:
        return

    trv.local_temperature_calibration_entity = await find_local_calibration_entity(
        self, entity_id, trv=trv
    )
    _LOGGER.debug(
        "better_thermostat %s: uses local calibration entity %s",
        self.device_name,
        trv.local_temperature_calibration_entity,
    )
    if trv.local_temperature_calibration_entity is None:
        _LOGGER.warning(
            "better_thermostat %s: no local calibration entity found for '%s', skipping calibration init",
            self.device_name,
            entity_id,
        )
        return

    await wait_for_calibration_entity_or_timeout(
        self, entity_id, trv.local_temperature_calibration_entity
    )


async def init(self: AdapterHost, entity_id: str) -> None:
    """Initialize the generic adapter for a TRV entity.

    A generic climate entity exposes no channel of its own, so adopting a
    local calibration entity is the whole of it.
    """
    await discover_calibration_entity(self, entity_id)


async def get_calibration_offset(self: AdapterHost, entity_id: str) -> float:
    """Read the offset the calibration entity currently reports.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to read for.

    Returns
    -------
    float
        Offset in Kelvin, whatever unit the entity publishes it in, or 0.0
        when the TRV has no calibration entity or it reports nothing
        readable.
    """
    calibration_entity = self.real_trvs[entity_id].local_temperature_calibration_entity
    if calibration_entity is not None:
        state = self.hass.states.get(calibration_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return 0.0
        try:
            # For SELECT entities, remove the 'k' suffix if present (e.g., "1.5k" -> "1.5")
            state_str = state.state.replace("k", "")
            return published_to_offset(offset_scale(state), float(state_str))
        except ValueError, TypeError:
            _LOGGER.warning(
                "better_thermostat %s: Could not convert calibration offset '%s' to float, using 0",
                self.device_name,
                state.state,
            )
            return 0.0
    else:
        return 0.0


def _calibration_state(self: AdapterHost, entity_id: str) -> State | None:
    """Read the state of the TRV's calibration entity, if there is one.

    A TRV for which the lookup found no calibration entity and one whose
    entity reports nothing yet both leave the bounds undeclared, and the
    state machine takes an entity id rather than the ``None`` the first
    case holds, so the two are separated here once instead of in every
    getter.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV whose calibration entity is read.

    Returns
    -------
    State or None
        State of the calibration entity, or None when the TRV has none or
        it has not reported.
    """
    calibration_entity = self.real_trvs[entity_id].local_temperature_calibration_entity
    if calibration_entity is None:
        return None
    return self.hass.states.get(calibration_entity)


def _offered_offsets(state: State) -> list[float]:
    """Read the offsets the options of a select-backed entity stand for.

    An option that carries no number is left out rather than discarding
    the whole list, which is how the write path treats the same option
    when it snaps a request onto what the entity offers.

    Parameters
    ----------
    state : State
        State of the calibration entity, carrying its ``options``.

    Returns
    -------
    list of float
        Offset each usable option stands for.
    """
    # Attribute values are untyped, so each option is read as text, as the
    # write path reads the same list.
    parsed = [
        _option_to_offset(str(option))
        for option in state.attributes.get("options") or []
    ]
    return [value for value in parsed if value is not None]


async def get_calibration_offset_step(self: AdapterHost, entity_id: str) -> float:
    """Read the granularity the calibration entity accepts.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to read for.

    Returns
    -------
    float
        Step a number entity publishes, the smallest spacing between the
        options of a select, or the shared default when neither is
        readable.
    """
    state = _calibration_state(self, entity_id)
    if state is None:
        return DEFAULT_OFFSET_STEP
    if state.domain == "select":
        offered = sorted(set(_offered_offsets(state)))
        spacings = [high - low for low, high in pairwise(offered)]
        return min(spacings, default=DEFAULT_OFFSET_STEP)
    if "step" not in state.attributes:
        return DEFAULT_OFFSET_STEP
    return published_step_to_offset(
        offset_scale(state), float(str(state.attributes["step"]))
    )


async def get_min_calibration_offset(self: AdapterHost, entity_id: str) -> float:
    """Read the lowest offset the calibration entity accepts.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to read for.

    Returns
    -------
    float
        Lowest offset the entity offers: the smallest of a select's
        options, the ``min`` a number entity publishes, or the shared
        default when neither is readable.
    """
    state = _calibration_state(self, entity_id)
    if state is None:
        return DEFAULT_OFFSET_MIN
    if state.domain == "select":
        return min(_offered_offsets(state), default=DEFAULT_OFFSET_MIN)
    if "min" not in state.attributes:
        return DEFAULT_OFFSET_MIN
    return published_to_offset(offset_scale(state), float(str(state.attributes["min"])))


async def get_max_calibration_offset(self: AdapterHost, entity_id: str) -> float:
    """Read the highest offset the calibration entity accepts.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to read for.

    Returns
    -------
    float
        Highest offset the entity offers: the largest of a select's
        options, the ``max`` a number entity publishes, or the shared
        default when neither is readable.
    """
    state = _calibration_state(self, entity_id)
    if state is None:
        return DEFAULT_OFFSET_MAX
    if state.domain == "select":
        return max(_offered_offsets(state), default=DEFAULT_OFFSET_MAX)
    if "max" not in state.attributes:
        return DEFAULT_OFFSET_MAX
    return published_to_offset(offset_scale(state), float(str(state.attributes["max"])))


def _setpoint_payload(
    state: State | None, entity_id: str, temperature: float
) -> dict[str, str | float]:
    """Build the set_temperature payload the head accepts.

    A head that advertises only TARGET_TEMPERATURE_RANGE rejects a
    ``temperature`` write and publishes its heating setpoint as
    ``target_temp_low``, so the setpoint goes out as the lower bound. The upper
    bound the head holds travels along unchanged, raised to the lower bound
    when it sits below it, because Home Assistant refuses a lower bound above
    the upper one. Every other head gets the single-setpoint payload.

    Parameters
    ----------
    state : State | None
            the head's current state, whose attributes are in the system unit
    entity_id : str
            entity_id of the head
    temperature : float
            the setpoint in the system unit

    Returns
    -------
    dict[str, str | float]
            the service data for ``climate.set_temperature``
    """
    if (
        state is None
        or not supports_temperature_range(state)
        or supports_single_target_temperature(state)
    ):
        return {"entity_id": entity_id, "temperature": temperature}
    try:
        high = max(float(state.attributes["target_temp_high"]), temperature)
    except KeyError, TypeError, ValueError:
        high = temperature
    return {
        "entity_id": entity_id,
        "target_temp_low": temperature,
        "target_temp_high": high,
    }


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Set new target temperature."""
    temperature = celsius_to_system_temperature(self.hass, temperature)
    await self.hass.services.async_call(
        "climate",
        "set_temperature",
        _setpoint_payload(self.hass.states.get(entity_id), entity_id, temperature),
        blocking=True,
        context=self.context,
    )


async def set_hvac_mode(self: AdapterHost, entity_id: str, hvac_mode: str) -> None:
    """Set new target hvac mode.

    A write the device or Home Assistant refuses raises, so the caller can
    retry it and tell a refused mode from one that went out.
    """

    hvac_mode_norm = normalize_hvac_mode(hvac_mode)
    _LOGGER.debug(
        "better_thermostat %s: set_hvac_mode %s -> %s",
        self.device_name,
        hvac_mode,
        hvac_mode_norm,
    )
    await self.hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": entity_id, "hvac_mode": hvac_mode_norm},
        blocking=True,
        context=self.context,
    )


def _published_offset(state: State | None, calibration_offset: float) -> float:
    """Return the value a calibration number takes for ``calibration_offset``.

    The value is converted into the unit the entity publishes and held
    inside the ``min`` and ``max`` it publishes, which are what Home
    Assistant checks a written value against. The offset was clamped in
    Kelvin already; holding the converted value inside the published bounds
    keeps the float error of the round trip from pushing a value at a bound
    just outside it.

    Parameters
    ----------
    state : State or None
        State of the calibration number, or None when it has none yet.
    calibration_offset : float
        Offset in Kelvin.

    Returns
    -------
    float
        The value to write, in the entity's published unit.
    """
    value = offset_to_published(offset_scale(state), calibration_offset)
    if state is None:
        return value
    for bound, pick in (("min", max), ("max", min)):
        try:
            value = pick(value, float(str(state.attributes[bound])))
        except KeyError, TypeError, ValueError:
            continue
    return value


async def set_calibration_offset(
    self: AdapterHost, entity_id: str, calibration_offset: float
) -> bool:
    """Write a calibration offset to the discovered calibration entity.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to write to
    calibration_offset : float
        Calibration offset in Kelvin, clamped to the device's declared range

    Returns
    -------
    bool
        True once the write went out, False when no calibration entity was
        discovered for this TRV and there is nothing to write to.
    """
    calibration_entity = self.real_trvs[entity_id].local_temperature_calibration_entity
    if calibration_entity is not None:
        max_calibration = await get_max_calibration_offset(self, entity_id)
        min_calibration = await get_min_calibration_offset(self, entity_id)

        calibration_offset = min(max_calibration, calibration_offset)
        calibration_offset = max(min_calibration, calibration_offset)

        entity_state = self.hass.states.get(calibration_entity)

        # Derive domain safely - from entity_state if available, otherwise from entity_id
        domain = (
            entity_state.domain if entity_state else calibration_entity.split(".", 1)[0]
        )

        # Check if it's a SELECT entity or NUMBER entity
        if domain == "select":
            # For SELECT entities, format with 'k' suffix (e.g., "1.5k")
            rounded = f"{calibration_offset:.1f}"
            option_value = f"{rounded}k"
            # The option carries the value that goes on the wire, and the
            # confirmation compares the device's report against it. Both the
            # snap onto the option list and the one-decimal format move the
            # value, so the command is the number the chosen option stands for.
            commanded = float(rounded)

            # Get available options (handle None entity_state gracefully)
            options: list[str] = []
            if entity_state:
                options = [
                    str(option)
                    for option in entity_state.attributes.get("options") or []
                ]

            # Validate and snap to closest matching option if needed
            if options:
                if option_value not in options:
                    # Parse all options and find the closest match
                    parsed_options = {}
                    for opt in options:
                        parsed = _option_to_offset(opt)
                        if parsed is not None:
                            parsed_options[opt] = parsed

                    if parsed_options:
                        # Find option with minimum distance to target offset
                        closest_option = min(
                            parsed_options,
                            key=lambda opt: abs(
                                parsed_options[opt] - calibration_offset
                            ),
                        )
                        option_value = closest_option
                        commanded = parsed_options[closest_option]

            calibration_offset = commanded

            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": calibration_entity, "option": option_value},
                blocking=True,
                context=self.context,
            )
        else:
            await self.hass.services.async_call(
                "number",
                SERVICE_SET_VALUE,
                {
                    "entity_id": calibration_entity,
                    "value": _published_offset(entity_state, calibration_offset),
                },
                blocking=True,
                context=self.context,
            )

        self.real_trvs[entity_id].last_calibration = calibration_offset
        last_hvac_mode = self.real_trvs[entity_id].last_hvac_mode
        if last_hvac_mode is not None and last_hvac_mode != "off":
            await asyncio.sleep(3)
            # The offset is on the wire whatever happens to the mode after
            # it. The mode goes out through the mode channel, which retries,
            # reports and paces a refusal like any other mode write.
            await delegate_set_hvac_mode(self, entity_id, last_hvac_mode)

        return True
    else:
        return False


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Set new target valve."""
    return  # Not supported
