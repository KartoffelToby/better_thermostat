"""TRV event handlers and helpers for better_thermostat.

This module contains the various Home Assistant TRV event handlers and
helper functions used by the Better Thermostat integration to read and
convert thermostat states and prepare outbound payloads.
"""

from __future__ import annotations

import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State, callback
from homeassistant.util import dt as dt_util

from custom_components.better_thermostat.adapters.delegate import get_current_offset
from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.model_fixes.model_quirks import (
    load_model_quirks,
)
from custom_components.better_thermostat.utils.const import (
    CONF_HOMEMATICIP,
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.helpers import (
    attr_to_celsius,
    convert_to_float,
    get_device_model,
    group_all_members_off,
    is_reasonable_temperature,
    mode_remap,
)

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_trv_change(self, event):
    """Trigger a change in the trv state."""
    if self.startup_running:
        return
    if self.control_queue_task is None:
        return
    if self.bt_target_temp is None or self.cur_temp is None or self.tolerance is None:
        return
    if self.bt_update_lock:
        return
    _main_change = False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    entity_id = event.data.get("entity_id")

    if new_state is None or old_state is None or new_state.attributes is None:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s update contained not all necessary data for processing, skipping",
            self.device_name,
            entity_id,
        )
        return

    if not isinstance(new_state, State) or not isinstance(old_state, State):
        _LOGGER.debug(
            "better_thermostat %s: TRV %s update contained not a State, skipping",
            self.device_name,
            entity_id,
        )
        return

    # Check if the update is coming from the code
    if self.context == event.context:
        return

    # _LOGGER.debug(f"better_thermostat {self.device_name}: TRV {entity_id} update received")

    _org_trv_state = self.hass.states.get(entity_id)
    if _org_trv_state is None:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s state not found in registry, skipping",
            self.device_name,
            entity_id,
        )
        return

    trv = self.real_trvs.get(entity_id)
    if trv is None:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s is not tracked in real_trvs, skipping",
            self.device_name,
            entity_id,
        )
        return

    if _org_trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        # The device is gone; its last internal temperature must not
        # keep feeding the calibration as if it were live.
        if trv.current_temperature is not None:
            _LOGGER.debug(
                "better_thermostat %s: TRV %s became %s; invalidating its "
                "internal temperature",
                self.device_name,
                entity_id,
                _org_trv_state.state,
            )
            trv.current_temperature = None
            # The next valid reading is the first live data after the
            # outage and must not be dropped by the debounce below.
            trv.accept_next_internal_temp = True
        return

    child_lock = (trv.advanced or {}).get("child_lock")

    # Dynamic model detection: only once (e.g. at startup), not on every event
    try:
        prev_model = trv.model
        if not prev_model:
            if _org_trv_state is not None and isinstance(
                _org_trv_state.attributes, dict
            ):
                # Only check when there are hints available
                if (
                    "model_id" in _org_trv_state.attributes
                    or "device" in _org_trv_state.attributes
                ):
                    detected = await get_device_model(self, entity_id)
                    if isinstance(detected, str) and detected:
                        _LOGGER.info(
                            "better_thermostat %s: TRV %s model detected: %s; "
                            "loading quirks",
                            self.device_name,
                            entity_id,
                            detected,
                        )
                        quirks = await load_model_quirks(self, detected, entity_id)
                        trv.model = detected
                        trv.model_quirks = quirks
    except Exception as e:
        _LOGGER.debug(
            "better_thermostat %s: dynamic model detection failed for %s: %s",
            self.device_name,
            entity_id,
            e,
        )

    _new_current_temp = attr_to_celsius(
        self, _org_trv_state, "current_temperature", None, "TRV_current_temp"
    )
    if _new_current_temp is not None and not is_reasonable_temperature(
        _new_current_temp
    ):
        _LOGGER.warning(
            "better_thermostat %s: TRV %s reports implausible current_temperature "
            "%s; ignoring",
            self.device_name,
            entity_id,
            _new_current_temp,
        )
        _new_current_temp = None

    _time_diff = 5
    try:
        for trv_conf in self.all_trvs:
            if trv_conf["advanced"][CONF_HOMEMATICIP]:
                _time_diff = 600
    except KeyError:
        pass
    if (
        _new_current_temp is not None
        and trv.current_temperature != _new_current_temp
        and (
            trv.consume_accept_next_internal_temp()
            or (dt_util.now() - self.last_internal_sensor_change).total_seconds()
            > _time_diff
            or (trv.calibration_received is False and trv.calibration != 1)
        )
    ):
        _old_temp = trv.current_temperature
        trv.current_temperature = _new_current_temp
        _LOGGER.debug(
            "better_thermostat %s: TRV %s sends new internal temperature from %s to %s",
            self.device_name,
            entity_id,
            _old_temp,
            _new_current_temp,
        )
        self.last_internal_sensor_change = dt_util.now()
        _main_change = True

        # async def in controlling? (left as note)
        if trv.calibration_received is False:
            trv.calibration_received = True
            _LOGGER.debug(
                "better_thermostat %s: calibration accepted by TRV %s",
                self.device_name,
                entity_id,
            )
            _main_change = False
            if trv.calibration == 0:
                trv.last_calibration = await get_current_offset(self, entity_id)

    if self.ignore_states:
        return

    try:
        mapped_state = convert_inbound_states(self, entity_id, _org_trv_state)
    except TypeError:
        _LOGGER.debug(
            "better_thermostat %s: remapping TRV %s state failed, skipping",
            self.device_name,
            entity_id,
        )
        return

    # Always cache hvac_action from the TRV state so it stays current
    try:
        hvac_action_attr = _org_trv_state.attributes.get("hvac_action")
        if hvac_action_attr is None:
            hvac_action_attr = _org_trv_state.attributes.get("action")
        if hvac_action_attr is not None:
            val = str(hvac_action_attr).strip().lower()
            prev = trv.hvac_action
            trv.hvac_action = val
            if prev != val:
                _main_change = True
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s hvac_action changed: %s -> %s",
                    self.device_name,
                    entity_id,
                    prev,
                    val,
                )

        # valve_position aktualisieren
        val_pos = _org_trv_state.attributes.get("valve_position")
        if val_pos is not None:
            trv.valve_position = convert_to_float(
                str(val_pos), self.device_name, "trv_event"
            )

    except Exception:
        pass

    if mapped_state in (HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL):
        if trv.hvac_mode != _org_trv_state.state and not child_lock:
            _old = trv.hvac_mode
            _LOGGER.debug(
                "better_thermostat %s: TRV %s decoded TRV mode changed from %s to %s - converted %s",
                self.device_name,
                entity_id,
                _old,
                _org_trv_state.state,
                new_state.state,
            )
            trv.hvac_mode = _org_trv_state.state
            _main_change = True
            if (
                child_lock is False
                and trv.system_mode_received is True
                and trv.last_hvac_mode != _org_trv_state.state
                and (mapped_state != HVACMode.OFF or group_all_members_off(self))
            ):
                self.bt_hvac_mode = mapped_state

    _main_key = "temperature"
    if "temperature" not in old_state.attributes:
        _main_key = "target_temp_low"

    _old_heating_setpoint = attr_to_celsius(
        self, old_state, _main_key, None, "trigger_trv_change()"
    )
    _new_heating_setpoint = attr_to_celsius(
        self, new_state, _main_key, None, "trigger_trv_change()"
    )
    _is_no_off_device = trv.advanced.get("no_off_system_mode", False)
    if (
        _new_heating_setpoint is not None
        and _old_heating_setpoint is not None
        and (self.bt_hvac_mode != HVACMode.OFF or _is_no_off_device)
    ):
        _LOGGER.debug(
            "better_thermostat %s: trigger_trv_change / _old_heating_setpoint: %s - _new_heating_setpoint: %s - _last_temperature: %s",
            self.device_name,
            _old_heating_setpoint,
            _new_heating_setpoint,
            trv.last_temperature,
        )
        if (
            _new_heating_setpoint < self.bt_min_temp
            or self.bt_max_temp < _new_heating_setpoint
        ):
            _LOGGER.warning(
                "better_thermostat %s: New TRV %s setpoint outside of range, overwriting it",
                self.device_name,
                entity_id,
            )

            if _new_heating_setpoint < self.bt_min_temp:
                _new_heating_setpoint = self.bt_min_temp
            else:
                _new_heating_setpoint = self.bt_max_temp

        # Step-aware echo detection: changes strictly smaller than the device
        # step are treated as device-side rounding echoes of a BT-written
        # value, not as user input. User input on a TRV display moves the
        # setpoint by at least one step.
        _step_raw = trv.target_temp_step or self.bt_target_temp_step or 0.5
        try:
            _step = float(_step_raw)
        except TypeError, ValueError:
            _step = 0.5
        if _step <= 0:
            _step = 0.5
        # Compare only against values BT itself wrote. ``_old_heating_setpoint``
        # is the TRV's previously published state and is not necessarily a
        # BT-written value, so it does not belong in the echo-suppression set.
        _bt_known_values = (self.bt_target_temp, trv.last_temperature)
        _is_echo = any(
            v is not None and abs(_new_heating_setpoint - v) < _step
            for v in _bt_known_values
        )
        _accept_user_setpoint = (
            not _is_echo
            and not child_lock
            and trv.target_temp_received is True
            and trv.system_mode_received is True
            and trv.hvac_mode != HVACMode.OFF
            and self.window_open is False
            and not trv.ignore_trv_states
        )
        if _accept_user_setpoint:
            _LOGGER.debug(
                "better_thermostat %s: TRV %s decoded TRV target temp changed from %s to %s",
                self.device_name,
                entity_id,
                self.bt_target_temp,
                _new_heating_setpoint,
            )
            self.bt_target_temp = _new_heating_setpoint
            if self.cooler_entity_id is not None:
                if self.bt_target_temp >= self.bt_target_cooltemp:
                    self.bt_target_cooltemp = self.bt_target_temp + (
                        self.bt_target_temp_step or 0.5
                    )

            _main_change = True
        elif _new_heating_setpoint != _old_heating_setpoint:
            # A setpoint change arrived from the TRV but was not adopted as user
            # intent. Record which guard suppressed it so intermittent "change
            # ignored" / "device not syncing" reports can be diagnosed from a
            # debug log instead of guesswork.
            _LOGGER.debug(
                "better_thermostat %s: TRV %s setpoint change %s -> %s NOT adopted "
                "(echo=%s child_lock=%s target_temp_received=%s system_mode_received=%s "
                "hvac_mode=%s window_open=%s ignore_trv_states=%s bt_target_temp=%s "
                "last_temperature=%s step=%s)",
                self.device_name,
                entity_id,
                _old_heating_setpoint,
                _new_heating_setpoint,
                _is_echo,
                child_lock,
                trv.target_temp_received,
                trv.system_mode_received,
                trv.hvac_mode,
                self.window_open,
                trv.ignore_trv_states,
                self.bt_target_temp,
                trv.last_temperature,
                _step,
            )

        if trv.advanced.get("no_off_system_mode", False):
            if _new_heating_setpoint == trv.min_temp:
                # Only set OFF if window is NOT open - min_temp during window
                # open was set by BT, not by user turning off heating - and only
                # when the whole group agrees, so a single no_off valve dropping
                # to min_temp cannot switch the room off.
                if not self.window_open and group_all_members_off(self):
                    if self.bt_hvac_mode != HVACMode.OFF:
                        _LOGGER.debug(
                            "better_thermostat %s: TRV %s reported min_temp %s on a "
                            "no_off_system_mode device -> interpreting as heating OFF",
                            self.device_name,
                            entity_id,
                            _new_heating_setpoint,
                        )
                    self.bt_hvac_mode = HVACMode.OFF
            else:
                self.bt_hvac_mode = HVACMode.HEAT
            _main_change = True

    if _main_change is True:
        self.async_write_ha_state()
        return await self.control_queue_task.put(self)

    self.async_write_ha_state()
    return


