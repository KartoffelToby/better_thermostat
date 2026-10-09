"""TRV event handlers and helpers for better_thermostat.

This module contains the various Home Assistant TRV event handlers and
helper functions used by the Better Thermostat integration to read and
convert thermostat states and prepare outbound payloads.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import TYPE_CHECKING, NotRequired, TypedDict

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from custom_components.better_thermostat.adapters.delegate import get_calibration_offset
from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.events.cooler import cooling_writes_as_held
from custom_components.better_thermostat.model_fixes.model_quirks import (
    load_model_quirks,
    trv_report_is_unreadable,
)
from custom_components.better_thermostat.utils.advanced_flags import advanced_flag
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_CHILD_LOCK,
    CONF_HOMEMATICIP,
    CONF_NO_OFF_SYSTEM_MODE,
    CalibrationMode,
    CalibrationOutput,
)
from custom_components.better_thermostat.utils.helpers import (
    TRV_SETPOINT_KEYS,
    adopt_reported_hvac_modes,
    attr_to_celsius,
    configured_calibration_mode,
    configured_calibration_output,
    convert_to_float,
    cooler_send_cache,
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
from custom_components.better_thermostat.utils.scheduler import request_control_cycle

if TYPE_CHECKING:
    from homeassistant.core import Event, EventStateChangedData

    from custom_components.better_thermostat.climate import BetterThermostat
    from custom_components.better_thermostat.trv import Trv

_LOGGER = logging.getLogger(__name__)


class OutboundTrvPayload(TypedDict):
    """The writes one control cycle sends to a TRV, in °C.

    ``system_mode`` is the mode in the device's own spelling, or None when
    the device's mode is left untouched. ``local_temperature_calibration``
    is present only for a TRV calibrated through its local offset.
    """

    temperature: float | None
    local_temperature: float | None
    system_mode: str | None
    local_temperature_calibration: NotRequired[float]


def accepts_user_setpoint(
    trv: Trv, *, is_echo: bool, child_lock: bool, contact_open: bool, was_off: bool
) -> bool:
    """Decide whether a setpoint a TRV reports is a user press to adopt.

    Parameters
    ----------
    trv
        The device that reported the setpoint. ``target_temperature_received``
        and ``system_mode_received`` say whether BT's own commands have
        landed, ``hvac_mode`` says whether the device is off, and
        ``ignore_trv_states`` is set while BT drives the device.
    is_echo
        Whether the reported value is a BT write coming back.
    child_lock
        Whether the device is configured as child-locked, so a press on
        its knob does not speak for the user.
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
        and trv.target_temperature_received is True
        and trv.system_mode_received is True
        and trv.hvac_mode != HVACMode.OFF
        and not was_off
        and contact_open is False
        and not trv.ignore_trv_states
    )


