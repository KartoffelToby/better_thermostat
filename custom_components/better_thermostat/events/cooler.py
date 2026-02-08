"""Cooler event handlers for the Better Thermostat integration.

Contains the event handler that reacts to changes in the configured cooler
entity and updates the integration state accordingly.
"""

import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State, callback

from custom_components.better_thermostat.utils.helpers import convert_to_float

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_cooler_change(self, event):
    """Trigger a change in the cooler state."""
    if self.startup_running:
        return
    if self.control_queue_task is None:
        return
    _main_change = False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    entity_id = event.data.get("entity_id")

    if None in (new_state, old_state, new_state.attributes):
        _LOGGER.debug(
            "better_thermostat %s: Cooler %s update contained not all "
            "necessary data for processing, skipping",
            self.device_name,
            entity_id,
        )
        return

    if not isinstance(new_state, State) or not isinstance(old_state, State):
        _LOGGER.debug(
            "better_thermostat %s: Cooler %s update contained not a State, skipping",
            self.device_name,
            entity_id,
        )
        return
    # set context HACK TO FIND OUT IF AN EVENT WAS SEND BY BT

    # Check if the update is coming from the code
    if self.context == event.context:
        return

    _LOGGER.debug(
        "better_thermostat %s: Cooler %s update received",
        self.device_name,
        entity_id,
    )

    _main_key = "temperature"
    if "temperature" not in old_state.attributes:
        _main_key = "target_temp_high"

    _old_cooling_setpoint = convert_to_float(
        str(old_state.attributes.get(_main_key, None)),
        self.device_name,
        "trigger_cooler_change()",
    )
    _new_cooling_setpoint = convert_to_float(
        str(new_state.attributes.get(_main_key, None)),
        self.device_name,
        "trigger_cooler_change()",
    )
    if (
        _new_cooling_setpoint is not None
        and _old_cooling_setpoint is not None
        and self.bt_hvac_mode is not HVACMode.OFF
    ):
        _LOGGER.debug(
            "better_thermostat %s: trigger_cooler_change / "
            "_old_cooling_setpoint: %s - _new_cooling_setpoint: %s",
            self.device_name,
            _old_cooling_setpoint,
            _new_cooling_setpoint,
        )
        if (
            _new_cooling_setpoint < self.bt_min_temp
            or self.bt_max_temp < _new_cooling_setpoint
        ):
            _LOGGER.warning(
                "better_thermostat %s: New Cooler %s setpoint outside of range, "
                "overwriting it",
                self.device_name,
                entity_id,
            )

            if _new_cooling_setpoint < self.bt_min_temp:
                _new_cooling_setpoint = self.bt_min_temp
            else:
                _new_cooling_setpoint = self.bt_max_temp

        self.bt_target_cooltemp = _new_cooling_setpoint
        if self.bt_target_temp >= self.bt_target_cooltemp:
            self.bt_target_temp = self.bt_target_cooltemp - self.bt_target_temp_step
        _main_change = True

    if _main_change is True:
        self.async_write_ha_state()
        return await self.control_queue_task.put(self)
    self.async_write_ha_state()
    return
