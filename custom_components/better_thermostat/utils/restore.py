"""Pure parsing/clamping helpers for restoring state on startup.

These functions take primitive inputs (raw attribute values, TRV ``State``
objects, bounds) and return parsed/clamped values.  They are free of any
``BetterThermostat`` entity dependency so they can be unit-tested in isolation;
``climate.py`` keeps the thin orchestration that reads ``async_get_last_state``
and assigns the results to entity attributes.
"""

from __future__ import annotations

import logging
from statistics import mean

from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import State

from .const import MAX_HEAT_LOSS, MAX_HEATING_POWER, MIN_HEAT_LOSS, MIN_HEATING_POWER
from .helpers import convert_to_float_celsius, state_temperature_unit
from .thermal_learning import clamp

_LOGGER = logging.getLogger(__name__)


def mean_trv_target(
    states: list[State],
    device_name: str,
    context: str = "restore(target)",
    system_unit: str | None = None,
) -> float | None:
    """Mean of the valid TRV target temperatures, each converted to Celsius.

    Returns ``None`` when no TRV exposes a usable target temperature.
    """
    temps: list[float] = []
    for state in states:
        raw = state.attributes.get(ATTR_TEMPERATURE)
        if raw is None:
            continue
        unit = state_temperature_unit(state.attributes, system_unit)
        celsius = convert_to_float_celsius(
            str(raw), device_name, context, unit_of_measurement=unit
        )
        if celsius is not None:
            temps.append(celsius)
    return mean(temps) if temps else None


def restore_target_temperature(
    saved: str | int | float | None,
    states: list[State],
    min_temp: float | None,
    max_temp: float | None,
    device_name: str,
    system_unit: str | None = None,
) -> float | None:
    """Resolve the restored heating target.

    With a usable numeric *saved* value, clamp it into ``[min_temp, max_temp]``
    (defaults 5.0 / 30.0).  When *saved* is missing or non-numeric, fall back to
    the mean of the TRV targets.  Returns ``None`` only when neither source
    yields a value.
    """
    if saved is None:
        return mean_trv_target(states, device_name, system_unit=system_unit)

    value = convert_to_float_celsius(
        saved, device_name, "restore_target_temperature", system_unit
    )
    if value is None:
        _LOGGER.warning(
            "better_thermostat %s: Saved target temperature %r is not numeric, "
            "falling back to the TRV mean",
            device_name,
            saved,
        )
        return mean_trv_target(states, device_name, system_unit=system_unit)

    low = min_temp if min_temp is not None else 5.0
    high = max_temp if max_temp is not None else 30.0
    if value < low:
        _LOGGER.warning(
            "better_thermostat %s: Saved target temperature %s is lower than "
            "min_temp %s, setting to min_temp",
            device_name,
            value,
            low,
        )
        return low
    if value > high:
        _LOGGER.warning(
            "better_thermostat %s: Saved target temperature %s is higher than "
            "max_temp %s, setting to max_temp",
            device_name,
            value,
            high,
        )
        return high
    return value


def clamp_heating_power(raw: str | int | float | None, device_name: str) -> float:
    """Parse and clamp a restored heating-power value to its valid range.

    A missing or non-numeric value falls back to ``0.01`` before clamping.
    """
    try:
        value = 0.01 if raw is None else float(raw)
    except (TypeError, ValueError):
        value = 0.01
    bounded = clamp(value, MIN_HEATING_POWER, MAX_HEATING_POWER)
    if bounded != value:
        _LOGGER.info(
            "better_thermostat %s: Restored heating_power %.3f is outside allowed "
            "range [%s, %s]; clamped to %.3f",
            device_name,
            value,
            MIN_HEATING_POWER,
            MAX_HEATING_POWER,
            bounded,
        )
    return bounded


def clamp_heat_loss(raw: str | int | float | None) -> float | None:
    """Parse and clamp a restored heat-loss value, or ``None`` if not numeric."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return clamp(value, MIN_HEAT_LOSS, MAX_HEAT_LOSS)