def _hold_report(
    self: BetterThermostat, trv: Trv, old_state: State | None, new_state: State | None
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


def _held_setpoint(self: BetterThermostat, state: State | None) -> float | None:
    """Return the setpoint a held report's state carries, or None."""
    return read_setpoint_celsius(self, state, TRV_SETPOINT_KEYS, "_hold_report()")


def _read_internal_temperature_later(
    self: BetterThermostat, trv: Trv, entity_id: str, interval_seconds: float
) -> None:
    """Read a device's internal temperature again once its debounce is over.

    A reading turned away only because it came within the debounce interval
    of the last one is the device's temperature once the interval is over: a
    device that reports on change says nothing more until it moves again.
    The interval runs from the reading the device last had accepted, which a
    reading that bypasses the debounce can move while the wait is on, so the
    wait lasts until the interval from the latest one is over. The wait runs
    on Home Assistant's timer, and the task around it is what the entity
    cancels when it is removed.

    The pending flag is set before the task is created, since Home Assistant
    starts the task eagerly and its coroutine can run to its end inside the
    call that creates it. It is cleared however the reread ends: by the
    coroutine once it runs, here when no task is created, and by the task's
    done callback when the task is cancelled before its coroutine starts,
    which then never runs a line of it.
    """
    if trv.internal_reread_pending:
        return
    started = False

    async def _wait(delay_seconds: float) -> None:
        due: asyncio.Future[None] = self.hass.loop.create_future()

        @callback
        def _due(_now: datetime) -> None:
            if not due.done():
                due.set_result(None)

        cancel_timer = async_call_later(self.hass, delay_seconds, _due)
        try:
            await due
        finally:
            cancel_timer()

    async def _reread() -> None:
        nonlocal started
        started = True
        try:
            while True:
                _last = trv.last_internal_sensor_change
                if _last is None:
                    break
                _remaining = interval_seconds - (dt_util.now() - _last).total_seconds()
                if _remaining <= 0:
                    break
                await _wait(max(0.1, _remaining))
        finally:
            trv.internal_reread_pending = False
        if self.is_removed or self.real_trvs.get(entity_id) is not trv:
            return
        _state = self.hass.states.get(entity_id)
        # A device that is gone has had its reading invalidated; the
        # attributes it still carries are not a live temperature.
        if trv_report_is_unreadable(self, entity_id, _state):
            return
        _reading = attr_to_celsius(
            self, _state, "current_temperature", None, "TRV_current_temp"
        )
        if (
            _reading is None
            or not is_reasonable_temperature(_reading)
            or _reading == trv.current_temperature
        ):
            return
        _LOGGER.debug(
            "better_thermostat %s: TRV %s internal temperature read again after "
            "the debounce interval: %s to %s",
            self.device_name,
            entity_id,
            trv.current_temperature,
            _reading,
        )
        trv.current_temperature = _reading
        trv.last_internal_sensor_change = dt_util.now()
        request_control_cycle(self)

    def _release_unstarted(_task: asyncio.Task[object]) -> None:
        if not started:
            trv.internal_reread_pending = False

    trv.internal_reread_pending = True
    task = self.task_manager.create_task(
        _reread(), name=f"bt_internal_reread_{entity_id}"
    )
    if task is None:
        trv.internal_reread_pending = False
        return
    task.add_done_callback(_release_unstarted)


async def trigger_trv_change(
    self: BetterThermostat,
    event: Event[EventStateChangedData],
    *,
    mode_settled: bool = False,
    request_cycle: bool = True,
    prior_hvac_mode: str | None = None,
) -> None:
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
    if (
        self.heat_target_temperature is None
        or self.room_temperature is None
        or self.tolerance is None
    ):
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
            "better_thermostat %s: TRV %s is no longer tracked, skipping",
            self.device_name,
            entity_id,
        )
        return

    if trv_report_is_unreadable(self, entity_id, _org_trv_state):
        # The device is gone; its last internal temperature must not
        # keep feeding SENSOR_FALLBACK and the ladder as if it were live.
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
        trv.accept_next_internal_temperature = True
        # Reachability/fail-soft state must re-evaluate now; otherwise it
        # stays stale until the next unrelated event.
        self.async_write_ha_state()
        request_control_cycle(self)
        return

    advanced = trv.advanced or {}
    # A missing flag counts as unlocked, and it can be missing: nothing
    # backfills the key, so an entry that has not been through the options flow
    # carries none, and an older one may hold a string. The config flow, the
    # child lock switch and both guards below read the flag the same way.
    child_lock = advanced_flag(advanced, CONF_CHILD_LOCK)

    # Dynamic model detection: only once (e.g. at startup), not on every event
    try:
        # Only check when the state carries hints
        if not trv.model and (
            "model_id" in _org_trv_state.attributes
            or "device" in _org_trv_state.attributes
        ):
            detected = await get_device_model(
                self, entity_id, configured_model=self.model
            )
            if isinstance(detected, str) and detected:
                _LOGGER.info(
                    "better_thermostat %s: TRV %s model detected: %s; loading quirks",
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
            exc_info=True,
        )

    _new_current_temperature = attr_to_celsius(
        self, _org_trv_state, "current_temperature", None, "TRV_current_temp"
    )
    # Only a report that carries no readable internal temperature invalidates
    # the stored one; a marker value such as AVM's 126.5 / 127 °C is ignored
    # below and leaves the stored reading in place.
    _reports_no_temperature = _new_current_temperature is None
    # SENSOR_FALLBACK counts a stored reading only while the TRV's report
    # confirms it. A report that turns a plausible reading into a marker
    # value takes the TRV out of the mean, and one that turns a marker value
    # back into a plausible reading puts it back, so either moves the room
    # temperature the control law reads while the stored value stays.
    _previous_temperature = attr_to_celsius(
        self, old_state, "current_temperature", None, "TRV_previous_temp"
    )
    if (
        self.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK
        and trv.current_temperature is not None
        and _new_current_temperature is not None
        and _previous_temperature is not None
        and is_reasonable_temperature(_new_current_temperature)
        != is_reasonable_temperature(_previous_temperature)
    ):
        _main_change = True
    if _new_current_temperature is not None and not is_reasonable_temperature(
        _new_current_temperature
    ):
        _LOGGER.warning(
            "better_thermostat %s: TRV %s reports implausible current_temperature "
            "%s; ignoring",
            self.device_name,
            entity_id,
            _new_current_temperature,
        )
        _new_current_temperature = None

    # A HomematicIP valve is radio-duty-cycle limited and is therefore read
    # far apart; every other integration only needs the short anti-flicker
    # window. Both the interval and the stamp it is measured against belong to
    # the device this event came from, so a duty-cycle limit on one valve does
    # not hold back the internal temperature of the other valves in the room.
    _time_diff = 600 if advanced_flag(advanced, CONF_HOMEMATICIP) else 5
    _last_internal_change = trv.last_internal_sensor_change
    if _reports_no_temperature:
        # A report without an internal temperature leaves no live value to
        # keep: the stored one would otherwise feed SENSOR_FALLBACK and the
        # ladder for as long as the device keeps reporting without it.
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
            trv.accept_next_internal_temperature = True
            _main_change = True
    elif (
        _new_current_temperature is not None
        and trv.current_temperature != _new_current_temperature
        and (
            trv.consume_accept_next_internal_temperature()
            or _last_internal_change is None
            or (dt_util.now() - _last_internal_change).total_seconds() > _time_diff
            or (trv.calibration_received is False and trv.calibration != 1)
        )
    ):
        _old_temperature = trv.current_temperature
        trv.current_temperature = _new_current_temperature
        _LOGGER.debug(
            "better_thermostat %s: TRV %s sends new internal temperature from %s to %s",
            self.device_name,
            entity_id,
            _old_temperature,
            _new_current_temperature,
        )
        trv.last_internal_sensor_change = dt_util.now()
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
                # The awaits above (model detection, quirk loading) can
                # outlive the entry: the offset read resolves the adapter
                # through a raw real_trvs index, so skip it once the TRV
                # is no longer tracked.
                if entity_id not in self.real_trvs:
                    _LOGGER.debug(
                        "better_thermostat %s: TRV %s is no longer tracked, "
                        "skipping offset read",
                        self.device_name,
                        entity_id,
                    )
                    return
                trv.last_calibration = await get_calibration_offset(self, entity_id)

        # Under SENSOR_FALLBACK the TRV readings are the room temperature,
        # so a new one is controlled on even when it confirms an offset write.
        if self.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK:
            _main_change = True
    elif (
        _new_current_temperature is not None
        and trv.current_temperature != _new_current_temperature
        and _last_internal_change is not None
    ):
        # Turned away by the debounce alone: the reading is read again once
        # the interval is over.
        _read_internal_temperature_later(self, trv, entity_id, _time_diff)

    if self.ignore_states:
        _hold_report(self, trv, old_state, new_state)
        if _main_change:
            trv.temperature_moved_while_held = True
        return

    # The offered mode list changes at runtime on devices whose
    # heating/cooling changeover is driven centrally, so every mode is judged
    # against the currently reported list rather than the startup snapshot.
    # The adoption precedes the inbound remapping below because the state
    # carried by this event is a state of the capabilities it reports:
    # remapping it against the previous list decodes it into a mode of a
    # device that no longer exists, and the entity mode that follows from it
    # is emitted before any later cache update could correct it.
    adopt_reported_hvac_modes(trv, _org_trv_state.attributes.get("hvac_modes"))

    mapped_state = convert_inbound_states(self, entity_id, _org_trv_state)

    # Always cache the reported hvac_action and valve position so both stay
    # current
    hvac_action_attr = _org_trv_state.attributes.get("hvac_action")
    if hvac_action_attr is None:
        hvac_action_attr = _org_trv_state.attributes.get("action")
    if hvac_action_attr is not None:
        value = str(hvac_action_attr).strip().lower()
        prev = trv.hvac_action
        trv.hvac_action = value
        if prev != value:
            _main_change = True
            _LOGGER.debug(
                "better_thermostat %s: TRV %s hvac_action changed: %s -> %s",
                self.device_name,
                entity_id,
                prev,
                value,
            )

    val_pos = _org_trv_state.attributes.get("valve_position")
    if val_pos is not None:
        trv.valve_position = convert_to_float(
            str(val_pos), self.device_name, "trv_event"
        )

    _was_off = (
        prior_hvac_mode if prior_hvac_mode is not None else trv.hvac_mode
    ) == HVACMode.OFF
    if mapped_state in (HVACMode.OFF, HVACMode.HEAT) and not mode_settled:
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
                    and self.clock.monotonic() < trv.withdrawn_hvac_mode_until
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
    # lands closer to the target than a step.
    # ``_old_heating_setpoint`` is the TRV's previously published state and is
    # not necessarily a BT-written value, so it does not belong in the
    # echo-suppression set.
    _step = normalize_step(trv.target_temp_step or self.bt_target_temperature_step)
    # A device that carries both the heating and the cooling role reports one
    # setpoint for two targets, so the set of values BT itself wrote holds what
    # either channel wrote: the cooling channel's own write is no more a user
    # press than the heating channel's is.
    _cooling_owns = cooling_owns_dual_role_report(self, entity_id, _org_trv_state.state)
    if entity_id == dual_role_entity_id(self):
        _known_values = (
            trv.commanded_setpoint,
            trv.confirmed_setpoint,
            *trv.echo_setpoint_values(),
            *cooling_writes_as_held(self, _org_trv_state),
        )
    else:
        _known_values = (
            trv.commanded_setpoint,
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
        # A report the cooling channel owns is bounded by the cooling range.
        cooling=_cooling_owns,
    )
    _is_no_off_device = advanced_flag(advanced, CONF_NO_OFF_SYSTEM_MODE)
    # An AUTO the mode decoding ignores says nothing about the room, so the
    # setpoint it carries, typically the device's own schedule, is not adopted
    # either. A swapped device decodes AUTO as HEAT and never matches. The
    # setpoint comes from the event's own state, so that state decides, not
    # the registry state, which may already hold a later report.
    _ignored_auto_report = new_state.state == HVACMode.AUTO and mode_remap(
        self, entity_id, new_state.state, True
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
            "better_thermostat %s: trigger_trv_change / _old_heating_setpoint: %s - _new_heating_setpoint: %s - commanded_setpoint: %s",
            self.device_name,
            _old_heating_setpoint,
            _reported_setpoint,
            trv.commanded_setpoint,
        )
        # The no_off OFF detection compares against the TRV's minimum, so it
        # uses the reported value, not one the clamp may have raised into the
        # channel's range.
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
        if _was_off and trv.hvac_mode != HVACMode.OFF and not _is_echo:
            # The report that switches the device on shows the setpoint it
            # held while it was off. That is the device's own value, as it
            # is at startup: the reports after this one carry it as well and
            # are no press either, until a write replaces it.
            trv.remember_setpoint_confirmed(
                _raw_heating_setpoint, trv.confirmed_write_id
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
                        self.heat_target_temperature,
                        _adopted_cooling_setpoint,
                    )
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s decoded cooling target changed "
                    "from %s to %s",
                    self.device_name,
                    entity_id,
                    self.cool_target_temperature,
                    _adopted_cooling_setpoint,
                )
                self.cool_target_temperature = _adopted_cooling_setpoint
                # The turn takes the place of the cooling channel's last write
                # as what the device holds, so the cycle compares the cooling
                # target with the turn rather than with a write the device no
                # longer holds. The turn was not sent, so it carries no send
                # time for the resend throttle, and the device has not settled
                # on any write since.
                _cooler_sent = cooler_send_cache(self)
                _cooler_sent["temperature"] = (_raw_heating_setpoint, None)
                _cooler_sent.pop("temperature_settled", None)
                # Residual tie-break only, the counterpart of the one below.
                self._enforce_heat_below_cool()
            else:
                # What the TRV reports speaks for the heating channel alone: a
                # value that would cross the cooling target is lowered below
                # it, so a knob turn on a radiator valve cannot move the
                # cooler's target.
                _adopted_heating_setpoint = self._clamp_inbound_heat_target(
                    _new_heating_setpoint
                )
                if _adopted_heating_setpoint != _new_heating_setpoint:
                    # A user turning the knob up step by step would collect one
                    # warning per press, so yielding to the cooling target is an
                    # INFO: the range clamp above and the ordering fallback below
                    # own the WARNING level.
                    _LOGGER.info(
                        "better_thermostat %s: TRV %s reported setpoint %.2f does not "
                        "clear the cooling target %.2f, keeping %.2f",
                        self.device_name,
                        entity_id,
                        _new_heating_setpoint,
                        self.cool_target_temperature,
                        _adopted_heating_setpoint,
                    )
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s decoded TRV target temp changed from %s to %s",
                    self.device_name,
                    entity_id,
                    self.heat_target_temperature,
                    _adopted_heating_setpoint,
                )
                self.heat_target_temperature = _adopted_heating_setpoint
                trv.remember_setpoint_adopted(_raw_heating_setpoint)
                # The clamp leaves the cooling target alone, so this only settles
                # the degenerate case where no heating value below the cooling
                # target exists inside the range: at a cooling target within one
                # step of bt_min_temp it lifts that target by one step, and a
                # range the children narrowed above a target already in place is
                # what moves it further — that move is what brings it back inside
                # the range.
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
        elif (
            _is_no_off_device
            and self.contact_open
            and not _is_echo
            and abs(_raw_heating_setpoint - _old_heating_setpoint)
            >= setpoint_echo_window(_step)
        ):
            # BT holds a device without an off mode at its minimum while a
            # contact is open. A turn there is not adopted and switches the
            # room neither on nor off, and the cycle requested for it writes
            # the minimum back over it.
            _LOGGER.debug(
                "better_thermostat %s: TRV %s turned to %s while a window or "
                "door is open, turning it back",
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
                "(echo=%s child_lock=%s target_temperature_received=%s system_mode_received=%s "
                "hvac_mode=%s window_open=%s door_open=%s ignore_trv_states=%s "
                "heat_target_temperature=%s commanded_setpoint=%s pending_setpoints=%s step=%s)",
                self.device_name,
                entity_id,
                _old_heating_setpoint,
                _reported_setpoint,
                _is_echo,
                child_lock,
                trv.target_temperature_received,
                trv.system_mode_received,
                trv.hvac_mode,
                self.window_open,
                self.door_open,
                trv.ignore_trv_states,
                self.heat_target_temperature,
                trv.commanded_setpoint,
                trv.echo_setpoint_values(),
                _step,
            )

        if _is_no_off_device and _accept_user_setpoint:
            # The setpoint of a device without an off mode carries the room's
            # mode, so a report is a control change only where it moves it.
            # Only a press the room adopts speaks for that mode: BT parks the
            # device at its minimum itself while it calls for no heat or a
            # contact is open, and that value coming back is BT's own write,
            # just as a turn at a locked device or one BT ignores is no word
            # from the user.
            _room_before = (self.bt_hvac_mode, self.cool_target_temperature)
            if setpoint_at_minimum(
                _raw_heating_setpoint,
                trv.min_temp,
                step=trv.target_temp_step,
                whole_degrees=published_in_whole_fahrenheit(
                    new_state, self.hass.config.units.temperature_unit
                ),
            ):
                # Only when the whole group agrees, so a single no_off valve
                # dropping to min_temp cannot switch the room off.
                if group_all_members_off(self):
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
                # Leaving OFF is what puts a group with a cooler into HEAT_COOL,
                # so this is the first moment the ordering of the two targets is
                # checked at all: a setpoint adopted while the group was still
                # off has not passed that check yet.
                self._enforce_cool_above_heat()
            if (self.bt_hvac_mode, self.cool_target_temperature) != _room_before:
                _main_change = True

    if _main_change is True and request_cycle:
        self.async_write_ha_state()
        return request_control_cycle(self)

    self.async_write_ha_state()
    return


