"""Base adapter functions and the capability declaration shared by adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

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
                    await self.hass.services.async_call(
                        "number",
                        SERVICE_SET_VALUE,
                        {"entity_id": calibration_entity, "value": 0},
                        blocking=False,
                        context=self.context,
                    )
                except Exception as e:
                    _LOGGER.error(
                        "better_thermostat %s: Failed to set calibration to 0 for entity '%s': %s",
                        self.device_name,
                        calibration_entity,
                        e,
                    )
                return
            await asyncio.sleep(5)
            continue
        return
