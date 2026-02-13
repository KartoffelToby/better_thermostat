"""Base adapter functions shared across multiple adapters."""

import asyncio
import logging

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

_LOGGER = logging.getLogger(__name__)


async def wait_for_calibration_entity_or_timeout(self, entity_id, calibration_entity):
    """Wait for calibration entity to become available with timeout.

    If the entity is not available after timeout, force set calibration to 0.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        The TRV entity ID
    calibration_entity : str
        The local temperature calibration entity ID

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

    # Wait for the entity to be available with timeout
    _ready = True
    _max_retries = 6  # 30 seconds total (6 * 5 seconds)
    _retry_count = 0
    while _ready:
        _state = self.hass.states.get(calibration_entity)
        if _state is None or _state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.info(
                "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
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
                _ready = False
                return
            await asyncio.sleep(5)
            continue
        _ready = False
        return
