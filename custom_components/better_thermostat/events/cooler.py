"""Cooler event handlers for the Better Thermostat integration.

Contains the event handler that reacts to changes in the configured cooler
entity and updates the integration state accordingly.
"""

from __future__ import annotations

import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State, callback

from custom_components.better_thermostat.utils.helpers import (
    COOLER_SETPOINT_KEYS,
    convert_to_float,
    normalize_step,
    read_setpoint_celsius,
    resolve_inbound_setpoint,
    resolve_state_change_event,
    state_temperature_unit,
)

_LOGGER = logging.getLogger(__name__)


def _get_cooler_step(self, state: State) -> float:
    """Return the cooler's setpoint step as a °C delta."""
    raw_step = state.attributes.get("target_temp_step")
    step = (
        convert_to_float(str(raw_step), self.device_name, "trigger_cooler_change()")
        if raw_step is not None
        else None
    )
    if (
        step is not None
        and state_temperature_unit(
            state.attributes, self.hass.config.units.temperature_unit
        )
        == UnitOfTemperature.FAHRENHEIT
    ):
        step = round(step * 5.0 / 9.0, 4)
    if step is None or step <= 0:
        return normalize_step(self.bt_target_temp_step)
    return step


@callback
async def trigger_cooler_change(self, event):
    """Trigger a change in the cooler state."""
    if self.startup_running:
        return
    if self.control_queue_task is None:
        return

    resolved_event = resolve_state_change_event(self, event, "Cooler")
    if resolved_event is None:
        return
    old_state, new_state, entity_id = resolved_event

    _LOGGER.debug(
        "better_thermostat %s: Cooler %s update received", self.device_name, entity_id
    )

    _main_change = False
    _step = _get_cooler_step(self, new_state)
    # The previous state only answers whether the cooler was publishing a
    # setpoint at all, so it is read without clamping or echo detection.
    _old_cooling_setpoint = read_setpoint_celsius(
        self, old_state, COOLER_SETPOINT_KEYS, "trigger_cooler_change()"
    )
    _new_cooling_setpoint = resolve_inbound_setpoint(
        self,
        new_state,
        keys=COOLER_SETPOINT_KEYS,
        known_values=(self.bt_target_cooltemp, self.last_sent_cooler_temp),
        step=_step,
        device_label="Cooler",
        entity_id=entity_id,
        log_source="trigger_cooler_change()",
    )
    if (
        _new_cooling_setpoint is not None
        and _old_cooling_setpoint is not None
        and self.bt_hvac_mode != HVACMode.OFF
    ):
        _LOGGER.debug(
            "better_thermostat %s: trigger_cooler_change / "
            "_old_cooling_setpoint: %s - _new_cooling_setpoint: %s - "
            "bt_target_cooltemp: %s - last_sent: %s - step: %s - echo: %s",
            self.device_name,
            _old_cooling_setpoint,
            _new_cooling_setpoint.value,
            self.bt_target_cooltemp,
            self.last_sent_cooler_temp,
            _step,
            _new_cooling_setpoint.is_echo,
        )
        if not _new_cooling_setpoint.is_echo:
            self.bt_target_cooltemp = _new_cooling_setpoint.value
            self._enforce_heat_below_cool()
            _main_change = True

    if _main_change is True:
        self.async_write_ha_state()
        return await self.control_queue_task.put(self)
    self.async_write_ha_state()
    return