def convert_inbound_states(self, entity_id, state: State) -> str | None:
    """Convert HVAC mode in a thermostat state from Home Assistant.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id :
        entity id of the TRV whose state is being converted
    state : State
        Inbound thermostat state, which will be modified

    Returns
    -------
    Modified state
    """

    if state is None:
        raise TypeError("convert_inbound_states() received None state, cannot convert")

    if state.attributes is None or state.state is None:
        raise TypeError("convert_inbound_states() received None state, cannot convert")

    remapped_state = mode_remap(self, entity_id, str(state.state), True)

    if remapped_state not in (HVACMode.OFF, HVACMode.HEAT):
        return None
    return remapped_state


def convert_outbound_states(self, entity_id, hvac_mode) -> dict | None:
    """Convert outbound states for TRV control.

    Returns the payload for setting the TRV state.
    """
    _new_local_calibration = None
    _new_heating_setpoint = None
    _new_valve_position = None

    try:
        _calibration_type = self.real_trvs[entity_id].advanced.get("calibration")
        _calibration_mode = self.real_trvs[entity_id].advanced.get("calibration_mode")

        if _calibration_type is None:
            _LOGGER.warning(
                "better_thermostat %s: no calibration type found in device config, talking to the TRV using fallback mode",
                self.device_name,
            )
            # Fallback: do not apply local calibration, only set the target temperature
            _new_heating_setpoint = self.bt_target_temp
            _new_local_calibration = None

        elif _calibration_type == CalibrationType.LOCAL_BASED:
            _new_local_calibration = calculate_calibration_local(self, entity_id)
            _new_heating_setpoint = self.bt_target_temp

        elif _calibration_type in (
            CalibrationType.TARGET_TEMP_BASED,
            CalibrationType.DIRECT_VALVE_BASED,
        ):
            if _calibration_mode == CalibrationMode.NO_CALIBRATION:
                _new_heating_setpoint = self.bt_target_temp
            else:
                _new_heating_setpoint = calculate_calibration_setpoint(self, entity_id)
            _new_local_calibration = None

        else:
            # Unknown calibration type - use fallback
            _LOGGER.warning(
                "better_thermostat %s: unknown calibration type %s, using fallback mode",
                self.device_name,
                _calibration_type,
            )
            _new_heating_setpoint = self.bt_target_temp
            _new_local_calibration = None

        # System mode handling - applies to ALL calibration modes including fallback
        _system_modes = self.real_trvs[entity_id].hvac_modes
        _has_system_mode = _system_modes is not None

        # Normalize without forcing to str to avoid values like "HVACMode.HEAT"
        _orig_mode = hvac_mode
        hvac_mode = mode_remap(self, entity_id, hvac_mode, False)
        _LOGGER.debug(
            "better_thermostat %s: convert_outbound_states(%s) system_mode in=%s out=%s",
            self.device_name,
            entity_id,
            _orig_mode,
            hvac_mode,
        )

        if not _has_system_mode:
            _LOGGER.debug(
                "better_thermostat %s: device config expects no system mode, while the device has one. Device system mode will be ignored",
                self.device_name,
            )
            if hvac_mode == HVACMode.OFF:
                _new_heating_setpoint = self.real_trvs[entity_id].min_temp
            hvac_mode = None
            _LOGGER.debug(
                "better_thermostat %s: convert_outbound_states(%s) suppressing system_mode for no-off device",
                self.device_name,
                entity_id,
            )
        if hvac_mode == HVACMode.OFF and (
            (_system_modes is not None and HVACMode.OFF not in _system_modes)
            or self.real_trvs[entity_id].advanced.get("no_off_system_mode")
        ):
            _min_temp = self.real_trvs[entity_id].min_temp
            _LOGGER.debug(
                "better_thermostat %s: sending %s°C to the TRV because this device has no system mode off and heater should be off",
                self.device_name,
                _min_temp,
            )
            _new_heating_setpoint = _min_temp
            hvac_mode = None

        # Build payload; include calibration only if present
        _payload = {
            "temperature": _new_heating_setpoint,
            "local_temperature": self.real_trvs[entity_id].current_temperature,
            "system_mode": hvac_mode,
        }
        if _new_local_calibration is not None:
            _payload["local_temperature_calibration"] = _new_local_calibration
        if _new_valve_position is not None:
            _payload["valve_position"] = _new_valve_position
        return _payload
    except Exception as e:
        _LOGGER.exception(
            "better_thermostat %s: exception in convert_outbound_states for %s: %s",
            self.device_name,
            entity_id,
            e,
        )
        return None
