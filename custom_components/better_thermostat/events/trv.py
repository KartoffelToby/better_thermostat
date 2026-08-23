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
    trv_state_unknown_as_available,
)
from custom_components.better_thermostat.utils.const import (
    CONF_HOMEMATICIP,
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.helpers import (
    TRV_SETPOINT_KEYS,
    adopt_reported_hvac_modes,
    attr_to_celsius,
    convert_to_float,
    cooling_owns_dual_role_report,
    device_offers_mode,
    dual_role_entity_id,
    get_device_model,
    group_all_members_off,
    is_reasonable_temperature,
    mode_remap,
    normalize_step,
    read_setpoint_celsius,
    resolve_inbound_setpoint,
    resolve_state_change_event,
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
    resolved_event = resolve_state_change_event(self, event, "TRV")
    if resolved_event is None:
        return
    old_state, new_state, entity_id = resolved_event

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

    state_unknown_as_available = trv_state_unknown_as_available(self, entity_id)
    if _org_trv_state.state == STATE_UNAVAILABLE or (
        (not state_unknown_as_available) and _org_trv_state.state == STATE_UNKNOWN
    ):
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

    advanced = trv.advanced or {}
    # A missing flag counts as unlocked, and it can be missing: nothing
    # backfills the key, so an entry that has not been through the options flow
    # carries none. The config flow, the child lock switch and both guards
    # below read an absent flag the same way.
    child_lock = advanced.get("child_lock")

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

    # The offered HVAC modes change at runtime on devices whose heating /
    # cooling changeover is driven centrally, so every mode is judged against
    # the currently reported list rather than the startup snapshot. The
    # adoption precedes the inbound remapping below because the state carried
    # by this event is a state of the capabilities it reports: remapping it
    # against the previous list decodes it into a mode of a device that no
    # longer exists, and the entity mode that follows from it is emitted
    # before any later cache update could correct it.
    adopt_reported_hvac_modes(trv, _org_trv_state.attributes.get("hvac_modes"))

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
                not child_lock
                and trv.system_mode_received is True
                and trv.last_hvac_mode != _org_trv_state.state
                and (mapped_state != HVACMode.OFF or group_all_members_off(self))
            ):
                self.bt_hvac_mode = mapped_state

    # The previous state only answers whether the TRV was publishing a setpoint
    # at all, so it is read without clamping or echo detection.
    _old_heating_setpoint = read_setpoint_celsius(
        self, old_state, TRV_SETPOINT_KEYS, "trigger_trv_change()"
    )
    # Compare only against values BT itself wrote. ``_old_heating_setpoint`` is
    # the TRV's previously published state and is not necessarily a BT-written
    # value, so it does not belong in the echo-suppression set.
    _step = normalize_step(trv.target_temp_step or self.bt_target_temp_step)
    # A device that carries both the heating and the cooling role reports one
    # setpoint for two targets, so the set of values BT itself wrote holds what
    # either channel wrote: the cooling channel's own write is no more a user
    # press than the heating channel's is. Both cooling values are needed —
    # the send cache is primed only once the service call returns, while the
    # cooling target already holds the value the call is carrying.
    _cooling_owns = cooling_owns_dual_role_report(self, entity_id, _org_trv_state.state)
    if entity_id == dual_role_entity_id(self):
        _known_values = (
            self.bt_target_temp,
            trv.last_temperature,
            self.bt_target_cooltemp,
            self.last_sent_cooler_temp,
        )
    else:
        _known_values = (self.bt_target_temp, trv.last_temperature)
    _setpoint = resolve_inbound_setpoint(
        self,
        new_state,
        keys=TRV_SETPOINT_KEYS,
        known_values=_known_values,
        step=_step,
        log_source="trigger_trv_change()",
    )
    _is_no_off_device = advanced.get("no_off_system_mode", False)
    if (
        _setpoint is not None
        and _old_heating_setpoint is not None
        and (self.bt_hvac_mode != HVACMode.OFF or _is_no_off_device)
    ):
        _LOGGER.debug(
            "better_thermostat %s: trigger_trv_change / _old_heating_setpoint: %s - _new_heating_setpoint: %s - _last_temperature: %s",
            self.device_name,
            _old_heating_setpoint,
            _setpoint.value,
            trv.last_temperature,
        )
        # The no_off OFF detection compares against the device's true min_temp,
        # so it uses the reported value, not one the clamp may have raised into
        # [bt_min_temp, bt_max_temp].
        _raw_heating_setpoint = _setpoint.raw
        _new_heating_setpoint = _setpoint.value
        _is_echo = _setpoint.is_echo
        _accept_user_setpoint = (
            not _is_echo
            and not child_lock
            and trv.target_temp_received is True
            and trv.system_mode_received is True
            and trv.hvac_mode != HVACMode.OFF
            and self.contact_open is False
            and not trv.ignore_trv_states
        )
        if _accept_user_setpoint:
            if _setpoint.clamped:
                _LOGGER.warning(
                    "better_thermostat %s: New TRV %s setpoint outside of range, overwriting it",
                    self.device_name,
                    entity_id,
                )
            if _cooling_owns:
                # The device is running as the cooler, so a press on its own
                # remote names a cooling setpoint. It is filed under the
                # cooling channel with the same bound the cooler handler
                # applies: the value is raised to clear the heating target
                # rather than pulling that target down.
                _adopted_cooling_setpoint = self._clamp_inbound_cool_target(
                    _new_heating_setpoint
                )
                if _adopted_cooling_setpoint != _new_heating_setpoint:
                    _LOGGER.info(
                        "better_thermostat %s: TRV %s reported setpoint %.2f does "
                        "not clear the heating target %.2f, keeping %.2f",
                        self.device_name,
                        entity_id,
                        _new_heating_setpoint,
                        self.bt_target_temp,
                        _adopted_cooling_setpoint,
                    )
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s decoded cooling target changed "
                    "from %s to %s",
                    self.device_name,
                    entity_id,
                    self.bt_target_cooltemp,
                    _adopted_cooling_setpoint,
                )
                self.bt_target_cooltemp = _adopted_cooling_setpoint
                # Residual tie-break only, the counterpart of the one below.
                self._enforce_heat_below_cool()
            else:
                # A knob turn on the TRV is authoritative for the heating
                # channel alone, so a setpoint that would cross the cooling
                # target is lowered to clear it and the cooling target stays
                # where the user put it.
                _adopted_heating_setpoint = self._clamp_inbound_heat_target(
                    _new_heating_setpoint
                )
                if _adopted_heating_setpoint != _new_heating_setpoint:
                    # A user turning the knob up reports every intermediate
                    # setpoint, so this is annunciated at info level: the target
                    # is being honoured as far as the cooling channel allows,
                    # which is not the anomaly a warning stands for.
                    _LOGGER.info(
                        "better_thermostat %s: TRV %s reported setpoint %.2f does not "
                        "clear the cooling target %.2f, keeping %.2f",
                        self.device_name,
                        entity_id,
                        _new_heating_setpoint,
                        self.bt_target_cooltemp,
                        _adopted_heating_setpoint,
                    )
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s decoded TRV target temp changed from %s to %s",
                    self.device_name,
                    entity_id,
                    self.bt_target_temp,
                    _adopted_heating_setpoint,
                )
                self.bt_target_temp = _adopted_heating_setpoint
                if self.cooler_entity_id is not None:
                    # Residual tie-break only: the clamp already cleared the
                    # cooling target unless it ran into bt_min_temp, so this
                    # moves the cooling target by at most one step as long as
                    # that target lies inside the configured range, and only
                    # when no legal heating setpoint below it exists. A range
                    # the children narrowed above a target already in place is
                    # the exception.
                    self._enforce_cool_above_heat()

            _main_change = True
        elif _new_heating_setpoint != _old_heating_setpoint:
            # A setpoint change arrived from the TRV but was not adopted as user
            # intent. Record which guard suppressed it so intermittent "change
            # ignored" / "device not syncing" reports can be diagnosed from a
            # debug log instead of guesswork.
            _LOGGER.debug(
                "better_thermostat %s: TRV %s setpoint change %s -> %s NOT adopted "
                "(echo=%s child_lock=%s target_temp_received=%s system_mode_received=%s "
                "hvac_mode=%s window_open=%s door_open=%s ignore_trv_states=%s "
                "bt_target_temp=%s last_temperature=%s step=%s)",
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
                self.door_open,
                trv.ignore_trv_states,
                self.bt_target_temp,
                trv.last_temperature,
                _step,
            )

        if advanced.get("no_off_system_mode", False):
            if _raw_heating_setpoint == trv.min_temp:
                # Only set OFF if no window/door contact is open - min_temp
                # during an open contact was set by BT, not by the user turning
                # off heating - and only
                # when the whole group agrees, so a single no_off valve dropping
                # to min_temp cannot switch the room off.
                if not self.contact_open and group_all_members_off(self):
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
                # A valve that was switched off at the knob reports its turn
                # back up while bt_hvac_mode still reads OFF, so the tie-break
                # in the setpoint block above was gated out for a heating
                # target the bound had to pin to the cooling target at
                # bt_min_temp. Resolving the mode is what puts a group with a
                # cooler into HEAT_COOL, so that pair is separated here. This
                # branch also runs on reports that leave the mode as it was,
                # where the call only acts on a pair that is already crossed —
                # the one case it exists to settle wherever it is called from.
                self._enforce_cool_above_heat()
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
    advanced = self.real_trvs[entity_id].advanced or {}

    try:
        _calibration_type = advanced.get("calibration")
        _calibration_mode = advanced.get("calibration_mode")

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
        # The cache holds the device's own spelling, so whether it offers OFF is
        # decided on the normalized list, like every other capability check.
        if hvac_mode == HVACMode.OFF and (
            (
                _system_modes is not None
                and not device_offers_mode(_system_modes, HVACMode.OFF)
            )
            or advanced.get("no_off_system_mode")
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
