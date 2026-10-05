"""Shelly adapter for Better Thermostat Direct Valve control.

Adds Direct Valve support for Shelly TRVs that expose a writable native
Home Assistant valve-position Number entity, such as Shelly BLU TRV.

The adapter intentionally keeps device-specific responsibilities small:
* discover the native valve Number using Better Thermostat's shared helper;
* translate Better Thermostat's 0..100 % target to the Number min/max/step;
* avoid duplicate writes when the native Number already confirms the same
  successfully-sent command;
* suppress native climate setpoint writes only when Direct Valve is selected
  and Shelly exposes no native target temperature (``temperature is None``).

It does not implement a second controller, HVAC state machine, call-for-heat
policy, retry worker, or boiler logic. Those remain Better Thermostat/core
responsibilities.
"""

from __future__ import annotations

import logging

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..utils.const import CalibrationType
from ..utils.helpers import find_valve_entity
from .generic import (
    get_current_offset,
    get_info as generic_get_info,
    get_max_offset,
    get_min_offset,
    get_offset_step,
    init as generic_init,
    set_hvac_mode,
    set_offset,
    set_temperature as generic_set_temperature,
)

__all__ = (
    "get_current_offset",
    "get_info",
    "get_max_offset",
    "get_min_offset",
    "get_offset_step",
    "init",
    "set_hvac_mode",
    "set_offset",
    "set_temperature",
    "set_valve",
)

_LOGGER = logging.getLogger(__name__)


def _is_writable_number(valve: dict | None) -> bool:
    """Return whether a discovery result is a writable native Number."""
    return bool(
        valve
        and valve.get("entity_id")
        and valve.get("writable", False)
        and valve.get("domain") == "number"
    )


def _is_direct_valve(trv) -> bool:
    """Return whether this TRV is configured for Direct Valve control."""
    if trv is None:
        return False
    advanced = getattr(trv, "advanced", None)
    return isinstance(advanced, dict) and (
        advanced.get("calibration") == CalibrationType.DIRECT_VALVE_BASED
    )


def _scale_valve_target(
    target_pct: float,
    min_value: float,
    max_value: float,
    step: float,
) -> float:
    """Scale a 0..100 % target onto a Number min/max/step grid.

    Home Assistant Number steps are anchored at the entity minimum, not zero.
    """
    if max_value < min_value:
        raise ValueError("number max is below min")

    pct = max(0.0, min(100.0, float(target_pct)))
    value = min_value + (pct / 100.0) * (max_value - min_value)

    if step > 0:
        value = min_value + round((value - min_value) / step) * step

    return max(min_value, min(max_value, value))


def _same_command(a: float | None, b: float, step: float) -> bool:
    """Compare native Number commands using half a declared step tolerance."""
    if a is None:
        return False
    tolerance = max(abs(step) / 2.0, 1e-6) if step > 0 else 1e-6
    return abs(float(a) - float(b)) <= tolerance


async def _discover_valve(
    self,
    entity_id: str,
    *,
    clear_on_miss: bool = True,
) -> str | None:
    """Discover and cache this Shelly TRV's writable native valve Number.

    ``clear_on_miss`` is used only for initialization.  During runtime a
    failed re-discovery must not clear a previously valid cached entity: the
    Better Thermostat delegate calls ``adapter.set_valve`` only while that
    cache says a writable valve exists. Clearing it on a transient registry
    miss would therefore prevent all later retries until a BT reload.
    """
    trv = self.real_trvs.get(entity_id)
    if trv is None:
        return None

    valve = await find_valve_entity(self, entity_id)
    if _is_writable_number(valve):
        trv.valve_position_entity = valve["entity_id"]
        trv.valve_position_writable = True
        _LOGGER.debug(
            "better_thermostat %s: Shelly TRV %s uses valve entity %s "
            "(reason=%s)",
            getattr(self, "device_name", "unknown"),
            entity_id,
            trv.valve_position_entity,
            valve.get("reason"),
        )
        return trv.valve_position_entity

    if clear_on_miss:
        trv.valve_position_entity = None
        trv.valve_position_writable = False
    return None


async def get_info(self, entity_id):
    """Return generic capabilities plus native Shelly valve support."""
    info = await generic_get_info(self, entity_id)
    valve = await find_valve_entity(self, entity_id)
    info["support_valve"] = _is_writable_number(valve)
    return info


async def init(self, entity_id):
    """Discover the Shelly valve Number, then initialize generic features."""
    trv = self.real_trvs.get(entity_id)
    if trv is None:
        raise RuntimeError(f"Shelly TRV runtime object missing: {entity_id}")

    try:
        await _discover_valve(self, entity_id)
    except Exception:
        # A Shelly climate without a valve Number must still retain generic
        # thermostat behaviour. Discovery failure therefore does not make the
        # whole adapter unusable; Direct Valve simply remains unavailable.
        trv.valve_position_entity = None
        trv.valve_position_writable = False
        _LOGGER.warning(
            "better_thermostat %s: Shelly valve discovery failed for %s; "
            "continuing with generic thermostat behaviour",
            getattr(self, "device_name", "unknown"),
            entity_id,
            exc_info=True,
        )

    await generic_init(self, entity_id)


