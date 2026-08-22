"""Generic adapter helpers used by multiple TRV integrations.

This module implements the generic, default behaviour for TRV adapters
used by Better Thermostat when a device-specific adapter does not exist.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..utils.helpers import (
    celsius_to_system_temperature,
    find_local_calibration_entity,
    normalize_hvac_mode,
)
from .base import AdapterCapabilities, wait_for_calibration_entity_or_timeout
from .types import AdapterHost, AdapterProbeHost

_LOGGER = logging.getLogger(__name__)

# Generic HA climate entities: offset via a discovered number entity,
# no valve-position channel.
CAPABILITIES = AdapterCapabilities(offset_write=True, valve_write=False)


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
        return float(str(option).replace("k", ""))
    except ValueError, TypeError:
        return None


async def get_info(self: AdapterProbeHost, entity_id: str) -> dict[str, bool]:
    """Get info from TRV."""
    support_offset = False

    offset = await find_local_calibration_entity(self, entity_id)
    if offset is not None:
        support_offset = True
    return {"support_offset": support_offset, "support_valve": False}


async def init(self: AdapterHost, entity_id: str) -> None:
    """Initialize generic adapter for an entity.

    Finds and registers a local calibration entity (if configured) and waits
    for it to appear before returning. Returns None after initialization.
    """
    if (
        self.real_trvs[entity_id].local_temperature_calibration_entity is None
        and self.real_trvs[entity_id].calibration != 1
    ):
        self.real_trvs[
            entity_id
        ].local_temperature_calibration_entity = await find_local_calibration_entity(
            self, entity_id
        )
        _LOGGER.debug(
            "better_thermostat %s: uses local calibration entity %s",
            self.device_name,
            self.real_trvs[entity_id].local_temperature_calibration_entity,
        )
        if self.real_trvs[entity_id].local_temperature_calibration_entity is not None:
            await wait_for_calibration_entity_or_timeout(
                self,
                entity_id,
                self.real_trvs[entity_id].local_temperature_calibration_entity,
            )
        else:
            _LOGGER.warning(
                "better_thermostat %s: no local calibration entity found for '%s', skipping calibration init",
                self.device_name,
                entity_id,
            )


async def get_current_offset(self: AdapterHost, entity_id: str) -> float:
    """Get current offset."""
    if self.real_trvs[entity_id].local_temperature_calibration_entity is not None:
        state = self.hass.states.get(
            cast(str, self.real_trvs[entity_id].local_temperature_calibration_entity)
        )
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return 0.0
        try:
            # For SELECT entities, remove the 'k' suffix if present (e.g., "1.5k" -> "1.5")
            state_str = str(state.state).replace("k", "")
            return float(state_str)
        except ValueError, TypeError:
            _LOGGER.warning(
                "better_thermostat %s: Could not convert calibration offset '%s' to float, using 0",
                self.device_name,
                state.state,
            )
            return 0.0
    else:
        return 0.0


async def get_offset_step(self: AdapterHost, entity_id: str) -> float | None:
    """Get offset step."""
    if self.real_trvs[entity_id].local_temperature_calibration_entity is not None:
        state = self.hass.states.get(
            cast(str, self.real_trvs[entity_id].local_temperature_calibration_entity)
        )
        if state is None:
            return None
        return float(str(state.attributes.get("step", 1)))
    else:
        return None


async def get_min_offset(self: AdapterHost, entity_id: str) -> float:
    """Get min offset."""
    if self.real_trvs[entity_id].local_temperature_calibration_entity is not None:
        state = self.hass.states.get(
            cast(str, self.real_trvs[entity_id].local_temperature_calibration_entity)
        )
        if state is None:
            return -6.0

        # For SELECT entities, infer from options
        if state.domain == "select":
            options = state.attributes.get("options", [])
            if options:
                try:
                    # Extract numeric values from options (remove 'k' suffix)
                    values = [float(opt.replace("k", "")) for opt in options]
                    return min(values)
                except ValueError, TypeError:
                    return -6.0
            return -6.0

        # For NUMBER entities, use the min attribute
        return float(str(state.attributes.get("min", -10)))
    else:
        return -6


async def get_max_offset(self: AdapterHost, entity_id: str) -> float:
    """Get max offset."""
    if self.real_trvs[entity_id].local_temperature_calibration_entity is not None:
        state = self.hass.states.get(
            cast(str, self.real_trvs[entity_id].local_temperature_calibration_entity)
        )
        if state is None:
            return 6.0

        # For SELECT entities, infer from options
        if state.domain == "select":
            options = state.attributes.get("options", [])
            if options:
                try:
                    # Extract numeric values from options (remove 'k' suffix)
                    values = [float(opt.replace("k", "")) for opt in options]
                    return max(values)
                except ValueError, TypeError:
                    return 6.0
            return 6.0

        # For NUMBER entities, use the max attribute
        return float(str(state.attributes.get("max", 10)))
    else:
        return 6


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Set new target temperature."""
    temperature = celsius_to_system_temperature(self.hass, temperature)
    await self.hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id, "temperature": temperature},
        blocking=True,
        context=self.context,
    )