def convert_inbound_states(
    self: BetterThermostat, entity_id: str, state: State
) -> str | None:
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

    if state.attributes is None or state.state is None:
        raise TypeError("convert_inbound_states() received None state, cannot convert")

    remapped_state = mode_remap(self, entity_id, state.state, True)

    if remapped_state not in (HVACMode.OFF, HVACMode.HEAT):
        return None
    return remapped_state


def convert_outbound_states(
    self: BetterThermostat, entity_id: str, hvac_mode: HVACMode | str | None
) -> OutboundTrvPayload | None:
    """Convert outbound states for TRV control.

    Returns the payload for setting the TRV state.
    """
    _new_local_calibration = None
    _new_heating_setpoint = None
    advanced = self.real_trvs[entity_id].advanced or {}

    try:
        _calibration_output = configured_calibration_output(advanced)
        _calibration_mode = configured_calibration_mode(advanced)

        if _calibration_output == CalibrationOutput.LOCAL_BASED:
            _new_local_calibration = calculate_calibration_local(self, entity_id)
            _new_heating_setpoint = self.heat_target_temperature

        elif _calibration_output in (
            CalibrationOutput.TARGET_TEMP_BASED,
            CalibrationOutput.DIRECT_VALVE_BASED,
        ):
            if _calibration_mode == CalibrationMode.NO_CALIBRATION:
                _new_heating_setpoint = self.heat_target_temperature
            else:
                _new_heating_setpoint = calculate_calibration_setpoint(self, entity_id)
            _new_local_calibration = None

        else:
            # Fallback: do not apply local calibration, only set the target
            # temperature.
            _LOGGER.warning(
                "better_thermostat %s: no known calibration type in device "
                "config (%s), talking to the TRV using fallback mode",
                self.device_name,
                advanced.get(CONF_CALIBRATION),
            )
            _new_heating_setpoint = self.heat_target_temperature
            _new_local_calibration = None

        # System mode handling - applies to ALL calibration modes including fallback
        _system_modes = self.real_trvs[entity_id].hvac_modes
        _has_system_mode = _system_modes is not None

        # Normalize without forcing to str to avoid values like "HVACMode.HEAT"
        _orig_mode = hvac_mode
        # No mode leaves the device's mode untouched, which is what the
        # remap answers for a mode the device does not offer as well.
        hvac_mode = (
            None if hvac_mode is None else mode_remap(self, entity_id, hvac_mode, False)
        )
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
            or advanced_flag(advanced, CONF_NO_OFF_SYSTEM_MODE)
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
        _payload: OutboundTrvPayload = {
            "temperature": _new_heating_setpoint,
            "local_temperature": self.real_trvs[entity_id].current_temperature,
            "system_mode": hvac_mode,
        }
        if _new_local_calibration is not None:
            _payload["local_temperature_calibration"] = _new_local_calibration
        return _payload
    except Exception as e:
        _LOGGER.exception(
            "better_thermostat %s: exception in convert_outbound_states for %s: %s",
            self.device_name,
            entity_id,
            e,
        )
        return None
