"""TRV event handlers and helpers for better_thermostat.

This module contains the various Home Assistant TRV event handlers and
helper functions used by the Better Thermostat integration to read and
convert thermostat states and prepare outbound payloads.
"""

from __future__ import annotations

import logging
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State, callback
from homeassistant.util import dt as dt_util

from custom_components.better_thermostat.adapters.delegate import get_current_offset
from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.events.cooler import cooling_writes_as_held
from custom_components.better_thermostat.events.temperature import (
    queue_control_cycle,
    refresh_room_temperature_from_trvs,
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
    published_in_whole_fahrenheit,
    read_setpoint_celsius,
    resolve_inbound_setpoint,
    resolve_state_change_event,
    room_mode_intent,
    setpoint_at_minimum,
    setpoint_echo_window,
)

if TYPE_CHECKING:
    from custom_components.better_thermostat.trv import Trv

_LOGGER = logging.getLogger(__name__)


def accepts_user_setpoint(
    trv: Trv,
    *,
    is_echo: bool,
    child_lock: bool | None,
    contact_open: bool,
    was_off: bool,
) -> bool:
    """Decide whether a setpoint a TRV reports is a user press to adopt.

    Parameters
    ----------
    trv
        The device that reported the setpoint. ``target_temp_received``
        and ``system_mode_received`` say whether BT's own commands have
        landed, ``hvac_mode`` says whether the device is off, and
        ``ignore_trv_states`` is set while BT drives the device.
    is_echo
        Whether the reported value is a BT write coming back.
    child_lock
        Whether the device is configured as child-locked, so a press on
        its knob does not speak for the user. ``None`` is an unset
        option and reads as not locked.
    contact_open
        Whether a window or door contact of the room is open.
    was_off
        Whether the device was off before this report. A report that
        switches it on carries a setpoint turned while it was off, which is
        no more a press than one reported while it is still off.

    Returns
    -------
    bool
        ``True`` when the report is a user press BT adopts as its own
        target, ``False`` when any guard suppresses it.
    """
    return (
        not is_echo
        and not child_lock
        and trv.target_temp_received is True
        and trv.system_mode_received is True
        and trv.hvac_mode != HVACMode.OFF
        and not was_off
        and contact_open is False
        and not trv.ignore_trv_states
    )


@callback
def _hold_report(
    self, trv: Trv, old_state: State | None, new_state: State | None
) -> None:
    """Park a report that arrives while a control cycle holds the handler off.

    The end of the cycle reads the device's state against the state kept
    here, which answers the one question the handler asks of a previous
    state: whether the device was publishing a setpoint. A report whose
    previous state carries none is the device coming back, and the state it
    came back from becomes the reference. A later report that moves the
    setpoint the device came back with makes the state before that move the
    reference, so a knob turned after the return is still read as a press.
    A later report that switches the device on makes the off state it
    switched on from the reference, and off the mode it is judged against,
    as the handler would have cached the earlier report outside a cycle.
    A report that moves the setpoint of a device that is on, after the
    reference was set to off, makes the state before that move the reference
    and its mode the mode it is judged against: outside a cycle the handler
    has cached the device as on by then, and reads the move as a press.
    """
    previous_setpoint = _held_setpoint(self, old_state)
    returned = previous_setpoint is None
    moved_after_return = _held_setpoint(
        self, trv.state_before_held_report
    ) is None and previous_setpoint != _held_setpoint(self, new_state)
    switched_on_after_first_report = (
        trv.report_unread
        and old_state is not None
        and old_state.state == HVACMode.OFF
        and _reports_on(new_state)
    )
    pressed_after_switch_on = (
        trv.report_unread
        and trv.hvac_mode_before_held_report == HVACMode.OFF
        and _reports_on(old_state)
        and _reports_on(new_state)
        and previous_setpoint != _held_setpoint(self, new_state)
    )
    if not trv.report_unread or returned or moved_after_return:
        trv.state_before_held_report = old_state
        trv.hvac_mode_before_held_report = trv.hvac_mode
    if switched_on_after_first_report:
        trv.state_before_held_report = old_state
        trv.hvac_mode_before_held_report = HVACMode.OFF
    if pressed_after_switch_on and old_state is not None:
        trv.state_before_held_report = old_state
        trv.hvac_mode_before_held_report = old_state.state
    trv.report_unread = True


