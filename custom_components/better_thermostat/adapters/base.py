"""Base adapter functions and the capability declaration shared by adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import logging
import math

from homeassistant.components.number.const import SERVICE_SET_VALUE, NumberDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    UnitOfTemperature,
)
from homeassistant.core import State

from ..utils.helpers import celsius_unit_divisor, device_released, state_is_live
from .types import AdapterHost

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdapterCapabilities:
    """What one ecosystem's adapter can do, declared per adapter module.

    Each adapter exports a ``CAPABILITIES`` constant. The effective
    per-TRV descriptor (:meth:`Trv.capabilities`) intersects this
    declaration with the discovered entity surface: an ecosystem that
    writes through a number entity only has the capability once that
    entity was discovered, while a service-call ecosystem (deCONZ,
    Tado) carries it unconditionally.

    Attributes
    ----------
    offset_write : bool
        Whether the adapter can write a local temperature offset.
    valve_write : bool
        Whether the adapter can write a valve position.
    offset_needs_entity : bool
        Whether the offset write requires a discovered number entity
        rather than an ecosystem service call.
    valve_needs_entity : bool
        Whether the valve write requires a discovered number entity
        rather than an ecosystem service call.
    valve_only : bool
        Whether Direct Valve Based uses the valve as its only actuator,
        leaving the device's thermostat settings and reports independent.
    """

    offset_write: bool = False
    valve_write: bool = False
    # Whether the write goes through a discovered number entity (and
    # therefore requires one) instead of an ecosystem service call.
    offset_needs_entity: bool = True
    valve_needs_entity: bool = True
    valve_only: bool = False


@dataclass(frozen=True)
class DeviceChannels:
    """Which calibration channels one TRV device offers.

    An adapter answers this from what Home Assistant already knows about
    the device, before any TRV record exists, so the config flow can offer
    only the calibration outputs the device can take.

    Attributes
    ----------
    offset_write : bool
        Whether the device takes a local temperature offset.
    valve_write : bool
        Whether the device takes a valve position.
    """

    offset_write: bool = False
    valve_write: bool = False


class OffsetScale(StrEnum):
    """How the value a calibration number publishes relates to Kelvin.

    Home Assistant converts a number of device class ``temperature`` into
    the system unit as an absolute temperature, so on a Fahrenheit system an
    offset of 0 K the device holds in Celsius is published as 32 °F, and a
    value written is converted back the same way. A number of any other
    device class is published in its native unit; when that unit is
    Fahrenheit the value is a difference, and one Kelvin is 1.8 of it. A
    unit counting tenths or hundredths of a degree Celsius, which a Z-Wave
    configuration parameter publishes as its raw value, is not converted
    either: one Kelvin is 10 or 100 of it. Everything else is taken to be in
    Kelvin, which is what a degree Celsius of difference is.
    """

    KELVIN = "kelvin"
    FAHRENHEIT_TEMPERATURE = "fahrenheit_temperature"
    FAHRENHEIT_DIFFERENCE = "fahrenheit_difference"
    CELSIUS_TENTHS = "celsius_tenths"
    CELSIUS_HUNDREDTHS = "celsius_hundredths"


# Counts of the published unit that make one Kelvin, for the scales whose
# published value is a whole number of fractions of a degree Celsius.
_COUNTS_PER_KELVIN: dict[OffsetScale, int] = {
    OffsetScale.CELSIUS_TENTHS: 10,
    OffsetScale.CELSIUS_HUNDREDTHS: 100,
}

# Attribute in which a Z-Wave JS configuration parameter lists the values
# that carry a meaning of their own instead of a number, such as the
# Eurotronic Spirit's -128 for "external sensor".
ATTR_RESERVED_VALUES = "reserved_values"


def offset_scale(state: State | None) -> OffsetScale:
    """Read how a calibration entity publishes its offset.

    Parameters
    ----------
    state : State or None
        State of the calibration entity. Home Assistant publishes the unit
        and the device class of an unavailable entity as well, so only an
        entity that has no state at all is read as Kelvin by default.

    Returns
    -------
    OffsetScale
        The relation between the published value and an offset in Kelvin.
    """
    if state is None or state.domain != "number":
        return OffsetScale.KELVIN
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    divisor = celsius_unit_divisor(unit)
    if divisor == 10:
        return OffsetScale.CELSIUS_TENTHS
    if divisor == 100:
        return OffsetScale.CELSIUS_HUNDREDTHS
    if unit != UnitOfTemperature.FAHRENHEIT:
        return OffsetScale.KELVIN
    if state.attributes.get(ATTR_DEVICE_CLASS) == NumberDeviceClass.TEMPERATURE:
        return OffsetScale.FAHRENHEIT_TEMPERATURE
    return OffsetScale.FAHRENHEIT_DIFFERENCE


def published_to_offset(scale: OffsetScale, value: float) -> float:
    """Return the offset in Kelvin a published calibration value stands for.

    Parameters
    ----------
    scale : OffsetScale
        How the calibration entity publishes its offset.
    value : float
        Value, ``min`` or ``max`` as the entity publishes it.

    Returns
    -------
    float
        The same offset in Kelvin, at full precision.
    """
    if scale is OffsetScale.FAHRENHEIT_TEMPERATURE:
        return (value - 32.0) * 5.0 / 9.0
    if scale is OffsetScale.FAHRENHEIT_DIFFERENCE:
        return value * 5.0 / 9.0
    if scale in _COUNTS_PER_KELVIN:
        return value / _COUNTS_PER_KELVIN[scale]
    return value


def offset_to_published(scale: OffsetScale, calibration_offset: float) -> float:
    """Return the value a calibration entity takes for an offset in Kelvin.

    Parameters
    ----------
    scale : OffsetScale
        How the calibration entity publishes its offset.
    calibration_offset : float
        Offset in Kelvin.

    Returns
    -------
    float
        The value to write, in the unit the entity publishes.
    """
    if scale is OffsetScale.FAHRENHEIT_TEMPERATURE:
        return calibration_offset * 9.0 / 5.0 + 32.0
    if scale is OffsetScale.FAHRENHEIT_DIFFERENCE:
        return calibration_offset * 9.0 / 5.0
    if scale in _COUNTS_PER_KELVIN:
        return calibration_offset * _COUNTS_PER_KELVIN[scale]
    return calibration_offset


def published_step_to_offset(scale: OffsetScale, step: float) -> float:
    """Return the offset step in Kelvin a published ``step`` stands for.

    Home Assistant publishes the native step of a ``temperature`` number
    without converting it, so that step is already the device's own; a
    difference published in Fahrenheit or in fractions of a degree Celsius
    is rescaled.

    Parameters
    ----------
    scale : OffsetScale
        How the calibration entity publishes its offset.
    step : float
        The ``step`` the entity publishes.

    Returns
    -------
    float
        The step in Kelvin.
    """
    if scale is OffsetScale.FAHRENHEIT_DIFFERENCE:
        return step * 5.0 / 9.0
    if scale in _COUNTS_PER_KELVIN:
        return step / _COUNTS_PER_KELVIN[scale]
    return step


def counts_whole_fractions(scale: OffsetScale) -> bool:
    """Whether a calibration entity of ``scale`` takes whole counts of its unit.

    Parameters
    ----------
    scale : OffsetScale
        How the calibration entity publishes its offset.

    Returns
    -------
    bool
        True for a unit of tenths or hundredths of a degree Celsius, whose
        values are a parameter's raw integers.
    """
    return scale in _COUNTS_PER_KELVIN


def reserved_published_values(state: State) -> frozenset[float]:
    """Read the values a calibration entity reserves for a meaning of its own.

    Home Assistant publishes the keys of ``reserved_values`` as integers;
    a state that went through JSON, as the frontend and a restored state
    see it, carries them as text. Both read the same.

    Parameters
    ----------
    state : State
        State of the calibration entity.

    Returns
    -------
    frozenset of float
        The reserved values as published, empty when the entity lists none.
    """
    reserved = state.attributes.get(ATTR_RESERVED_VALUES)
    if not isinstance(reserved, Mapping):
        return frozenset()
    values: set[float] = set()
    for key in reserved:
        try:
            values.add(float(str(key)))
        except ValueError:
            continue
    return frozenset(values)


def published_reading(state: State) -> float | None:
    """Return the offset in Kelvin a calibration entity reports, if it reports one.

    Parameters
    ----------
    state : State
        Live state of the calibration entity.

    Returns
    -------
    float or None
        The offset in Kelvin, or None when the state carries a value the
        entity reserves for a meaning of its own, which is no offset.

    Raises
    ------
    ValueError
        When the state carries no number.
    """
    # A select publishes its options with a "k" suffix ("1.5k").
    value = float(state.state.replace("k", ""))
    if value in reserved_published_values(state):
        return None
    return published_to_offset(offset_scale(state), value)


def published_step(state: State) -> float:
    """Return the step a calibration number publishes, in its own unit.

    Parameters
    ----------
    state : State
        State of the calibration number.

    Returns
    -------
    float
        The positive step the entity publishes, or 1.0 when it publishes
        none that is a usable step, which is the step Home Assistant gives a
        number over a range wider than one.
    """
    try:
        step = abs(float(str(state.attributes.get("step", 1.0))))
    except ValueError:
        return 1.0
    if step == 0.0 or not math.isfinite(step):
        return 1.0
    return step


def published_bound(state: State, bound: str) -> float | None:
    """Return the ``min`` or ``max`` a calibration number takes a write at.

    A bound the entity reserves for a meaning of its own is no offset, so
    the bound moves inward by the entity's step until it reaches a value
    that is one.

    Parameters
    ----------
    state : State
        State of the calibration number.
    bound : str
        ``"min"`` or ``"max"``.

    Returns
    -------
    float or None
        The bound in the entity's published unit, or None when it publishes
        none that can be read.
    """
    try:
        value = float(str(state.attributes[bound]))
    except KeyError, TypeError, ValueError:
        return None
    reserved = reserved_published_values(state)
    if not reserved:
        return value
    step = published_step(state)
    inward = step if bound == "min" else -step
    for _ in range(len(reserved)):
        if value not in reserved:
            break
        value += inward
    return value


def _zero_offset_option(state: State | None) -> str:
    """Return the option of a calibration select that carries a zero offset.

    Parameters
    ----------
    state : State or None
        State of the calibration select, or None when it has none yet.

    Returns
    -------
    str
        The offered option that reads as zero Kelvin, or the Kelvin spelling
        of zero when the entity offers nothing that does.
    """
    if state is None:
        return "0.0k"
    for option in state.attributes.get("options") or []:
        try:
            if float(str(option).replace("k", "")) == 0.0:
                return str(option)
        except ValueError, TypeError:
            continue
    return "0.0k"


async def _write_zero_calibration(
    self: AdapterHost, calibration_entity: str, state: State | None
) -> None:
    """Write a zero offset through the service the entity's domain answers to.

    Discovery accepts a calibration helper in either the ``number`` or the
    ``select`` domain, and each takes its own service: a select rejects
    ``number.set_value`` and only moves when told an option it offers.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    calibration_entity : str
        Entity ID of the calibration helper to write to.
    state : State or None
        State of that entity, used to pick an option it actually offers.

    Returns
    -------
    None
    """
    if calibration_entity.split(".", 1)[0] == "select":
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": calibration_entity, "option": _zero_offset_option(state)},
            blocking=True,
            context=self.context,
        )
        return
    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {
            "entity_id": calibration_entity,
            "value": offset_to_published(offset_scale(state), 0.0),
        },
        blocking=True,
        context=self.context,
    )


async def wait_for_calibration_entity_or_timeout(
    self: AdapterHost, entity_id: str, calibration_entity: str | None
) -> None:
    """Wait for calibration entity to become available with timeout.

    If the entity is not available after timeout, force set calibration to 0.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        The TRV entity ID
    calibration_entity : str or None
        The local temperature calibration entity ID, or None when the TRV
        has no calibration entity to wait for

    Returns
    -------
    None
    """
    if calibration_entity is None:
        _LOGGER.warning(
            "better_thermostat %s: calibration_entity is None for '%s', skipping wait",
            self.device_name,
            entity_id,
        )
        return

    # Six passes with a five-second sleep between them. The last pass forces
    # the write instead of sleeping again, so the wait spans 25 seconds.
    _max_retries = 6
    _retry_count = 0
    while True:
        _state = self.hass.states.get(calibration_entity)
        if not state_is_live(_state):
            _LOGGER.info(
                "better_thermostat %s: waiting for local_temperature_calibration entity with id '%s' to become fully available...",
                self.device_name,
                calibration_entity,
            )
            _retry_count += 1
            # The device can be released while the wait goes on, and the
            # write that ends it is not sent to it then.
            if device_released(self, entity_id):
                return
            if _retry_count >= _max_retries:
                _LOGGER.warning(
                    "better_thermostat %s: local_temperature_calibration entity '%s' not available after timeout, forcing calibration to 0",
                    self.device_name,
                    calibration_entity,
                )
                # Force set calibration to 0 to initialize the entity
                try:
                    await _write_zero_calibration(self, calibration_entity, _state)
                except Exception as e:
                    _LOGGER.error(
                        "better_thermostat %s: Failed to set calibration to 0 for entity '%s': %s",
                        self.device_name,
                        calibration_entity,
                        e,
                        exc_info=True,
                    )
                return
            await asyncio.sleep(5)
            continue
        return