async def set_hvac_mode(self: AdapterHost, entity_id: str, hvac_mode: str) -> None:
    """Set new target hvac mode."""

    hvac_mode_norm = normalize_hvac_mode(hvac_mode)
    _LOGGER.debug(
        "better_thermostat %s: set_hvac_mode %s -> %s",
        self.device_name,
        hvac_mode,
        hvac_mode_norm,
    )
    try:
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": hvac_mode_norm},
            blocking=True,
            context=self.context,
        )
    except TypeError:
        _LOGGER.debug(
            "TypeError in set_hvac_mode (entity=%s, hvac_mode=%s)",
            entity_id,
            hvac_mode_norm,
        )
    except Exception as exc:
        _LOGGER.exception(
            "better_thermostat %s: Exception in set_hvac_mode for %s with %s: %s",
            self.device_name,
            entity_id,
            hvac_mode_norm,
            exc,
        )


async def set_offset(self: AdapterHost, entity_id: str, offset: float) -> bool:
    """Write a calibration offset to the discovered calibration entity.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to write to
    offset : float
        Calibration offset in Kelvin, clamped to the device's declared range

    Returns
    -------
    bool
        True once the write went out, False when no calibration entity was
        discovered for this TRV and there is nothing to write to.
    """
    if self.real_trvs[entity_id].local_temperature_calibration_entity is not None:
        max_calibration = await get_max_offset(self, entity_id)
        min_calibration = await get_min_offset(self, entity_id)

        offset = min(max_calibration, offset)
        offset = max(min_calibration, offset)

        calibration_entity = cast(
            str, self.real_trvs[entity_id].local_temperature_calibration_entity
        )
        entity_state = self.hass.states.get(calibration_entity)

        # Derive domain safely - from entity_state if available, otherwise from entity_id
        domain = (
            entity_state.domain if entity_state else calibration_entity.split(".", 1)[0]
        )

        # Check if it's a SELECT entity or NUMBER entity
        if domain == "select":
            # For SELECT entities, format with 'k' suffix (e.g., "1.5k")
            option_value = f"{offset:.1f}k"

            # Get available options (handle None entity_state gracefully)
            options: list[str] = []
            if entity_state:
                options = entity_state.attributes.get("options", [])

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
                            key=lambda opt: abs(parsed_options[opt] - offset),
                        )
                        option_value = closest_option

            # The option carries the value that goes on the wire, and the
            # confirmation compares the device's report against it. Both the
            # snap onto the option list and the one-decimal format move the
            # value, so the command is read back off the option itself.
            commanded = _option_to_offset(option_value)
            if commanded is not None:
                offset = commanded

            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": calibration_entity, "option": option_value},
                blocking=True,
                context=self.context,
            )
        else:
            # For NUMBER entities, use the original set_value service
            await self.hass.services.async_call(
                "number",
                SERVICE_SET_VALUE,
                {"entity_id": calibration_entity, "value": offset},
                blocking=True,
                context=self.context,
            )

        self.real_trvs[entity_id].last_calibration = offset
        if (
            self.real_trvs[entity_id].last_hvac_mode is not None
            and self.real_trvs[entity_id].last_hvac_mode != "off"
        ):
            await asyncio.sleep(3)
            await set_hvac_mode(
                self, entity_id, cast(str, self.real_trvs[entity_id].last_hvac_mode)
            )

        return True
    else:
        return False


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Set new target valve."""
    return  # Not supported