def _reports_on(state: State | None) -> bool:
    """Return whether a held report's state names a mode other than off."""
    return state is not None and state.state not in (
        HVACMode.OFF,
        STATE_UNAVAILABLE,
        STATE_UNKNOWN,
    )


def _held_setpoint(self, state: State | None) -> float | None:
    """Return the setpoint a held report's state carries, or None."""
    return read_setpoint_celsius(self, state, TRV_SETPOINT_KEYS, "_hold_report()")


async def trigger_trv_change(
    self,
    event,
    *,
    mode_settled: bool = False,
    request_cycle: bool = True,
    prior_hvac_mode: str | None = None,
):
    """Trigger a change in the trv state.

    ``mode_settled`` reads a report whose mode the end of a control cycle
    has already settled, so the mode it carries is left to the device's next
    report. ``request_cycle=False`` reads the report without requesting a
    control cycle for it, for a caller that decides that itself.
    ``prior_hvac_mode`` is the mode the device was cached in before the
    report, for a caller whose cache has moved since; without it the cache
    is that mode.
    """
    if self.startup_running:
        return
    if self.control_queue_task is None:
        return
    if self.bt_target_temp is None or self.cur_temp is None or self.tolerance is None:
        return
    if self.bt_update_lock:
        return
    _main_change = False
    _room_temperature_changed = False
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
        # During the room sensor fallback the room is taken from a TRV that
        # still reports, and a room temperature that moves is controlled on.
        if refresh_room_temperature_from_trvs(self):
            self.async_write_ha_state()
            queue_control_cycle(self)
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
    # Only a report that carries no readable internal temperature invalidates
    # the stored one; a marker value such as AVM's 126.5 / 127 °C is ignored
    # below and leaves the stored reading in place.
    _reports_no_temp = _new_current_temp is None
    # The room sensor fallback takes the room from the first TRV whose report
    # carries a plausible temperature. A report that turns a marker value back
    # into a plausible reading puts the TRV back into that choice while the
    # stored reading it carries is unchanged or held back by the debounce.
    _previous_temp = attr_to_celsius(
        self, old_state, "current_temperature", None, "TRV_previous_temp"
    )
    _marker_cleared = (
        self.room_sensor_fallback
        and _new_current_temp is not None
        and _previous_temp is not None
        and is_reasonable_temperature(_new_current_temp)
        and not is_reasonable_temperature(_previous_temp)
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

    # A HomematicIP valve is radio-duty-cycle limited and is therefore read
    # far apart; every other integration only needs the short anti-flicker
    # window. Both the interval and the stamp it is measured against belong to
    # the device this event came from, so a duty-cycle limit on one valve does
    # not hold back the internal temperature of the other valves in the room.
    _time_diff = 600 if advanced.get(CONF_HOMEMATICIP) else 5
    _last_internal_change = trv.last_internal_sensor_change
    _internal_temp_taken = False
    if _reports_no_temp:
        # A report without an internal temperature leaves no live value to
        # keep: the stored one would otherwise feed the calibration for as
        # long as the device keeps reporting without it.
        if trv.current_temperature is not None:
            _LOGGER.debug(
                "better_thermostat %s: TRV %s reports no internal "
                "temperature; invalidating %s",
                self.device_name,
                entity_id,
                trv.current_temperature,
            )
            trv.current_temperature = None
            # The next valid reading is the first live data after the gap
            # and must not be dropped by the debounce below.
            trv.accept_next_internal_temp = True
            _main_change = True
    elif (
        _new_current_temp is not None
        and trv.current_temperature != _new_current_temp
        and (
            trv.consume_accept_next_internal_temp()
            or _last_internal_change is None
            or (dt_util.now() - _last_internal_change).total_seconds() > _time_diff
            or (trv.calibration_received is False and trv.calibration != 1)
        )
    ):
        _internal_temp_taken = True
        _old_temp = trv.current_temperature
        trv.current_temperature = _new_current_temp
        _LOGGER.debug(
            "better_thermostat %s: TRV %s sends new internal temperature from %s to %s",
            self.device_name,
            entity_id,
            _old_temp,
            _new_current_temp,
        )
        trv.last_internal_sensor_change = dt_util.now()
        _main_change = True
        _room_temperature_changed = refresh_room_temperature_from_trvs(self)
        if _room_temperature_changed:
            self.async_write_ha_state()

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

        # A room temperature the room sensor fallback takes from the report,
        # including the one that starts a due fallback, is controlled on
        # even when the report also confirms an offset write.
        if _room_temperature_changed:
            _main_change = True

    # The room sensor fallback reads the TRVs' live reports, so a due
    # fallback starts on the first report that carries a usable temperature,
    # and an active one moves off a TRV whose report carries none, whether or
    # not the stored internal temperature changed. A usable reading that the
    # debounce held back moves the active fallback no more than the TRV,
    # unless the report is the one that clears a marker value.
    if not _internal_temp_taken and (
        self.room_sensor_fallback_due or _new_current_temp is None or _marker_cleared
    ):
        if refresh_room_temperature_from_trvs(self):
            self.async_write_ha_state()
            _main_change = True

    if self.ignore_states:
        # A control cycle is running and the rest of the report is held
        # for its end. An internal temperature it took, and with it a room
        # temperature it changed during the room sensor fallback, asks the
        # end of the cycle for one more; the confirmation of an offset
        # write, which cleared _main_change, does not.
        _hold_report(self, trv, old_state, new_state)
        if _main_change:
            trv.temperature_moved_while_held = True
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

    _was_off = (
        prior_hvac_mode if prior_hvac_mode is not None else trv.hvac_mode
    ) == HVACMode.OFF
    if (
        mapped_state in (HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL)
        and not mode_settled
    ):
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
            # A mode the room took back before the device applied it is
            # Better Thermostat's own command landing late, not a press, for
            # as long as a device is given to apply a command.
            _withdrawn_command_landed = False
            if trv.withdrawn_hvac_mode is not None:
                _withdrawn_still_pending = (
                    trv.withdrawn_hvac_mode_until is not None
                    and monotonic() < trv.withdrawn_hvac_mode_until
                )
                _withdrawn_command_landed = _withdrawn_still_pending and (
                    trv.withdrawn_hvac_mode == _org_trv_state.state
                )
                if _withdrawn_command_landed or not _withdrawn_still_pending:
                    trv.withdrawn_hvac_mode = None
                    trv.withdrawn_hvac_mode_until = None
            if (
                not child_lock
                and not _withdrawn_command_landed
                and trv.system_mode_received is True
                and trv.last_hvac_mode != _org_trv_state.state
                and (mapped_state != HVACMode.OFF or group_all_members_off(self))
            ):
                # The decoded mode is the room's intent, which the service
                # paths store the same way; get_hvac_bt_mode() publishes it as
                # HEAT_COOL in a room with a cooler.
                self.bt_hvac_mode = room_mode_intent(HVACMode(mapped_state))

    if (
        child_lock
        and not mode_settled
        and new_state.state != old_state.state
        and _org_trv_state.state != trv.last_hvac_mode
    ):
        # A mode switched at a locked device is not adopted, whichever mode it
        # is, and the cycle requested for it drives the device back to the
        # mode Better Thermostat last sent it.
        _main_change = True

    # The previous state only answers whether the TRV was publishing a setpoint
    # at all, so it is read without clamping or echo detection.
    _old_heating_setpoint = read_setpoint_celsius(
        self, old_state, TRV_SETPOINT_KEYS, "trigger_trv_change()"
    )
    # Compare only against values BT itself wrote to this device: the last
    # command, the setpoint the device last confirmed, and the writes since,
    # which a device may still hold against a later write it did not take.
    # The room target is not one of them: the device holds it rounded onto
    # its own grid, and a knob turned one step toward an off-grid target
    # lands closer to the target than a step. ``_old_heating_setpoint`` is
    # the TRV's previously published state and is not necessarily a
    # BT-written value, so it does not belong in the echo-suppression set.
    _step = normalize_step(trv.target_temp_step or self.bt_target_temp_step)
    # A device that carries both the heating and the cooling role reports one
    # setpoint for two targets, so the set of values BT itself wrote holds what
    # either channel wrote: the cooling channel's own write is no more a user
    # press than the heating channel's is.
    _cooling_owns = cooling_owns_dual_role_report(self, entity_id, _org_trv_state.state)
    if entity_id == dual_role_entity_id(self):
        _known_values = (
            trv.last_temperature,
            trv.confirmed_setpoint,
            *trv.echo_setpoint_values(),
            *cooling_writes_as_held(self, _org_trv_state),
        )
    else:
        _known_values = (
            trv.last_temperature,
            trv.confirmed_setpoint,
            *trv.echo_setpoint_values(),
        )
    _setpoint = resolve_inbound_setpoint(
        self,
        new_state,
        keys=TRV_SETPOINT_KEYS,
        known_values=_known_values,
        step=_step,
        log_source="trigger_trv_change()",
    )
    _is_no_off_device = advanced.get("no_off_system_mode", False)
    # An AUTO the remap did not decode is a report from a device without the
    # heat auto swapped option, running a mode of its own; its setpoint is not
    # a target for the room, so neither the setpoint nor the mode it implies
    # on a no_off device is adopted. The setpoint comes from the event's own
    # state, so that state decides, not the registry state, which may already
    # hold a later report.
    _ignored_auto_report = new_state.state == HVACMode.AUTO and mode_remap(
        self, entity_id, str(new_state.state), True
    ) not in (HVACMode.OFF, HVACMode.HEAT)
    if (
        _setpoint is not None
        and _old_heating_setpoint is not None
        and (self.bt_hvac_mode != HVACMode.OFF or _is_no_off_device)
        and not _ignored_auto_report
    ):
        # The logs name the value the TRV reported; BT's range clamp is shown
        # beside it, so a setpoint BT wrote above its own maximum does not
        # read as capped.
        _reported_setpoint = (
            f"{_setpoint.raw} (clamped to {_setpoint.value})"
            if _setpoint.clamped
            else f"{_setpoint.value}"
        )
        _LOGGER.debug(
            "better_thermostat %s: trigger_trv_change / _old_heating_setpoint: %s - _new_heating_setpoint: %s - _last_temperature: %s",
            self.device_name,
            _old_heating_setpoint,
            _reported_setpoint,
            trv.last_temperature,
        )
        # The no_off OFF detection compares against the TRV's minimum, so it
        # uses the reported value, not one the clamp may have raised into
        # [bt_min_temp, bt_max_temp].
        _raw_heating_setpoint = _setpoint.raw
        _new_heating_setpoint = _setpoint.value
        _is_echo = _setpoint.is_echo
        _accept_user_setpoint = accepts_user_setpoint(
            trv,
            is_echo=_is_echo,
            child_lock=child_lock,
            contact_open=self.contact_open,
            was_off=_was_off,
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
                trv.remember_setpoint_adopted(_raw_heating_setpoint)
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
        elif (
            child_lock
            and not _is_echo
            and abs(_raw_heating_setpoint - _old_heating_setpoint)
            >= setpoint_echo_window(_step)
        ):
            # A turn at a locked device is not adopted, and the cycle requested
            # for it writes the room's setpoint back over it.
            _LOGGER.debug(
                "better_thermostat %s: TRV %s is child-locked, turning its "
                "setpoint %s back",
                self.device_name,
                entity_id,
                _new_heating_setpoint,
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
                "hvac_mode=%s window_open=%s door_open=%s ignore_trv_states=%s "
                "bt_target_temp=%s last_temperature=%s step=%s)",
                self.device_name,
                entity_id,
                _old_heating_setpoint,
                _reported_setpoint,
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
            # The setpoint of a device without an off mode carries the room's
            # mode, so a report is a control change only where it moves it.
            _room_before = (self.bt_hvac_mode, self.bt_target_cooltemp)
            if setpoint_at_minimum(
                _raw_heating_setpoint,
                trv.min_temp,
                step=trv.target_temp_step,
                whole_degrees=published_in_whole_fahrenheit(
                    new_state, self.hass.config.units.temperature_unit
                ),
            ):
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
            if (self.bt_hvac_mode, self.bt_target_cooltemp) != _room_before:
                _main_change = True

    if _main_change is True and request_cycle:
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
