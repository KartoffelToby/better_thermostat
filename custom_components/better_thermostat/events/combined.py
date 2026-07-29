"""Event triggers for combined heat/cool (auto) devices."""

from __future__ import annotations

import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.better_thermostat.utils.helpers import (
    convert_to_float_celsius,
    is_reasonable_temperature,
)

_LOGGER = logging.getLogger(__name__)


async def trigger_combined_change(self, event):
    """Handle state change events from combined heat/cool devices.

    Updates the combined device's internal state (hvac_mode, temperature,
    fan_mode, swing_mode, current_temperature) from the HA state event,
    then queues a control cycle to sync the device with BT's setpoints.
    """
    entity_id = event.data.get("entity_id")
    new_state = event.data.get("new_state")

    if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        _LOGGER.debug(
            "better_thermostat %s: combined device %s unavailable, skipping",
            self.device_name,
            entity_id,
        )
        return

    _attrs = new_state.attributes
    _trv = self.combined_trvs.get(entity_id)
    if _trv is None:
        _LOGGER.debug(
            "better_thermostat %s: combined device %s not in registry, skipping",
            self.device_name,
            entity_id,
        )
        return

    # Update hvac_mode from device state
    _trv.hvac_mode = new_state.state
    _trv.last_hvac_mode = new_state.state

    # Update target temperature (single setpoint for HEAT/COOL/AUTO)
    _raw_temp = _attrs.get("temperature")
    if _raw_temp is not None:
        _temp = convert_to_float_celsius(
            str(_raw_temp),
            self.device_name,
            "trigger_combined_change",
            unit_of_measurement=_attrs.get("unit_of_measurement"),
        )
        if _temp is not None:
            _trv.temperature = _temp
            _trv.last_temperature = _temp

    # Update current_temperature
    _raw_current = _attrs.get("current_temperature")
    if _raw_current is not None:
        _current = convert_to_float_celsius(
            str(_raw_current),
            self.device_name,
            "trigger_combined_change",
            unit_of_measurement=_attrs.get("unit_of_measurement"),
        )
        if _current is not None and is_reasonable_temperature(_current):
            _trv.current_temperature = _current

    # Update fan_mode if present
    _fan_mode = _attrs.get("fan_mode")
    if _fan_mode:
        _trv.fan_mode = _fan_mode
        self._bt_fan_mode = _fan_mode

    # Update swing_mode if present
    _swing_mode = _attrs.get("swing_mode")
    if _swing_mode:
        _trv.swing_mode = _swing_mode
        self._bt_swing_mode = _swing_mode

    # Update hvac_modes list
    _hvac_modes = _attrs.get("hvac_modes")
    if _hvac_modes:
        _trv.hvac_modes = _hvac_modes

    # Update valve_position if present
    _valve = _attrs.get("valve_position")
    if _valve is not None:
        try:
            _trv.valve_position = float(_valve)
        except ValueError, TypeError:
            pass

    _LOGGER.debug(
        "better_thermostat %s: combined device %s state updated: hvac_mode=%s, temp=%s, current_temp=%s, fan=%s, swing=%s",
        self.device_name,
        entity_id,
        _trv.hvac_mode,
        _trv.temperature,
        _trv.current_temperature,
        _trv.fan_mode,
        _trv.swing_mode,
    )

    # Queue a control cycle to sync setpoints/fan/swing to the device
    if self.bt_hvac_mode != HVACMode.OFF:
        try:
            self.control_queue_task.put_nowait(self)
        except Exception:
            pass
