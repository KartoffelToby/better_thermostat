"""Base adapter functions and the capability declaration shared by adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
import logging

from homeassistant.components.number.const import SERVICE_SET_VALUE, NumberDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import State

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
    """

    offset_write: bool = False
    valve_write: bool = False
    # Whether the write goes through a discovered number entity (and
    # therefore requires one) instead of an ecosystem service call.
    offset_needs_entity: bool = True
    valve_needs_entity: bool = True


class OffsetScale(StrEnum):
    """How the value a calibration number publishes relates to Kelvin.

    Home Assistant converts a number of device class ``temperature`` into
    the system unit as an absolute temperature, so on a Fahrenheit system an
    offset of 0 K the device holds in Celsius is published as 32 °F, and a
    value written is converted back the same way. A number of any other
    device class is published in its native unit; when that unit is
    Fahrenheit the value is a difference, and one Kelvin is 1.8 of it.
    Everything else is taken to be in Kelvin, which is what a degree
    Celsius of difference is.
    """

    KELVIN = "kelvin"
    FAHRENHEIT_TEMPERATURE = "fahrenheit_temperature"
    FAHRENHEIT_DIFFERENCE = "fahrenheit_difference"


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
    if state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) != UnitOfTemperature.FAHRENHEIT:
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
    return calibration_offset


def published_step_to_offset(scale: OffsetScale, step: float) -> float:
    """Return the offset step in Kelvin a published ``step`` stands for.

    Home Assistant publishes the native step of a ``temperature`` number
    without converting it, so that step is already the device's own; only a
    difference published in Fahrenheit is rescaled.

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
    return step


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
        if _state is None or _state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.info(
                "better_thermostat %s: waiting for local_temperature_calibration entity with id '%s' to become fully available...",
                self.device_name,
                calibration_entity,
            )
            _retry_count += 1
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
