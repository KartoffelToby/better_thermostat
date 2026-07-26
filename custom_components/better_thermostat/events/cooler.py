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
    attr_to_celsius,
    convert_to_float,
    state_temperature_unit,
)

_LOGGER = logging.getLogger(__name__)


def _get_cooling_setpoint(self, state: State) -> float | None:
    """Read the cooler's setpoint from a state and return it in °C.

    A climate entity that supports both a single target and a target range
    publishes ``temperature`` as None while it runs in range mode, so a
    present-but-empty attribute must not stop the range key from being read.
    """
    for key in ("temperature", "target_temp_high"):
        if state.attributes.get(key) is None:
            continue
        setpoint = attr_to_celsius(self, state, key, None, "trigger_cooler_change()")
        if setpoint is not None:
            return setpoint
    return None


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
        fallback = self.bt_target_temp_step
        step = fallback if isinstance(fallback, (int, float)) and fallback > 0 else 0.5
    return float(step)


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

    if new_state is None or old_state is None:
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

    if new_state.attributes is None:
        _LOGGER.debug(
            "better_thermostat %s: Cooler %s update had no attributes, skipping",
            self.device_name,
            entity_id,
        )
        return

    # Skip updates that BT itself triggered: our own service calls carry
    # self.context, so a matching context means this is an echo of our write.
    if self.context == event.context:
        return

    _LOGGER.debug(
        "better_thermostat %s: Cooler %s update received", self.device_name, entity_id
    )

    _old_cooling_setpoint = _get_cooling_setpoint(self, old_state)
    _new_cooling_setpoint = _get_cooling_setpoint(self, new_state)
    if (
        _new_cooling_setpoint is not None
        and _old_cooling_setpoint is not None
        and self.bt_hvac_mode != HVACMode.OFF
    ):
        _step = _get_cooler_step(self, new_state)
        # Adopt only what a user set on the cooler itself. A value within one
        # step of a setpoint BT wrote is the device reporting our own write
        # back: rounded to its own grid, or delayed until a poll that carries a
        # foreign context and therefore passes the context check above. User
        # input moves the setpoint by at least one step.
        _bt_known_values = (self.bt_target_cooltemp, self.last_sent_cooler_temp)
        _is_echo = any(
            isinstance(value, (int, float))
            and abs(_new_cooling_setpoint - value) < _step
            for value in _bt_known_values
        )
        _LOGGER.debug(
            "better_thermostat %s: trigger_cooler_change / "
            "_old_cooling_setpoint: %s - _new_cooling_setpoint: %s - "
            "bt_target_cooltemp: %s - last_sent: %s - step: %s - echo: %s",
            self.device_name,
            _old_cooling_setpoint,
            _new_cooling_setpoint,
            self.bt_target_cooltemp,
            self.last_sent_cooler_temp,
            _step,
            _is_echo,
        )
        if not _is_echo:
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
            self._enforce_heat_below_cool()
            _main_change = True

    if _main_change is True:
        self.async_write_ha_state()
        return await self.control_queue_task.put(self)
    self.async_write_ha_state()
    return
