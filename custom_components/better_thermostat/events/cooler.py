"""Cooler event handlers for the Better Thermostat integration.

Contains the event handler that reacts to changes in the configured cooler
entity and updates the integration state accordingly.
"""

from __future__ import annotations

import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State

from custom_components.better_thermostat.utils.controlling import (
    last_sent_cooler_temperature,
)
from custom_components.better_thermostat.utils.helpers import (
    COOLER_SETPOINT_KEYS,
    convert_to_float,
    normalize_step,
    read_setpoint_celsius,
    resolve_inbound_setpoint,
    resolve_state_change_event,
    setpoint_echo_window,
    state_temperature_unit,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle

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
    # Compare only against values BT itself wrote. ``_old_cooling_setpoint`` is
    # the cooler's previously published state and is not necessarily a
    # BT-written value, so it does not belong in the echo-suppression set.
    _last_sent = last_sent_cooler_temperature(self)
    _new_cooling_setpoint = resolve_inbound_setpoint(
        self,
        new_state,
        keys=COOLER_SETPOINT_KEYS,
        known_values=(self.bt_target_cooltemp, _last_sent),
        step=_step,
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
            _last_sent,
            _step,
            _new_cooling_setpoint.is_echo,
        )
        # The cooler handler has no device-side gate of its own, so an event
        # that republishes the same setpoint — an attribute refresh, a mode
        # change, a temperature push — must not be read as user intent: a
        # stale report would otherwise revert a BT-side target that has not
        # been written yet.
        _reported_moved = abs(
            _new_cooling_setpoint.raw - _old_cooling_setpoint
        ) >= setpoint_echo_window(_step)
        if not _new_cooling_setpoint.is_echo and _reported_moved:
            if _new_cooling_setpoint.clamped:
                _LOGGER.warning(
                    "better_thermostat %s: New Cooler %s setpoint outside of range, "
                    "overwriting it",
                    self.device_name,
                    entity_id,
                )
            self.bt_target_cooltemp = _new_cooling_setpoint.value
            self._enforce_heat_below_cool()
            _main_change = True

    if _main_change is True:
        self.async_write_ha_state()
        return request_control_cycle(self)
    self.async_write_ha_state()
    return