async def set_temperature(self, entity_id, temperature):
    """Write a native target only when Shelly exposes a usable setpoint.

    In Shelly BLU TRV Direct Valve mode the climate entity reports
    ``temperature=None``. Sending ``climate.set_temperature`` in that state is
    not part of Direct Valve control and needlessly exercises the slow
    Gateway/BLE setpoint path. Other Shelly TRVs/modes keep generic behaviour.
    """
    trv = self.real_trvs.get(entity_id)
    state = self.hass.states.get(entity_id)

    if (
        _is_direct_valve(trv)
        and trv is not None
        and trv.valve_position_writable is True
        and state is not None
        and state.attributes.get("temperature") is None
    ):
        _LOGGER.debug(
            "better_thermostat %s: Shelly TRV %s is Direct Valve with no "
            "native target temperature; skip set_temperature(%s)",
            getattr(self, "device_name", "unknown"),
            entity_id,
            temperature,
        )
        return None

    return await generic_set_temperature(self, entity_id, temperature)


async def set_valve(self, entity_id, valve):
    """Set Better Thermostat's 0..100 % target on the Shelly valve Number.

    A write is skipped only when the native Number already confirms the same
    command that this adapter most recently sent successfully (or no command
    has been sent since adapter startup). If the desired target reverses while
    an older command's readback is still visible, the differing last command
    prevents stale readback from suppressing the reversal.

    Missing/unavailable valve entities raise so Better Thermostat's delegate
    reports the write as failed instead of recording a false successful target.
    """
    trv = self.real_trvs.get(entity_id)
    if trv is None:
        raise RuntimeError(f"Shelly TRV runtime object missing: {entity_id}")

    valve_entity_id = (
        trv.valve_position_entity
        if trv.valve_position_writable is True
        else None
    )

    # If this function is already being called, a missing cache can be
    # re-populated. Note that Better Thermostat's delegate itself gates calls
    # on an existing writable valve cache, so changing a Shelly TRV from
    # thermostat mode (sensor-only valve position) to Direct Valve mode still
    # requires a Better Thermostat reload to perform initial discovery.
    if not valve_entity_id:
        valve_entity_id = await _discover_valve(
            self, entity_id, clear_on_miss=False
        )

    if not valve_entity_id or trv.valve_position_writable is not True:
        raise RuntimeError(f"No writable Shelly valve Number for {entity_id}")

    valve_state = self.hass.states.get(valve_entity_id)
    if valve_state is None:
        # One re-discovery attempt covers registry/entity-id changes.
        rediscovered = await _discover_valve(
            self, entity_id, clear_on_miss=False
        )
        if rediscovered and rediscovered != valve_entity_id:
            valve_entity_id = rediscovered
            valve_state = self.hass.states.get(valve_entity_id)

    if (
        valve_state is None
        or valve_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, "")
    ):
        raise RuntimeError(
            f"Shelly valve entity unavailable: {valve_entity_id}"
        )

    try:
        min_value = float(str(valve_state.attributes.get("min", 0)))
        max_value = float(str(valve_state.attributes.get("max", 100)))
        step = float(str(valve_state.attributes.get("step", 1)))
        target_pct = max(0.0, min(100.0, float(valve)))
        command_value = _scale_valve_target(
            target_pct, min_value, max_value, step
        )
        current_value = float(str(valve_state.state))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid Shelly valve state/bounds for {valve_entity_id}"
        ) from exc

    # ``delegate.set_valve`` owns the successful-command bookkeeping.  It
    # updates ``last_valve_percent`` only after this adapter returns without an
    # exception, so a failed write cannot poison the de-duplication reference.
    last_target_pct = trv.last_valve_percent
    try:
        last_target_pct = (
            float(last_target_pct) if last_target_pct is not None else None
        )
    except (TypeError, ValueError):
        last_target_pct = None

    current_matches = _same_command(current_value, command_value, step)
    last_matches = (
        last_target_pct is None
        or abs(last_target_pct - target_pct) <= 1e-6
    )

    if current_matches and last_matches:
        _LOGGER.debug(
            "better_thermostat %s: Shelly TRV %s valve already confirms %s "
            "(%s); skip duplicate write",
            getattr(self, "device_name", "unknown"),
            entity_id,
            command_value,
            valve_entity_id,
        )
        return None

    _LOGGER.debug(
        "better_thermostat %s: TO Shelly TRV %s set_valve %.1f%% -> %s "
        "via %s (readback=%s, last_target=%s)",
        getattr(self, "device_name", "unknown"),
        entity_id,
        target_pct,
        command_value,
        valve_entity_id,
        current_value,
        last_target_pct,
    )

    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": valve_entity_id, "value": command_value},
        blocking=True,
        context=self.context,
    )

    # Successful-command bookkeeping is intentionally left to delegate.set_valve.
    return None
