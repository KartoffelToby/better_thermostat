"""Controlling module for Better Thermostat."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from datetime import datetime
import logging
import math
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.components.climate.const import (
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    HVACMode,
)
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import (
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.util.unit_conversion import TemperatureConverter

from custom_components.better_thermostat.adapters.delegate import (
    calibration_entity_disabled,
    get_calibration_offset,
    set_calibration_offset,
    set_hvac_mode,
    set_temperature,
    set_valve,
    valve_channel_available,
)
from custom_components.better_thermostat.core.decide import decide, is_boost_heating
from custom_components.better_thermostat.core.desired import DesiredState, TrvDesired
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.core.safety import clamp as safety_clamp
from custom_components.better_thermostat.core.snapshot import WorldSnapshot
from custom_components.better_thermostat.core.watchdog import (
    WATCHDOG_MAX_AGE_S,
    control_loop_stalled,
)
from custom_components.better_thermostat.entity import announce_learned_state
from custom_components.better_thermostat.events.cooler import cooling_writes_as_held
from custom_components.better_thermostat.events.trv import (
    convert_outbound_states,
    trigger_trv_change,
)
from custom_components.better_thermostat.model_fixes.model_quirks import (
    override_set_hvac_mode,
    override_set_temperature,
    trv_report_is_unreadable,
    trv_state_unknown_as_available,
)
from custom_components.better_thermostat.utils.advanced_flags import advanced_flag
from custom_components.better_thermostat.utils.calibration.pid import resolve_unique_id
from custom_components.better_thermostat.utils.const import (
    CONF_CHILD_LOCK,
    CONF_HOMEMATICIP,
    CalibrationMode,
    CalibrationOutput,
)
from custom_components.better_thermostat.utils.helpers import (
    COOLER_SETPOINT_KEYS,
    TRV_SETPOINT_KEYS,
    CoolerCommand,
    CoolerFailureRun,
    CoolerSendCache,
    attr_to_celsius,
    clamp_valve_percent,
    configured_calibration_mode,
    configured_calibration_output,
    convert_to_float,
    cooler_send_cache,
    cooling_owns_dual_role_device,
    dual_role_entity_id,
    get_current_set_temperatures,
    last_sent_cooler_temperature,
    matches_any_setpoint,
    normalize_step,
    on_cooler_grid,
    read_bound_celsius,
    read_setpoint_celsius,
    setpoint_echo_window,
    state_temperature_unit,
    supports_single_target_temperature,
    supports_temperature_range,
)
from custom_components.better_thermostat.utils.hvac_action import (
    COOLER_MODE_HYSTERESIS_K,
    should_cool_with_tolerance,
)
from custom_components.better_thermostat.utils.retry import (
    command_cancellation_as_disconnect,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle
from custom_components.better_thermostat.utils.snapshot import build_snapshot
from custom_components.better_thermostat.utils.watcher import (
    UNAVAILABLE_STATES,
    UNKNOWN_STATES,
)

if TYPE_CHECKING:
    from custom_components.better_thermostat.climate import BetterThermostat
    from custom_components.better_thermostat.trv import Trv
    from custom_components.better_thermostat.utils.telemetry import ValveCommand

_LOGGER = logging.getLogger(__name__)

# Write budget: minimum spacing between non-safety writes to one TRV.
# TRVs are battery- and radio-constrained; bursts of writes are a real
# failure cause. Safety-relevant writes (frost floor, OFF) bypass this.
MIN_WRITE_INTERVAL_S = 30.0
# A HomematicIP head shares its access point's 1 % radio duty cycle (36 s of
# airtime an hour) with every other HomematicIP device in the home, so it is
# written at most once per ten minutes per channel, the interval its own
# internal temperature is read at.
HOMEMATICIP_MIN_WRITE_INTERVAL_S = 600.0
# Device tolerance when comparing commanded vs reported setpoints.
RECONCILE_TOLERANCE_K = 0.05
# Floor for the commanded-vs-reported offset comparison. One declared offset
# step is the right window only while that step describes the grid the device
# reports on; an adapter declaring a nominal 0.01 K step describes a
# continuous range instead, and any report rounded coarser than that then reads
# as a divergence the write gate re-asserts on every control cycle. The floor
# covers those roundings and stays below the 0.1 K resolution a TRV reports its
# own temperature at, so no calibration error the room can feel hides beneath
# it. The setpoint channel's read-back tolerance describes a different
# comparison, so the offset channel carries its own floor.
OFFSET_MATCH_TOLERANCE_K = 0.05
# Resend throttle for the cooler path: cooler commands go straight to the
# service call (no reconciler in between), so an identical command is
# suppressed while the device's state feedback lags. A changed desired value
# passes this throttle untouched; only the failure backoff below can hold it.
#
# An air conditioner protects its compressor by ignoring commands for
# several minutes after a mode change, so re-asserting inside that window
# cannot achieve anything: manufacturers state three minutes, and dedicated
# thermostats hold the compressor off for four to five. The interval sits in
# that band and strictly below the periodic ticks that drive a control cycle
# — the five-minute reconcile and time triggers, the fifteen-minute watchdog
# — because the throttle compares strictly and the clock is read partway
# into a cycle: at exactly one tick period, scheduling jitter would decide
# whether a tick counts, and the pacing would land anywhere between one and
# two tick periods. A device that applies what it is told never reaches the
# interval at all — the timestamp only advances on a send, so a converged
# cooler leaves the window permanently open and a divergence appearing after
# that convergence is corrected on the next cycle.
COOLER_RESEND_INTERVAL_S = 240.0
# A rejected cooler command is not a completed send, so the resend throttle
# cannot pace its retry. Consecutive failures on one channel are paced by
# their own backoff instead, starting at this base. The base is deliberately
# shorter than the resend interval: a rejected command never reached the
# device, so there is no compressor window to respect, and the wait exists
# only to keep a rate-limited endpoint from being retried on every cycle.
COOLER_FAILURE_BACKOFF_BASE_S = 30.0
# Growth of that backoff: the first retry waits the base, each further one
# doubles the wait by this factor.
COOLER_FAILURE_BACKOFF_FACTOR = 2.0
# Ceiling of that backoff, half an hour. A channel that has been rejected
# this often is not going to accept the next command either, so the run is
# paced well beyond the resend interval; a device that starts working again
# is picked up on the following attempt.
COOLER_FAILURE_BACKOFF_MAX_S = 1800.0
# Longest run the backoff can tell apart. Past it the wait is pinned at the
# ceiling anyway, while the exponent would keep growing on a device that
# rejects every write until the float power overflows and takes the whole
# cooler cycle down with it.
COOLER_FAILURE_BACKOFF_MAX_RUN = 1 + math.ceil(
    math.log(
        COOLER_FAILURE_BACKOFF_MAX_S / COOLER_FAILURE_BACKOFF_BASE_S,
        COOLER_FAILURE_BACKOFF_FACTOR,
    )
)
# A cooler may snap a received setpoint onto its own step grid (e.g. 0.5 °C,
# or a whole-°F grid). A post-send reading within this distance of the sent
# value counts as that device-side quantization, not as an unapplied command.
COOLER_QUANTIZATION_TOLERANCE_K = 0.5
# Valve deviations below this are the device's own business.
RECONCILE_VALVE_TOLERANCE_PCT = 5.0
# Pause before re-queueing a cycle in which a TRV reported failure, so a
# persistently failing device cannot spin the control queue. Each further
# failure of the same cycle doubles the pause.
FAILED_CYCLE_BACKOFF_S = 2.0
# Ceiling of that pause. The five-minute reconcile tick queues a cycle for a
# device that does not hold what it was sent anyway, so a longer pause would
# not space the attempts any further; it would only hold back the retry that
# picks up a device which starts accepting the command again.
FAILED_CYCLE_BACKOFF_MAX_S = 300.0
# Once a run of failed cycles has reached that ceiling it is reported as a
# warning at most this often, with how long it has lasted; the attempts in
# between go to the debug log. A device that refuses for good would otherwise
# leave the same warning every few minutes all day.
FAILED_CYCLE_WARNING_INTERVAL_S = 3600.0
# Pause at the end of a TRV's control call while its state events are still
# ignored, so the device's reports of what was just written land inside that
# window instead of being taken as a change made at the device.
TRV_STATE_SETTLE_S = 3.0

# How long a write channel waits for the device to confirm a command
# before its watchdog releases the in-flight flag and assumes the command
# applied. Shared by the mode, setpoint and calibration watchdogs, so a
# device that never confirms paces its re-assert at this interval on
# every channel.
WRITE_CONFIRM_TIMEOUT_S = 360


def _write_interval_seconds(self: BetterThermostat, trv: Trv, channel: str) -> float:
    """Minimum spacing between non-safety writes to this TRV on ``channel``.

    The first setpoint write after the user changed the room's target or
    mode goes out at the normal pace on a HomematicIP head too: the user
    expects the head to follow within the normal interval, and a flurry of
    changes still coalesces on it. That write consumes the exemption.
    """
    if not advanced_flag(trv.advanced, CONF_HOMEMATICIP):
        return MIN_WRITE_INTERVAL_S
    user_change = self.last_user_change_monotonic
    last_write = trv.last_write_monotonic
    if (
        channel == "setpoint"
        and user_change is not None
        and (last_write is None or last_write < user_change)
    ):
        return MIN_WRITE_INTERVAL_S
    return HOMEMATICIP_MIN_WRITE_INTERVAL_S


def _budget_open(
    last_write: float | None, now_monotonic: float, interval_seconds: float
) -> bool:
    """Whether a channel's write-budget slot is free again."""
    return last_write is None or now_monotonic - last_write >= interval_seconds


# Per-channel write-budget stamp fields on the Trv.
_BUDGET_STAMPS = {
    "setpoint": "last_write_monotonic",
    "offset": "last_offset_write_monotonic",
    "valve": "last_valve_write_monotonic",
}


def _consume_budget(
    self: BetterThermostat, entity_id: str, channel: str, *, bypass: bool = False
) -> bool:
    """Occupy one channel's write-budget slot, or defer the write.

    Returns True when the write may proceed; the slot is stamped — also
    for bypassing (safety-relevant) writes, so the spacing stays
    accurate. Returns False when the budget defers, after logging it.
    """
    trv = self.real_trvs[entity_id]
    stamp_attr = _BUDGET_STAMPS[channel]
    now = self.clock.monotonic()
    last = getattr(trv, stamp_attr)
    if not bypass and not _budget_open(
        last, now, _write_interval_seconds(self, trv, channel)
    ):
        _LOGGER.debug(
            "better_thermostat %s: write budget defers %s write to %s "
            "(%.0fs since last write)",
            self.device_name,
            channel,
            entity_id,
            now - last,
        )
        return False
    setattr(trv, stamp_attr, now)
    return True


def _budget_remaining(self: BetterThermostat, entity_id: str, channel: str) -> float:
    """Seconds until a channel's write-budget slot reopens."""
    trv = self.real_trvs[entity_id]
    last = getattr(trv, _BUDGET_STAMPS[channel])
    if last is None:
        # Never written on this channel, so the slot is already open.
        # Subtracting a monotonic clock from zero would yield a large
        # negative interval instead.
        return 0.0
    return _write_interval_seconds(self, trv, channel) - (self.clock.monotonic() - last)


def _no_off_system_mode(trv: Trv) -> bool:
    """Whether this TRV cannot be switched off.

    Such devices receive their min temperature in place of OFF and keep
    reporting a heating mode, by design. Answered by the capability
    descriptor, not by re-deriving from raw fields.
    """
    return not trv.capabilities().supports_off_mode


def _schedule_budget_retry(
    self: BetterThermostat, entity_id: str, retry_in_seconds: float
) -> None:
    """Queue one control cycle for when the write budget reopens.

    A deferred setpoint write needs this follow-up: the reconciler
    compares the device against the last value actually written — which
    the device still matches — and configurations without a calibration
    tick have no other periodic trigger.

    A retry already due no later than this one covers it. A retry due
    later, such as one waiting out a HomematicIP head's interval when a
    user change has since shortened it, is cancelled and replaced.
    """
    trv = self.real_trvs[entity_id]
    delay = max(retry_in_seconds, 0.0)
    due_at = self.clock.monotonic() + delay
    if trv.budget_retry_due_at is not None and trv.budget_retry_due_at <= due_at:
        return
    if trv.budget_retry_task is not None:
        trv.budget_retry_task.cancel()
    trv.budget_retry_due_at = due_at

    async def _retry() -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            # A replacement retry owns the bookkeeping from here on.
            if trv.budget_retry_due_at == due_at:
                trv.budget_retry_due_at = None
                trv.budget_retry_task = None
        request_control_cycle(self)

    trv.budget_retry_task = self.task_manager.create_task(
        _retry(), name=f"bt_budget_retry_{entity_id}"
    )


def _schedule_reachability_retry(self: BetterThermostat, entity_id: str) -> None:
    """Queue one control cycle for an offline TRV's next retry window.

    Consumes the reachability region's ``retry_at``: the cycle re-reads
    the device's state without actively probing it. If the TRV is
    reachable when the cycle runs, normal control resumes and may write
    to it. If it stays offline, the kernel does not address it outside
    boost heating, and the region's step advances the exponential
    backoff. Availability events still trigger an immediate cycle when
    the device returns by itself.

    The wait runs on Home Assistant's timer rather than a sleep, so it
    follows Home Assistant's clock; the task around it is what the entity
    cancels when it is removed.
    """
    region = self.kernel_state.reachability.get(entity_id)
    if region is None or region.online or region.retry_at is None:
        return
    trv = self.real_trvs[entity_id]
    if trv.reachability_retry_pending:
        return
    trv.reachability_retry_pending = True
    delay = max(region.retry_at - self.clock.monotonic(), 0.0)

    async def _retry() -> None:
        due: asyncio.Future[None] = self.hass.loop.create_future()

        @callback
        def _due(_now: datetime) -> None:
            if not due.done():
                due.set_result(None)

        cancel_timer = async_call_later(self.hass, delay, _due)
        try:
            await due
        finally:
            cancel_timer()
            trv.reachability_retry_pending = False
        request_control_cycle(self)

    self.task_manager.create_task(_retry(), name=f"bt_reachability_retry_{entity_id}")


def _stamp_heartbeat(self: BetterThermostat) -> None:
    """Record that a control cycle ran to a deliberate decision.

    Skipping an unavailable TRV or deferring a write to the budget is
    such a decision; error paths that bail out without one deliberately
    leave the stamp alone so the watchdog can detect a silent hang.
    """
    self.kernel_state = replace(
        self.kernel_state, last_control_monotonic=self.clock.monotonic()
    )


def _get_valve_control(
    self: BetterThermostat,
    snapshot: WorldSnapshot,
    entity_id: str,
    calibration_mode: CalibrationMode | None,
    calibration_output: CalibrationOutput | None,
) -> tuple[ValveCommand | None, str | None]:
    """Determine valve control settings based on boost mode or calibration.

    Returns a tuple of (valve_command, source_name).
    Returns (None, None) if no valve control should be applied.
    """
    # Forcing the valve on a non-direct-valve TRV bypasses the calibration chain
    # and leaves the valve stuck open after boost ends.
    if (
        is_boost_heating(snapshot)
        and calibration_output == CalibrationOutput.DIRECT_VALVE_BASED
    ):
        _trv = self.real_trvs.get(entity_id)
        max_opening = _trv.valve_max_opening if _trv is not None else 100
        if isinstance(max_opening, (int, float)):
            target_percent = clamp_valve_percent(max_opening)
        else:
            target_percent = 100
        return {"valve_percent": target_percent, "apply_valve": True}, "boost_mode"

    # Check calibration-based valve control
    if calibration_output != CalibrationOutput.DIRECT_VALVE_BASED:
        return None, None

    # Try calibration balance from various calibration modes
    cal_bal = self.real_trvs[entity_id].calibration_balance
    if (
        isinstance(cal_bal, dict)
        and cal_bal.get("apply_valve")
        and cal_bal.get("valve_percent") is not None
    ):
        source_map: dict[CalibrationMode, str] = {
            CalibrationMode.MPC_CALIBRATION: "mpc_calibration",
            CalibrationMode.MPC_V2_CALIBRATION: "mpc_v2_calibration",
            CalibrationMode.TPI_CALIBRATION: "tpi_calibration",
            CalibrationMode.PID_CALIBRATION: "pid_calibration",
            CalibrationMode.HEATING_POWER_CALIBRATION: "heating_power_calibration",
        }
        source = (
            source_map.get(calibration_mode) if calibration_mode is not None else None
        )
        if source:
            return cal_bal, source

    return None, None


def compute_control_cycle(
    self: BetterThermostat, *, record: bool = True, commit: bool = True
) -> tuple[WorldSnapshot, DesiredState]:
    """Build one consistent observation and decision for a control cycle.

    Records the (snapshot, pre-decide state, desired) tuple in the
    flight recorder — exactly once per cycle. decide() treats its input
    state as immutable; the recorder copies what it stores. Probes (the
    reconciler) pass ``record=False`` to run the same observe-decide
    step without filling the recorder ring, and ``commit=False`` to
    leave the kernel regions (e.g. reachability retry counters)
    untouched — a probe is not a real cycle.
    """
    snapshot = build_snapshot(self)
    pre_state = self.kernel_state
    desired, post_state = decide(snapshot, pre_state)
    if commit:
        self.kernel_state = post_state
    if record:
        self.flight_recorder.record(snapshot, pre_state, desired)
    return snapshot, desired


def _reconcile_tolerance(self: BetterThermostat, state: State) -> float:
    """Per-device tolerance for the commanded-vs-reported comparison.

    Devices snap a written setpoint onto their own reported grid; a
    snapped value sits at most half a step away from the commanded one.
    The base tolerance covers devices that report no usable step.
    """
    step = convert_to_float(
        str(state.attributes.get("target_temp_step")), self.device_name, "reconcile()"
    )
    if step is None or step <= 0:
        return RECONCILE_TOLERANCE_K
    unit = state_temperature_unit(
        state.attributes, self.hass.config.units.temperature_unit
    )
    # A Kelvin interval equals a Celsius one, so only Fahrenheit scales.
    if unit == UnitOfTemperature.FAHRENHEIT:
        step = step * 5.0 / 9.0
    # Slack against float noise when the difference is exactly half a step.
    return max(RECONCILE_TOLERANCE_K, step / 2.0 + 1e-6)


def _calibration_match_tolerance(self: BetterThermostat, entity_id: str) -> float:
    """Per-device tolerance for the commanded-vs-reported offset comparison.

    A written offset travels to the device as a count of its declared
    step, and the ZHA number platform truncates that count toward zero
    (``int(value / step)``). Float division lands just short of the
    whole number, so a value written as 6.3 arrives as
    ``int(6.3 / 0.1) == 62`` counts and the device reports 6.2: the
    truncated count lands one step nearer zero than the command, in
    either sign. A device whose own grid is one declared step coarser
    lands there too. A report within one step of the command is
    therefore the command or its truncated neighbour, and a report
    further away is a lost write.
    OFFSET_MATCH_TOLERANCE_K is the floor: it covers devices that report
    no usable step and those whose declared step is finer than the grid
    they actually report on.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity ID of the TRV whose offset step decides the tolerance

    Returns
    -------
    float
        Tolerance in Kelvin for an offset comparison
    """
    step = convert_to_float(
        str(self.real_trvs[entity_id].local_calibration_step),
        self.device_name,
        "controlling()",
    )
    if step is None or step <= 0:
        return OFFSET_MATCH_TOLERANCE_K
    # Slack against float noise when the difference is exactly one step.
    return max(OFFSET_MATCH_TOLERANCE_K, step + 1e-6)


def _offset_diverges(self: BetterThermostat, trv: Trv) -> bool:
    """Whether the device's calibration offset left the commanded value.

    Compared only once the device has confirmed the last write — an
    in-flight write is the write path's business, not the reconciler's.
    The comparison shares its tolerance with that write path, so what
    the reconciler calls a divergence is what the gate re-asserts.
    """
    if not trv.capabilities().supports_offset_write:
        return False
    if trv.local_temperature_calibration_entity is None:
        # Service-call ecosystems have no readable calibration entity;
        # divergence is only verifiable through one.
        return False
    if trv.last_calibration is None or trv.calibration_received is not True:
        return False
    state = self.hass.states.get(trv.local_temperature_calibration_entity)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return False
    reported = convert_to_float(state.state, self.device_name, "reconcile()")
    if reported is None:
        return False
    return abs(float(trv.last_calibration) - reported) > _calibration_match_tolerance(
        self, trv.entity_id
    )


def _valve_diverges(self: BetterThermostat, trv: Trv) -> bool:
    """Whether the valve-position entity left the commanded percentage.

    Only the adapter-written number entity is verifiable; quirk-driven
    valve writes have no readable target.
    """
    if not trv.capabilities().supports_valve_write:
        return False
    if not (trv.valve_position_entity and trv.valve_position_writable is True):
        # Quirk-driven valve writes have no readable target to verify.
        return False
    if trv.last_valve_percent is None:
        return False
    state = self.hass.states.get(trv.valve_position_entity)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return False
    reported = convert_to_float(state.state, self.device_name, "reconcile()")
    if reported is None:
        return False
    return abs(float(trv.last_valve_percent) - reported) > RECONCILE_VALVE_TOLERANCE_PCT


def _valve_at_target(
    self: BetterThermostat, entity_id: str, target_percent: float
) -> bool:
    """Whether the valve channel already matches the intent.

    True when the last commanded percentage equals the (int-rounded)
    target and the readable position entity, if any, has not diverged
    from it — no difference, no network write.
    """
    trv = self.real_trvs[entity_id]
    if trv.last_valve_percent is None:
        return False
    if round(float(trv.last_valve_percent)) != round(float(target_percent)):
        return False
    return not _valve_diverges(self, trv)


def desired_diverges(
    self: BetterThermostat, snapshot: WorldSnapshot, desired: DesiredState
) -> bool:
    """Whether any TRV's reported state diverges from the clamped intent.

    Compares the commanded setpoint with the device-reported target and
    the intended mode with the device-reported mode; a lost write shows
    up here and the next control cycle re-sends it.
    """
    for entity_id, intent in desired.trvs.items():
        trv = self.real_trvs.get(entity_id)
        if trv is None:
            continue
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        if cooling_owns_dual_role_device(self, entity_id):
            # The cooling channel drives this device, so the mode and setpoint
            # it reports are that channel's write and not a heating write that
            # went missing. The heating intent it would be compared against is
            # one no control cycle applies while the handover stands, so the
            # divergence would be permanent and the tick would queue a cycle
            # for the whole length of every cooling period.
            continue

        if intent.hvac_mode is not None:
            if intent.hvac_mode == HVACMode.OFF:
                # A device that cannot switch off converges on its min
                # temperature instead; the setpoint comparison below covers it.
                if not _no_off_system_mode(trv) and state.state not in (
                    HVACMode.OFF,
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                ):
                    return True
            elif state.state == HVACMode.OFF:
                return True

        reported_target = attr_to_celsius(
            self, state, "temperature", None, "reconcile()"
        )
        commanded = trv.commanded_setpoint
        if (
            commanded is not None
            and reported_target is not None
            and abs(float(commanded) - float(reported_target))
            > _reconcile_tolerance(self, state)
        ):
            return True

        if _offset_diverges(self, trv) or _valve_diverges(self, trv):
            return True
    return False


async def reconcile_tick(self: BetterThermostat, now: datetime | None = None) -> None:
    """Periodic reconciliation: re-converge devices onto the intent.

    Builds a snapshot, asks the kernel for the desired state, and
    enqueues one control cycle when any device diverges — the general
    mechanism that heals lost writes without per-case keepalives.

    The control watchdog is read here too. A divergence behind a loop that
    has not completed a cycle for ``WATCHDOG_MAX_AGE_S`` is the silent hang
    it exists for and is logged as an error. A room whose devices hold the
    intent has nothing for a cycle to do, however long ago the last one
    ran, so a quiet loop is not reported.
    """
    if self.startup_running or self.ignore_states:
        return
    if self.kernel_state.maintenance.is_blocking(self.clock.monotonic()):
        return
    try:
        snapshot, desired = compute_control_cycle(self, record=False, commit=False)
        desired = safety_clamp(desired, snapshot)
        if not desired_diverges(self, snapshot, desired):
            return
        if control_loop_stalled(
            self.kernel_state.last_control_monotonic, self.clock.monotonic()
        ):
            _LOGGER.error(
                "better_thermostat %s: control watchdog: device state diverged "
                "and no control cycle for more than %.0f minutes, forcing one",
                self.device_name,
                WATCHDOG_MAX_AGE_S / 60.0,
            )
        else:
            _LOGGER.debug(
                "better_thermostat %s: reconcile: device state diverged, "
                "queueing a control cycle",
                self.device_name,
            )
        request_control_cycle(self)
    except Exception:
        _LOGGER.exception(
            "better_thermostat %s: reconcile tick failed", self.device_name
        )


def _through_safety_hull(
    snapshot: WorldSnapshot,
    entity_id: str,
    *,
    setpoint: float | None = None,
    valve_percent: float | None = None,
    calibration_offset: float | None = None,
) -> TrvDesired:
    """Run one intent through the safety hull at the command boundary."""
    desired = DesiredState(
        trvs={
            entity_id: TrvDesired(
                entity_id=entity_id,
                setpoint=setpoint,
                valve_percent=valve_percent,
                calibration_offset=calibration_offset,
            )
        }
    )
    return safety_clamp(desired, snapshot).trvs[entity_id]


class TaskManager:
    """Manages background asyncio tasks with automatic cleanup.

    Tracks created tasks and automatically removes them from the set when they complete.
    The entity cancels what is still running when it is removed; from then on
    the manager starts nothing.
    """

    def __init__(self, hass: HomeAssistant | None = None) -> None:
        """Initialize the task manager with an empty task set."""
        self.tasks: set[asyncio.Task[object]] = set()
        self.hass = hass
        self.closed = False

    def create_task[T](
        self, coro: Coroutine[Any, Any, T], name: str | None = None
    ) -> asyncio.Task[T] | None:
        """Create and track an asyncio task with automatic cleanup on completion.

        Parameters
        ----------
        coro : Coroutine
            The coroutine to execute as a task
        name : str, optional
            A descriptive name for the background task

        Returns
        -------
        asyncio.Task or None
            The created task, or None once the manager is closed
        """
        if self.closed:
            coro.close()
            return None
        if self.hass is not None:
            task = self.hass.async_create_background_task(
                coro, name=name or "bt_task_manager_task"
            )
        else:
            task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def cancel_all(self) -> list[asyncio.Task[object]]:
        """Cancel every tracked task and close the manager to new ones.

        Returns
        -------
        list[asyncio.Task]
            The cancelled tasks, for the caller to await
        """
        self.closed = True
        tasks = list(self.tasks)
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        return tasks


@dataclass
class _FailedCycleRun:
    """Consecutive control cycles that failed while the user's targets stood.

    ``intent`` is the room targets the user had set at the time and the whole
    of what tells one run from the next. ``failing`` holds the TRVs that
    failed during the run and ``reported`` the failures already logged with
    their traceback, as the TRV and the kind of error,
    ``wait_seconds`` the pause the run has reached, ``started_at`` when its first
    failure happened, ``warned_at`` when a failure at the ceiling was last
    reported as a warning and ``retry`` the pending re-queue.

    A TRV that starts failing during a run joins it and inherits its pause,
    up to the ceiling, for the retry of its own write. The five-minute
    reconcile tick queues a cycle for a device that does not hold what it
    was sent anyway, so that write is not held back any longer than the
    periodic ticks already space it.
    """

    intent: tuple[object, ...]
    failing: frozenset[str]
    reported: frozenset[tuple[str, str]]
    count: int
    wait_seconds: float
    started_at: float
    warned_at: float | None = None
    retry: asyncio.Task[None] | None = None


def _user_intent(self: BetterThermostat) -> tuple[object, ...]:
    """Return the room targets a user sets, as the failure pacing compares them."""
    return (
        self.heat_target_temperature,
        self.cool_target_temperature,
        self.bt_hvac_mode,
    )


async def _requeue_failed_cycle(self: BetterThermostat, delay_seconds: float) -> None:
    """Request a control cycle once a failed cycle's pause has passed."""
    await asyncio.sleep(delay_seconds)
    request_control_cycle(self)


def _controlled_cleanly(
    self: BetterThermostat,
    entity_id: str,
    dispatched: list[str],
    desired: DesiredState | None,
) -> bool:
    """Return whether a clean cycle controlled this TRV rather than skipping it.

    A cycle leaves out a TRV the cooling channel drives and skips one its
    decision did not address, and either way reports nothing wrong without
    having written to it. The decision addresses a TRV the snapshot reads as
    available, which includes one whose quirk runs it while it reports
    unknown, and during a heating boost every TRV. Without a shared decision
    each TRV decided on a snapshot of its own, and the TRV counts as
    controlled when its state reads as available.
    """
    if entity_id not in dispatched:
        return False
    state = self.hass.states.get(entity_id)
    if state is None:
        return False
    if desired is not None:
        return entity_id in desired.trvs
    return not trv_report_is_unreadable(self, entity_id, state)


def _pace_failed_cycle(
    self: BetterThermostat,
    run: _FailedCycleRun | None,
    failures: list[tuple[str, BaseException | bool]],
    dispatched: list[str],
    desired: DesiredState | None,
) -> _FailedCycleRun | None:
    """Report a cycle's failures and schedule its retry; return the run.

    A cycle that fails while the user's targets stand continues the run and
    doubles the pause, up to its ceiling, whichever TRV failed and whatever
    its error said: a refusal that numbers its messages, a payload the
    calibration moves between cycles and devices failing in turn are all
    still the same run. A failure after the user set new targets starts a
    run at the base pause. The retry is a task of its own, so the queue goes
    on serving requests while it waits: a new target reaches the devices at
    once. Each TRV and kind of error is logged with its traceback the first
    time it fails in a run, and in a single line after that.

    A clean cycle ends the run once the retry has fired and it controlled
    every TRV that failed in the run, or once the user has set new targets.
    A clean cycle before the retry fired, one the write budget deferred for
    instance, never tried the refused write again and keeps the run, and so
    does one whose decision left a failing TRV unaddressed or left it to the
    cooling channel. ``desired`` is the decision the cycle ran on, None when
    the cycle had no shared one.
    """
    if not failures:
        if run is None:
            return None
        if run.intent == _user_intent(self) and (
            (run.retry is not None and not run.retry.done())
            or not all(
                _controlled_cleanly(self, entity_id, dispatched, desired)
                for entity_id in run.failing
            )
        ):
            return run
        if run.retry is not None:
            run.retry.cancel()
        return None

    intent = _user_intent(self)
    now = self.clock.monotonic()
    if run is not None and run.intent == intent:
        count = run.count + 1
        wait_seconds = min(run.wait_seconds * 2, FAILED_CYCLE_BACKOFF_MAX_S)
        reported = run.reported
        failing = run.failing | {entity_id for entity_id, _ in failures}
        started_at = run.started_at
        warned_at = run.warned_at
    else:
        count = 1
        wait_seconds = FAILED_CYCLE_BACKOFF_S
        reported = frozenset()
        failing = frozenset(entity_id for entity_id, _ in failures)
        started_at = now
        warned_at = None
    # At the ceiling the run is reported hourly; below it every failure is.
    at_ceiling = wait_seconds >= FAILED_CYCLE_BACKOFF_MAX_S
    warn_again = not at_ceiling or (
        warned_at is None or now - warned_at >= FAILED_CYCLE_WARNING_INTERVAL_S
    )
    if run is not None and run.retry is not None:
        run.retry.cancel()

    # A retry inside the setpoint's write-budget window writes nothing, so
    # it is not due before the refused setpoint could go out again.
    delay_seconds = max(
        [wait_seconds]
        + [_budget_remaining(self, entity_id, "setpoint") for entity_id, _ in failures]
    )
    for entity_id, outcome in failures:
        if not isinstance(outcome, BaseException):
            continue
        kind = (entity_id, type(outcome).__name__)
        if kind not in reported:
            reported = reported | {kind}
            _LOGGER.error(
                "better_thermostat %s: ERROR controlling TRV %s: %s",
                self.device_name,
                entity_id,
                outcome,
                exc_info=outcome,
            )
        elif not at_ceiling:
            _LOGGER.warning(
                "better_thermostat %s: controlling TRV %s failed again (%d cycles "
                "in a row): %s; retrying in %.0f s",
                self.device_name,
                entity_id,
                count,
                outcome,
                delay_seconds,
            )
        else:
            _LOGGER.log(
                logging.WARNING if warn_again else logging.DEBUG,
                "better_thermostat %s: controlling TRV %s still failing after %d "
                "cycles in %.0f min: %s; retrying every %.0f s",
                self.device_name,
                entity_id,
                count,
                (now - started_at) / 60.0,
                outcome,
                delay_seconds,
            )
    if at_ceiling and warn_again:
        warned_at = now
    retry = asyncio.create_task(
        _requeue_failed_cycle(self, delay_seconds),
        name=f"bt_failed_cycle_retry_{self.device_name}",
    )
    return _FailedCycleRun(
        intent, failing, reported, count, wait_seconds, started_at, warned_at, retry
    )


def advance_hvac_action(self: BetterThermostat) -> None:
    """Recompute the heating action and commit its hysteresis state.

    The heating action and the hysteresis band behind it advance once per
    control cycle. Every dispatched device advances them, and a cycle that
    dispatches none advances them itself, so the band never rests on the state
    an earlier cycle left it in. The recompute is non-critical: a cycle that
    cannot take it goes on to the device writes rather than failing, so the
    traceback is the only trace a band that stops advancing leaves behind.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    """
    try:
        self.old_attr_hvac_action = self.attr_hvac_action
        result = self._compute_hvac_action_pure()
        self._commit_hvac_action(result)
        self.attr_hvac_action = result.action
    except Exception:
        _LOGGER.debug(
            "better_thermostat %s: hvac action recompute failed (non critical)",
            self.device_name,
            exc_info=True,
        )


def refresh_cached_trv_modes(self: BetterThermostat) -> None:
    """Settle every TRV's mode cache on what the handler has yet to read.

    ``Trv.hvac_mode`` holds the raw state string a device publishes, and the
    inbound event handler is its only other writer. That handler stands down
    for the whole length of a control cycle, and a cycle runs for seconds
    while the adapters wait for their writes to be confirmed. The caller runs
    this at the end of the cycle, before it releases ``ignore_states``.

    A device reporting the mode Better Thermostat last commanded it into is
    cached as holding it. A device reporting any other mode was changed at
    the device while nobody listened, or has not yet taken the command. The
    handler tells the two apart, and it only looks at a report whose mode
    differs from the cache, so the cache is set to the commanded mode: the
    device's next report then reaches the handler as the change it is, and
    the handler's guards decide whether the room follows it. Caching the
    reported mode instead would make that report read as no change, and the
    next cycle would drive the device back out of a mode the user chose.

    The mode of the Better Thermostat entity does not move here: a mode
    reported while the handler was standing down is read as user intent on
    the device's next report, not from a report nobody read.

    A device that says nothing, one that is unavailable or unknown, and one
    under a child lock keep the cache they have, which is how the event
    handler reads all three.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    """
    for entity_id, trv in self.real_trvs.items():
        state = self.hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES + UNKNOWN_STATES:
            continue
        if advanced_flag(trv.advanced, CONF_CHILD_LOCK):
            continue
        _settled_mode = state.state
        if trv.last_hvac_mode is not None and state.state != trv.last_hvac_mode:
            _settled_mode = trv.last_hvac_mode
        if trv.hvac_mode == _settled_mode:
            continue
        _LOGGER.debug(
            "better_thermostat %s: TRV %s reports %s while its cached mode is "
            "%s, caching %s",
            self.device_name,
            entity_id,
            state.state,
            trv.hvac_mode,
            _settled_mode,
        )
        trv.hvac_mode = _settled_mode


async def read_reports_held_during_cycle(self: BetterThermostat) -> None:
    """Read what every TRV reported while the cycle held the handler off.

    A setpoint turned at the device inside a cycle is otherwise read on the
    device's next report, and a cycle that starts before that report, such
    as the reconciler's, writes over the turn before anyone has read it. The
    caller runs this right after it releases ``ignore_states`` and before it
    takes the next cycle, so each TRV that reported meanwhile has its current
    state read by the inbound handler as a report of its own. That report
    replaces the state the first held report replaced, so a device that came
    back from ``unavailable`` inside the cycle is read as a return, the way
    the handler reads it outside a cycle, and not as a setpoint turned at the
    device.

    The mode such a state carries is read the same way, unless a mode
    command to the device is still unconfirmed. The handler declines a mode
    then, and reading it would take it into the cache and let the report
    that could adopt it once the command is settled pass as no change; it is
    left to the device's next report, the way ``refresh_cached_trv_modes``
    settled it. A device that is unavailable has nothing to read, and so has
    one reporting ``unknown`` unless its model reads that as operating, the
    way the handler reads it.

    The setpoint such a state carries is judged against the mode the device
    was cached in before the held report, not the mode
    ``refresh_cached_trv_modes`` settled since, so a head switched on inside
    the cycle does not bring a setpoint turned while it was off, as it does
    not outside a cycle.

    A control cycle is requested only when the report moved what the next
    cycle acts on: the room's targets or mode, the setpoint or mode the
    device is known to hold, or the internal temperature it reported while
    the cycle ran. A turn the room adopts at a target it already had moves
    only the setpoint the device holds, and the cycle is what writes the
    device's own share of that target back over the turn.
    The handler takes that reading as it arrives, as it does outside a cycle,
    unless it came too soon after the previous one; such a reading is taken
    here once that interval has passed, and asks for a cycle all the same. A
    head switched on inside the cycle asks for one as well, as its mode
    change does outside a cycle: the cache already holds the commanded mode,
    so the report moves nothing, yet the setpoint it was not adopted for
    has to be driven back to the room target. A device answering inside
    every cycle with a report that carries nothing new would otherwise keep
    one cycle following the next.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    """
    for entity_id, trv in list(self.real_trvs.items()):
        if not trv.report_unread:
            continue
        trv.report_unread = False
        previous = trv.state_before_held_report
        trv.state_before_held_report = None
        prior_hvac_mode = trv.hvac_mode_before_held_report
        trv.hvac_mode_before_held_report = None
        temperature_moved = trv.temperature_moved_while_held
        trv.temperature_moved_while_held = False
        state = self.hass.states.get(entity_id)
        if trv_report_is_unreadable(self, entity_id, state):
            continue
        held_report = Event(
            EVENT_STATE_CHANGED,
            EventStateChangedData(
                entity_id=entity_id, old_state=previous, new_state=state
            ),
            context=Context(),
        )
        acted_on_before = _held_report_control_inputs(self, trv)
        try:
            await trigger_trv_change(
                self,
                held_report,
                mode_settled=trv.system_mode_received is False,
                request_cycle=False,
                prior_hvac_mode=prior_hvac_mode,
            )
        except Exception:
            _LOGGER.exception(
                "better_thermostat %s: reading the report TRV %s sent during the "
                "cycle failed",
                self.device_name,
                entity_id,
            )
            continue
        switched_on = (
            prior_hvac_mode == HVACMode.OFF
            and state is not None
            and state.state != HVACMode.OFF
        )
        if (
            temperature_moved
            or switched_on
            or _held_report_control_inputs(self, trv) != acted_on_before
            or _locked_device_moved(self, entity_id, trv, state)
        ):
            request_control_cycle(self)


def _locked_device_moved(
    self: BetterThermostat, entity_id: str, trv: Trv, state: State | None
) -> bool:
    """Return whether a child-locked TRV holds a setpoint or mode it was not sent.

    The lock keeps a press at the device from being adopted, so it moves no
    control input, and the cycle that turns the device back has to be asked
    for by what the device holds. A mode command still waiting for its
    confirmation is left to its watchdog, since the report can lag it; a
    setpoint report that lags a write shows a value the device was sent
    before.

    The mode is held against the command of the channel that drives the
    device: while the cooling channel owns a device that carries both roles,
    that is the mode the cooler last sent, not the heating channel's. A device
    reporting ``unknown`` names no mode, which is how a model that reads
    ``unknown`` as operating reports, so only its setpoint is compared.
    """
    if state is None or not advanced_flag(trv.advanced, CONF_CHILD_LOCK):
        return False
    if state.state != STATE_UNKNOWN:
        if cooling_owns_dual_role_device(self, entity_id):
            commanded_mode = cooler_send_cache(self).get("hvac_mode", (None, None))[0]
        elif trv.system_mode_received:
            commanded_mode = trv.last_hvac_mode
        else:
            commanded_mode = None
        if commanded_mode is not None and state.state != commanded_mode:
            return True
    reported = read_setpoint_celsius(
        self, state, TRV_SETPOINT_KEYS, "read_reports_held_during_cycle()"
    )
    step = normalize_step(trv.target_temp_step or self.bt_target_temperature_step)
    known = [
        trv.commanded_setpoint,
        trv.confirmed_setpoint,
        *trv.echo_setpoint_values(),
    ]
    if entity_id == dual_role_entity_id(self):
        # The cooling channel's writes as the device holds them, on the grid
        # the cooling channel sends on, the way the inbound handler compares
        # them.
        known += cooling_writes_as_held(self, state)
    known_values = [value for value in known if value is not None]
    if reported is None or not known_values:
        return False
    window = setpoint_echo_window(step)
    return all(abs(reported - value) >= window for value in known_values)


def _held_report_control_inputs(self: BetterThermostat, trv: Trv) -> tuple[object, ...]:
    """Return what a report read at cycle end can move that a cycle acts on."""
    return (
        self.heat_target_temperature,
        self.cool_target_temperature,
        self.bt_hvac_mode,
        trv.hvac_mode,
        trv.confirmed_setpoint,
        last_sent_cooler_temperature(self),
        trv.current_temperature,
    )


async def control_queue(self: BetterThermostat) -> None:
    """Process control commands from the queue and coordinate TRV control.

    This async task runs continuously, processing control requests from the
    control_queue_task queue. It calculates heating power once per cycle,
    then controls all TRVs in parallel using asyncio.gather(). Cooler control
    is executed separately if a cooler entity is configured.

    The queue pauses during maintenance mode or when ignore_states is True.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance

    Returns
    -------
    None
        This function runs indefinitely in an asyncio task
    """
    if not hasattr(self, "task_manager"):
        self.task_manager = TaskManager(hass=self.hass)

    failed_run: _FailedCycleRun | None = None
    try:
        while True:
            if self.in_maintenance:
                await asyncio.sleep(1)
                continue

            if self.ignore_states or self.startup_running:
                await asyncio.sleep(1)
                continue
            else:
                controls_to_process = await self.control_queue_task.get()
                try:
                    if controls_to_process is not None:
                        self.ignore_states = True

                        # Calculate heating power once per cycle
                        try:
                            await self.calculate_heating_power()
                        except Exception:
                            _LOGGER.exception(
                                "better_thermostat %s: ERROR calculating heating power",
                                self.device_name,
                            )

                        # Calculate heat loss once per cycle (idle cooling)
                        try:
                            await self.calculate_heat_loss()
                        except Exception:
                            _LOGGER.exception(
                                "better_thermostat %s: ERROR calculating heat loss",
                                self.device_name,
                            )

                        # One observation and decision for the whole cycle;
                        # on failure each TRV falls back to its own cycle.
                        cycle = None
                        try:
                            cycle = compute_control_cycle(self)
                        except Exception:
                            _LOGGER.exception(
                                "better_thermostat %s: ERROR computing control cycle",
                                self.device_name,
                            )

                        # Handle cooler logic once per cycle, on the same
                        # observation the TRVs are controlled with. A cooler
                        # that is also a TRV still awaiting its initialisation
                        # joins the cycles only once that is done, like the
                        # heating channel.
                        _cooler_pass_completed = False
                        _cooler_trv = (
                            self.real_trvs.get(self.cooler_entity_id)
                            if self.cooler_entity_id is not None
                            else None
                        )
                        if self.cooler_entity_id is not None and not (
                            _cooler_trv is not None
                            and _cooler_trv.awaiting_initialization
                        ):
                            try:
                                await control_cooler(
                                    self, cycle[0] if cycle is not None else None
                                )
                            except Exception:
                                _LOGGER.exception(
                                    "better_thermostat %s: ERROR controlling cooler",
                                    self.device_name,
                                )
                            else:
                                _cooler_pass_completed = True

                        # Create tasks for all TRVs to run in parallel. A device
                        # that carries both roles takes one mode and one setpoint,
                        # so the heating channel stands down for the cycles the
                        # cooling channel drives it. A cooling pass that raised left
                        # no decision to read, and the permissive default is the
                        # heating channel keeping the device.
                        _shared_entity_id = dual_role_entity_id(self)
                        _cooling_owns_shared = (
                            _shared_entity_id is not None
                            and _cooler_pass_completed
                            and cooling_owns_dual_role_device(self, _shared_entity_id)
                        )
                        tasks = []
                        controlled_trvs = []
                        for trv in self.real_trvs.keys():
                            if _cooling_owns_shared and trv == _shared_entity_id:
                                _LOGGER.debug(
                                    "better_thermostat %s: %s is driven by the cooling "
                                    "channel this cycle, leaving the heating channel out",
                                    self.device_name,
                                    trv,
                                )
                                continue
                            controlled_trvs.append(trv)
                            tasks.append(control_trv(self, trv, cycle=cycle))

                        if _cooling_owns_shared and not tasks:
                            # The heating action and its hysteresis are advanced
                            # inside control_trv, so a cycle whose only device went
                            # to the cooling channel advances them here instead of
                            # leaving the band on the state the previous cycle left.
                            advance_hvac_action(self)
                            # Leaving the room's only device to the cooling channel
                            # is a deliberate decision and the cycle reached it, so
                            # it counts as one: without the stamp the control
                            # watchdog would read every cooling run longer than its
                            # window as a hung loop.
                            _stamp_heartbeat(self)

                        # Run all TRV controls in parallel
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        failures: list[tuple[str, BaseException | bool]] = [
                            (controlled_trvs[i], res)
                            for i, res in enumerate(results)
                            if isinstance(res, BaseException) or res is False
                        ]

                        # Retry the cycle if some TRVs failed; the retry
                        # coalesces with any already-pending request. The
                        # backoff sits here rather than in the failing worker:
                        # a worker holds the TRV lock and would stall the rest
                        # of the cycle with it.
                        failed_run = _pace_failed_cycle(
                            self,
                            failed_run,
                            failures,
                            controlled_trvs,
                            cycle[1] if cycle is not None else None,
                        )

                        announce_learned_state(self.hass, resolve_unique_id(self))

                        if not self.in_maintenance:
                            # The inbound handler stood down for the whole
                            # cycle, so a mode a device reported meanwhile
                            # never reached it. Settle the caches before the
                            # window closes, so the device's next report is
                            # read as the change it carries.
                            refresh_cached_trv_modes(self)
                            self.ignore_states = False
                            await read_reports_held_during_cycle(self)

                finally:
                    # One acknowledgement per item taken, including an item that
                    # carries no cycle and one whose handling is cancelled. The
                    # queue counts an item as unfinished until it is acknowledged,
                    # and cancellation reaches this loop between the get() and the
                    # end of the work it hands out.
                    self.control_queue_task.task_done()
    except asyncio.CancelledError:
        _LOGGER.debug(
            "better_thermostat %s: control_queue task cancelled, cleaning up",
            self.device_name,
        )
        raise
    finally:
        if failed_run is not None and failed_run.retry is not None:
            failed_run.retry.cancel()
        # Ensure ignore_states is reset on any exit unless maintenance wants it suppressed.
        if not self.in_maintenance:
            self.ignore_states = False


# The cooler channels that keep a failure run in the send cache.
_CoolerChannel = Literal["temperature", "hvac_mode"]


def cooler_low_bound(
    high: float, heat_target_temperature: float | None, lowest: float | None = None
) -> float:
    """Return the lower bound that travels with ``high`` in a range write.

    A range write needs both bounds, and Home Assistant rejects a low bound
    above the high one or outside the cooler's range. The heating target is
    the natural lower bound; it can only exceed the cooling target while the
    two are out of sync, so it is capped at the value being written. The
    heating target is held to the heaters' range, not the cooler's, so it is
    raised onto ``lowest``, the cooler's minimum, where it sits below it.
    """
    low = (
        high
        if heat_target_temperature is None
        else min(float(heat_target_temperature), high)
    )
    if lowest is not None and low < lowest:
        low = min(lowest, high)
    return low


def _cooler_failure_run(
    last_sent: CoolerSendCache, channel: _CoolerChannel
) -> CoolerFailureRun | None:
    """Return a channel's current run of consecutive send failures, if any."""
    if channel == "temperature":
        return last_sent.get("temperature_failed")
    return last_sent.get("hvac_mode_failed")


def _cooler_retry_deferred(
    last_sent: CoolerSendCache,
    channel: _CoolerChannel,
    wanted: CoolerCommand,
    now_monotonic: float,
) -> bool:
    """Whether a channel's backoff still holds a command back.

    A rejected command leaves no send timestamp behind, so the resend
    throttle cannot pace it; this backoff does. The wait grows with the run
    of consecutive failures of that command, so a device that rejects every
    write — a cloud air conditioner over its rate limit, for instance — is
    not retried harder than one that merely lags.

    A command other than the rejected one is a new command rather than a
    retry, and waits the base only: it has to reach the device promptly, but
    it must not hand a channel that is failing a fresh write budget at the
    cycle rate either, which is what a desired value alternating between two
    rejected commands would otherwise do.
    """
    run = _cooler_failure_run(last_sent, channel)
    if run is None:
        return False
    failures, failed_at, failed_wanted = run
    if failed_wanted != wanted:
        wait = COOLER_FAILURE_BACKOFF_BASE_S
    else:
        wait = min(
            COOLER_FAILURE_BACKOFF_BASE_S
            * COOLER_FAILURE_BACKOFF_FACTOR ** (failures - 1),
            COOLER_FAILURE_BACKOFF_MAX_S,
        )
    return (now_monotonic - failed_at) < wait


def _record_cooler_failure(
    last_sent: CoolerSendCache,
    channel: _CoolerChannel,
    wanted: CoolerCommand,
    now_monotonic: float,
) -> None:
    """Extend a channel's run of consecutive failures of one command.

    A run is a run of the same command; a different one that fails starts
    its own, so the run the counter holds and the run the gate prices always
    describe the same command. The count stops at the length the backoff can
    still tell apart.
    """
    previous = _cooler_failure_run(last_sent, channel)
    failures = 0 if previous is None or previous[2] != wanted else previous[0]
    run: CoolerFailureRun = (
        min(failures + 1, COOLER_FAILURE_BACKOFF_MAX_RUN),
        now_monotonic,
        wanted,
    )
    if channel == "temperature":
        last_sent["temperature_failed"] = run
    else:
        last_sent["hvac_mode_failed"] = run


async def control_cooler(
    self: BetterThermostat, snapshot: WorldSnapshot | None = None
) -> None:
    """Control the cooler entity based on current temperature and cooling setpoint.

    Activates cooling when the current temperature reaches the cooling target
    plus tolerance and is above the heating target, so a configured tolerance
    delays the switch-on instead of running the room below the cooling target.
    Deactivates cooling when the temperature falls back below the cooling
    target — or below the cooling target minus the width
    COOLER_MODE_HYSTERESIS_K borrows from underneath it whenever the tolerance
    is narrower than that minimum band — or when BT HVAC mode is OFF, or
    while a window or door contact is open.

    The control queue passes the cycle's snapshot in; a standalone
    invocation observes the world itself. Without a configured cooler there
    is nothing to control.
    """
    cooler_entity_id = self.cooler_entity_id
    if cooler_entity_id is None:
        return
    # Get current cooler state to avoid sending redundant commands
    cooler_state = self.hass.states.get(cooler_entity_id)
    if cooler_state is None or cooler_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s unavailable, skipping",
            self.device_name,
            self.cooler_entity_id,
        )
        return

    current_hvac_mode = cooler_state.state
    # The cooler reports its setpoint in the system unit; resolve it to the
    # Celsius Better Thermostat works in before any comparison. A range-only
    # cooler publishes it under the upper bound instead of "temperature".
    cooler_setpoint = read_setpoint_celsius(
        self, cooler_state, COOLER_SETPOINT_KEYS, "control_cooler()"
    )

    # A cooler that only advertises the range feature rejects a "temperature"
    # payload with a ServiceValidationError, so it never receives a setpoint.
    # Devices that advertise neither bit use the single-setpoint payload. A
    # cooler advertising both publishes the channel it does not drive as None,
    # so the write follows the channel the reading above came from.
    _write_range = supports_temperature_range(cooler_state) and (
        not supports_single_target_temperature(cooler_state)
        or (
            cooler_state.attributes.get("temperature") is None
            and cooler_state.attributes.get("target_temp_high") is not None
        )
    )

    last_sent = cooler_send_cache(self)
    now_monotonic = self.clock.monotonic()

    # Determine desired state based on the world snapshot of this cycle. The
    # cooler holds setpoints on its own grid only, so the command is the
    # cooling target rounded once onto that grid; every comparison below and
    # the send cache work with the value the device is actually sent.
    if snapshot is None:
        snapshot = build_snapshot(self)
    desired_temperature = on_cooler_grid(
        self, cooler_state, snapshot.cool_target_temperature
    )
    # Home Assistant refuses a setpoint outside the cooler's own range, and
    # the cooling target can leave it where a configured bound widens the
    # cooling range past the device's, so the write is held to the device.
    _cooler_min_bound = read_bound_celsius(
        self, cooler_state, ATTR_MIN_TEMP, lower=True, context="control_cooler()"
    )
    _cooler_max = read_bound_celsius(
        self, cooler_state, ATTR_MAX_TEMP, lower=False, context="control_cooler()"
    )
    if desired_temperature is not None:
        if _cooler_min_bound is not None and desired_temperature < _cooler_min_bound:
            desired_temperature = _cooler_min_bound
        if _cooler_max is not None and _cooler_max < desired_temperature:
            desired_temperature = _cooler_max

    room_temperature = snapshot.room_temperature
    cool_target_temperature = snapshot.cool_target_temperature
    heat_target_temperature = snapshot.heat_target_temperature
    tolerance = snapshot.tolerance

    if (
        room_temperature is None
        or cool_target_temperature is None
        or tolerance is None
        or heat_target_temperature is None
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s one or more required values are None "
            "(room_temperature=%s, cool_target_temperature=%s, tolerance=%s, heat_target_temperature=%s), "
            "defaulting to OFF",
            self.device_name,
            self.cooler_entity_id,
            room_temperature,
            cool_target_temperature,
            tolerance,
            heat_target_temperature,
        )
        desired_mode = HVACMode.OFF
    elif snapshot.hvac_mode == HVACMode.OFF:
        desired_mode = HVACMode.OFF
    elif self.contact_open:
        # An open window or door suppresses the cooler for the same reason it
        # suppresses the TRVs: the room cannot reach its target, so the unit
        # would run against an unbounded load. The kernel's window and door
        # regions own the decision, debounce included; the cooler reads their
        # combined verdict because the desired state carries no cooler intent
        # for the kernel to suppress.
        desired_mode = HVACMode.OFF
    else:
        # The tolerance delays the switch-on: cooling starts a tolerance above
        # the cooling target and holds until the room is back at the target, so
        # the room settles at or above the target rather than below it. A band
        # narrower than COOLER_MODE_HYSTERESIS_K takes the missing width from
        # below the target, because a room temperature resting on an edge would
        # otherwise flip the decision — and with it the write — on every cycle;
        # the switch-on edge never moves for that guard, which buys decision
        # stability and not a colder room. The heating target stays a hard
        # floor; relaxing it would let the cooler run into the band the heater
        # is working on.
        #
        # The latch carries the band, and it is unset only while BT has not
        # decided a cooler mode of its own — the state a restart or a
        # config-entry reload leaves behind. Seeding the hold edge from the
        # cooler's own reported mode there keeps a unit that is already running
        # inside the band running, instead of stopping it and letting the room
        # warm all the way back up to the switch-on edge. As soon as the latch
        # holds a decision it wins, because the reported mode lags a command by
        # a state update and can be changed externally, either of which would
        # drop the hold edge mid-band.
        _decided_mode = last_sent.get("hvac_mode_decided")
        _previously_cooling = (
            _decided_mode == HVACMode.COOL
            if _decided_mode is not None
            else current_hvac_mode == HVACMode.COOL
        )
        _cool_wanted = should_cool_with_tolerance(
            room_temperature,
            cool_target_temperature,
            tolerance,
            _previously_cooling,
            min_band=COOLER_MODE_HYSTERESIS_K,
        )
        if _cool_wanted and room_temperature > heat_target_temperature:
            desired_mode = HVACMode.COOL
        else:
            desired_mode = HVACMode.OFF
    # The band's state is the decision, not the send: a device that rejects
    # every command would never advance a send-stamped state, and the
    # decision would keep flipping between the two commands the device is
    # refusing. Recorded for every branch above, so a cycle that fell into a
    # guard leaves the band where that guard put it instead of on a stale
    # value. Each of those guards stops the cooler outright, so the run ends
    # there and the way back in is the switch-on edge — the same contract the
    # heating hysteresis in compute_hvac_action applies to the same three
    # guards, where a missing reading, an open contact and a mode of OFF each
    # return the band to IDLE.
    last_sent["hvac_mode_decided"] = desired_mode

    # A device that is also a controlled thermostat holds one mode and one
    # setpoint for both channels. The decision above is what hands it over: the
    # cooling channel writes it only while it wants to cool, and every other
    # cycle belongs to the heating channel, which reaches the device through
    # control_trv(). Standing down before the writes rather than sending OFF is
    # what keeps the heating channel's mode and setpoint standing; the latch
    # above is set first, so the band keeps both of its edges across the cycles
    # the cooling channel sits out.
    _shared_entity_id = dual_role_entity_id(self)
    if _shared_entity_id is not None and desired_mode != HVACMode.COOL:
        _LOGGER.debug(
            "better_thermostat %s: %s is driven by the heating channel this "
            "cycle, leaving the cooling channel out",
            self.device_name,
            _shared_entity_id,
        )
        # The heating channel overwrites the mode and the setpoint this cycle,
        # so the send cache no longer describes what the device holds. Dropping
        # the send timestamps keeps the resend throttle from suppressing the
        # first write of the next cooling period as a repeat of one the heating
        # channel has since replaced. The values stay, because they are what
        # tells a resend from a fresh command.
        _sent_temperature = last_sent.get("temperature")
        if _sent_temperature is not None and _sent_temperature[1] is not None:
            last_sent["temperature"] = (_sent_temperature[0], None)
        _sent_mode = last_sent.get("hvac_mode")
        if _sent_mode is not None and _sent_mode[1] is not None:
            last_sent["hvac_mode"] = (_sent_mode[0], None)
        return

    if _shared_entity_id is not None:
        # The heating channel's pending confirmations wait on a setpoint and a
        # mode this cycle supersedes, and while they are outstanding the
        # inbound handler declines every reported setpoint as unconfirmed.
        # Taking the device over releases them.
        _shared_trv = self.real_trvs[_shared_entity_id]
        _shared_trv.target_temperature_received = True
        _shared_trv.system_mode_received = True

    # Decide whether a temperature command is needed. When the current
    # temperature is unknown, only send if the desired value changed since
    # the last successful command; otherwise send when it differs from the
    # reported value beyond the device tolerance.
    last_temp, last_temp_ts = last_sent.get("temperature", (None, None))
    temperature_changed_since_last_send = last_temp != desired_temperature
    # A quantizing device settles near the sent value on its own grid. The
    # first post-send reading close to the sent value is remembered as the
    # device's answer; while it holds and the desired value is unchanged,
    # the command counts as converged.
    settled_temperature = last_sent.get("temperature_settled")
    if (
        not temperature_changed_since_last_send
        and last_temp is not None
        and cooler_setpoint is not None
        and settled_temperature is None
        and abs(cooler_setpoint - last_temp) <= COOLER_QUANTIZATION_TOLERANCE_K
    ):
        settled_temperature = cooler_setpoint
        last_sent["temperature_settled"] = settled_temperature
    temperature_to_send: float | None = None
    if desired_temperature is None:
        _LOGGER.debug(
            "better_thermostat %s: cooler %s desired temperature is None, "
            "skipping set_temperature",
            self.device_name,
            self.cooler_entity_id,
        )
    elif cooler_setpoint is None:
        if temperature_changed_since_last_send:
            temperature_to_send = desired_temperature
        else:
            _LOGGER.debug(
                "better_thermostat %s: cooler %s current temperature unknown and "
                "desired temperature unchanged (%s), skipping set_temperature",
                self.device_name,
                self.cooler_entity_id,
                desired_temperature,
            )
    elif not matches_any_setpoint(
        cooler_setpoint, {desired_temperature}, _reconcile_tolerance(self, cooler_state)
    ):
        temperature_to_send = desired_temperature

    # A range write carries both bounds, so a lower bound that drifted away
    # from the heating target needs a send of its own: the cooling target can
    # stay unchanged for as long as the user only moves the heating side.
    _low_bound_drifted = False
    _low_bound_changed = False
    if _write_range and desired_temperature is not None:
        _low_to_set = cooler_low_bound(
            desired_temperature,
            on_cooler_grid(self, cooler_state, heat_target_temperature),
            _cooler_min_bound,
        )
        # A lower bound BT never wrote at this value is a new payload, not a
        # resend; one it already wrote and the device ignored is a retry.
        last_low = last_sent.get("target_temp_low", (None, None))[0]
        _low_bound_changed = last_low != _low_to_set
        current_low = attr_to_celsius(
            self, cooler_state, "target_temp_low", None, "control_cooler()"
        )
        # The bound carries the same quantization latch as the temperature
        # channel: the first post-send reading close to the written bound is
        # the device's answer on its own grid, and while it holds and the
        # wanted bound is unchanged the bound counts as applied. Without it a
        # device that snaps both bounds is rewritten every resend interval
        # for as long as it is configured.
        settled_low = last_sent.get("target_temp_low_settled")
        if (
            not _low_bound_changed
            and last_low is not None
            and current_low is not None
            and settled_low is None
            and abs(current_low - last_low) <= COOLER_QUANTIZATION_TOLERANCE_K
        ):
            settled_low = current_low
            last_sent["target_temp_low_settled"] = settled_low
        _low_bound_settled = (
            not _low_bound_changed
            and settled_low is not None
            and current_low is not None
            and abs(current_low - settled_low) <= RECONCILE_TOLERANCE_K
        )
        # The device answers a written bound on its own grid, so the bound
        # carries the same per-device tolerance the TRV write-skip check uses.
        # A coarser answer than half a step is a bound the device did not
        # apply.
        if (
            current_low is not None
            and not _low_bound_settled
            and not matches_any_setpoint(
                current_low, {_low_to_set}, _reconcile_tolerance(self, cooler_state)
            )
        ):
            _LOGGER.debug(
                "better_thermostat %s: cooler %s lower bound %s differs from %s, "
                "sending both bounds",
                self.device_name,
                self.cooler_entity_id,
                current_low,
                _low_to_set,
            )
            temperature_to_send = desired_temperature
            _low_bound_drifted = True

    # Device quantization accepted: the reported value still sits on the
    # settled post-send reading, so the residual difference is the device's
    # own grid, not an unapplied command. That reading covers the upper bound
    # only, so a drifted lower bound is a deviation it cannot vouch for.
    if (
        temperature_to_send is not None
        and not _low_bound_drifted
        and not temperature_changed_since_last_send
        and settled_temperature is not None
        and cooler_setpoint is not None
        and abs(cooler_setpoint - settled_temperature) <= RECONCILE_TOLERANCE_K
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s settled at %s for desired %s "
            "(device quantization), skipping set_temperature",
            self.device_name,
            self.cooler_entity_id,
            settled_temperature,
            desired_temperature,
        )
        temperature_to_send = None

    # Throttle identical resends when the device's state feedback lags. The
    # cache tracks each channel on its own, so a payload carrying a lower
    # bound that was never written before is not a resend and goes out at
    # once; a bound the device merely ignored keeps its retry pacing.
    if (
        temperature_to_send is not None
        and not (_low_bound_drifted and _low_bound_changed)
        and not temperature_changed_since_last_send
        and last_temp_ts is not None
        and (now_monotonic - last_temp_ts) < COOLER_RESEND_INTERVAL_S
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s suppressing identical set_temperature "
            "within %ss resend interval",
            self.device_name,
            self.cooler_entity_id,
            COOLER_RESEND_INTERVAL_S,
        )
        temperature_to_send = None

    # An open contact suppresses the temperature channel alongside the mode,
    # the way a suppressed TRV receives a mode command and no setpoint. The
    # unit is held OFF and converges on nothing, so a setpoint written now
    # would only overwrite whatever the user turned its own dial to. Nothing
    # is attempted, so the failure backoff below records nothing either, and
    # the channel resumes on the cycle the contact shuts.
    if temperature_to_send is not None and self.contact_open:
        _LOGGER.debug(
            "better_thermostat %s: cooler %s suppressed by an open contact, "
            "skipping set_temperature",
            self.device_name,
            self.cooler_entity_id,
        )
        temperature_to_send = None

    # The command the payload would carry, in °C, as the failure backoff
    # compares it: a rejected send leaves the send cache untouched, so the
    # attempted command is what tells a retry from a new command.
    _temperature_wanted = None
    if temperature_to_send is not None:
        _temperature_wanted = (
            temperature_to_send,
            cooler_low_bound(
                temperature_to_send,
                on_cooler_grid(self, cooler_state, heat_target_temperature),
                _cooler_min_bound,
            )
            if _write_range
            else None,
        )
        if _cooler_retry_deferred(
            last_sent, "temperature", _temperature_wanted, now_monotonic
        ):
            _LOGGER.debug(
                "better_thermostat %s: cooler %s deferring set_temperature at "
                "failure-backoff step %s",
                self.device_name,
                self.cooler_entity_id,
                last_sent["temperature_failed"][0],
            )
            temperature_to_send = None

    if temperature_to_send is not None:
        _LOGGER.debug(
            "better_thermostat %s: TO COOLER set_temperature: %s from: %s to: %s",
            self.device_name,
            self.cooler_entity_id,
            cooler_setpoint,
            temperature_to_send,
        )
        _temperature_to_set = temperature_to_send
        _low_to_set = _low_to_set_c = cooler_low_bound(
            temperature_to_send,
            on_cooler_grid(self, cooler_state, heat_target_temperature),
            _cooler_min_bound,
        )
        if self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT:
            _temperature_to_set = round(
                TemperatureConverter.convert(
                    temperature_to_send,
                    UnitOfTemperature.CELSIUS,
                    UnitOfTemperature.FAHRENHEIT,
                ),
                1,
            )
            _low_to_set = round(
                TemperatureConverter.convert(
                    _low_to_set, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
                ),
                1,
            )
        if _write_range:
            _payload = {
                "entity_id": self.cooler_entity_id,
                "target_temp_high": _temperature_to_set,
                "target_temp_low": _low_to_set,
            }
        else:
            _payload = {
                "entity_id": self.cooler_entity_id,
                "temperature": _temperature_to_set,
            }
        # The device can report the write back while the call is still in
        # flight, so the value is recorded as sent before the call goes out.
        # A failed call must not look like a completed send, otherwise the
        # throttle would suppress the retry, so a failure puts the previous
        # entry back and records its run of failures instead, which paces the
        # retry without pretending the command arrived. Any exception from
        # this one service call is isolated (cloud integrations propagate raw
        # errors such as ConnectionError) so the hvac_mode command below still
        # runs. A command the device's client library cancelled counts as such
        # a failure; a cancellation of this task itself propagates.
        _previous_send = last_sent.get("temperature")
        last_sent["temperature"] = (temperature_to_send, now_monotonic)
        try:
            with command_cancellation_as_disconnect():
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    _payload,
                    blocking=True,
                    context=self.context,
                )
        except Exception as err:  # noqa: BLE001 - a device failure arrives as any exception type
            if _previous_send is None:
                last_sent.pop("temperature", None)
            else:
                last_sent["temperature"] = _previous_send
            _record_cooler_failure(
                last_sent, "temperature", _temperature_wanted, now_monotonic
            )
            _LOGGER.warning(
                "better_thermostat %s: set_temperature for cooler %s failed (%s); "
                "will retry on a later cycle",
                self.device_name,
                self.cooler_entity_id,
                err,
            )
        else:
            last_sent.pop("temperature_failed", None)
            # A fresh send invalidates the settled reading of the channels it
            # carried; the device answers those anew. A single-setpoint
            # payload carries no lower bound, so it says nothing about the
            # bound's settled reading.
            last_sent.pop("temperature_settled", None)
            if _write_range:
                last_sent["target_temp_low"] = (_low_to_set_c, now_monotonic)
                last_sent.pop("target_temp_low_settled", None)

    # Decide whether an hvac_mode command is needed, throttling identical
    # resends the same way as temperature commands.
    last_mode, last_mode_ts = last_sent.get("hvac_mode", (None, None))
    mode_changed_since_last_send = last_mode != desired_mode
    should_send_mode = current_hvac_mode != desired_mode

    if (
        should_send_mode
        and not mode_changed_since_last_send
        and last_mode_ts is not None
        and (now_monotonic - last_mode_ts) < COOLER_RESEND_INTERVAL_S
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s suppressing identical set_hvac_mode "
            "within %ss resend interval",
            self.device_name,
            self.cooler_entity_id,
            COOLER_RESEND_INTERVAL_S,
        )
        should_send_mode = False

    # Same pacing as the temperature channel: a rejected mode command has no
    # send timestamp, so its retry follows the failure backoff.
    if should_send_mode and _cooler_retry_deferred(
        last_sent, "hvac_mode", desired_mode, now_monotonic
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s deferring set_hvac_mode at "
            "failure-backoff step %s",
            self.device_name,
            self.cooler_entity_id,
            last_sent["hvac_mode_failed"][0],
        )
        should_send_mode = False

    if should_send_mode:
        _LOGGER.debug(
            "better_thermostat %s: TO COOLER set_hvac_mode: %s from: %s to: %s",
            self.device_name,
            self.cooler_entity_id,
            current_hvac_mode,
            desired_mode,
        )
        # Isolated like the temperature call above: one failing channel must
        # not abort the cooler cycle.
        try:
            with command_cancellation_as_disconnect():
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": self.cooler_entity_id, "hvac_mode": desired_mode},
                    blocking=True,
                    context=self.context,
                )
        except Exception as err:  # noqa: BLE001 - a device failure arrives as any exception type
            _record_cooler_failure(last_sent, "hvac_mode", desired_mode, now_monotonic)
            _LOGGER.warning(
                "better_thermostat %s: set_hvac_mode for cooler %s failed (%s); "
                "will retry on a later cycle",
                self.device_name,
                self.cooler_entity_id,
                err,
            )
        else:
            last_sent["hvac_mode"] = (desired_mode, now_monotonic)
            last_sent.pop("hvac_mode_failed", None)


async def control_trv(
    self: BetterThermostat,
    entity_id: str | None = None,
    cycle: tuple[WorldSnapshot, DesiredState] | None = None,
) -> bool:
    """Control a single TRV by setting temperature, HVAC mode, calibration, and valve position.

    All operations are executed within self._temperature_lock to ensure atomic execution when
    multiple TRVs are controlled in parallel. Unavailable TRVs are skipped without
    executing any control operations.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str, optional
        Entity ID of the TRV to control. If None or not found, returns False.
    cycle : tuple, optional
        Precomputed ``(snapshot, desired)`` control-cycle decision. If None, it is
        computed for this standalone invocation.

    Returns
    -------
    bool
        True if control succeeded or TRV was skipped (unavailable)
        False if TRV not found in real_trvs or state conversion failed
    """
    # Guard against missing or invalid entity_id
    if not entity_id or entity_id not in self.real_trvs:
        return False

    if not hasattr(self, "task_manager"):
        self.task_manager = TaskManager(hass=self.hass)

    # The suppression flag is owned by the invocation that set it under the
    # lock; a caller cancelled while still waiting for the lock never set it
    # and must not clear it for a concurrent holder mid-write.
    _suppression_owned = False
    try:
        async with self._temperature_lock:
            self.real_trvs[entity_id].ignore_trv_states = True
            _suppression_owned = True
            advance_hvac_action(self)
            _trv = self.hass.states.get(entity_id)

            # The cycle decision normally arrives from control_queue; a
            # standalone invocation is its own cycle.
            if cycle is None:
                cycle = compute_control_cycle(self)
            snapshot, desired = cycle
            trv_desired = desired.trvs.get(entity_id)

            # The kernel addresses only reachable TRVs (boost overrides the skip).
            if _trv is None or trv_desired is None:
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s is unavailable, skipping control. "
                    "Control will resume when TRV becomes available.",
                    self.device_name,
                    entity_id,
                )
                _schedule_reachability_retry(self, entity_id)
                _stamp_heartbeat(self)
                return True

            # See get_current_set_temperatures() docstring for why we accept a
            # match on either the single-setpoint or range-low attribute.
            _current_set_temperatures = get_current_set_temperatures(
                self, _trv, "controlling()"
            )

            _remapped_states = convert_outbound_states(
                self, entity_id, self.bt_hvac_mode
            )
            if not isinstance(_remapped_states, dict):
                _LOGGER.warning(
                    "better_thermostat %s: convert_outbound_states returned %r for %s "
                    "(expected dict) — skipping control cycle",
                    self.device_name,
                    _remapped_states,
                    entity_id,
                )
                # The caller backs the retry off; sleeping here would hold
                # the lock and stall every other TRV of this cycle.
                return False

            _temperature = _remapped_states.get("temperature", None)
            _calibration = _remapped_states.get("local_temperature_calibration", None)

            _advanced = self.real_trvs[entity_id].advanced
            _calibration_mode = configured_calibration_mode(_advanced)
            _calibration_output = configured_calibration_output(_advanced)
            # Pair the forced 100 % valve with a max-temperature setpoint so the TRV
            # firmware does not fight the valve command.
            if (
                is_boost_heating(snapshot)
                and _calibration_output == CalibrationOutput.DIRECT_VALVE_BASED
            ):
                _temperature = self.real_trvs[entity_id].max_temp

            # HOLD rung of the fail-soft ladder: no usable temperature exists,
            # so no calibration runs. The kernel's intent carries the raw
            # user target (passthrough); it is re-sent only when the device
            # diverges, and the safety hull enforces the frost floor. Mode
            # suppression (OFF / window) below stays active.
            if self.kernel_state.control_mode.mode == ControlMode.HOLD:
                _LOGGER.debug(
                    "better_thermostat %s: control mode HOLD - locking %s on the "
                    "last known target %s",
                    self.device_name,
                    entity_id,
                    trv_desired.setpoint,
                )
                _temperature = trv_desired.setpoint
                _calibration = None

            # Optional: set valve position if supported (e.g., MQTT/Z2M)
            try:
                if self.kernel_state.control_mode.mode == ControlMode.HOLD:
                    valve_settings, _source = None, None
                else:
                    valve_settings, _source = _get_valve_control(
                        self,
                        snapshot,
                        entity_id,
                        _calibration_mode,
                        _calibration_output,
                    )
                # A valve with no channel to write through is not pursued,
                # and no retry is scheduled for it, until one appears.
                if valve_settings is not None and not valve_channel_available(
                    self, entity_id
                ):
                    valve_settings = None
                if valve_settings is not None:
                    target_percent = round(valve_settings.get("valve_percent", 0))
                    target_percent = round(
                        _through_safety_hull(
                            snapshot, entity_id, valve_percent=float(target_percent)
                        ).valve_percent
                        or 0.0
                    )
                    # Closing the valve (0 %) is the overheat-safe direction
                    # and bypasses the write budget; everything else waits
                    # for the next slot and converges via the next cycle.
                    if _valve_at_target(self, entity_id, target_percent):
                        _LOGGER.debug(
                            "better_thermostat %s: valve of %s already at %s%%, "
                            "skipping write",
                            self.device_name,
                            entity_id,
                            target_percent,
                        )
                    elif _consume_budget(
                        self, entity_id, "valve", bypass=target_percent == 0
                    ):
                        _LOGGER.debug(
                            "better_thermostat %s: TO TRV set_valve: %s to: %s%% (source=%s)",
                            self.device_name,
                            entity_id,
                            target_percent,
                            _source,
                        )
                        ok = await set_valve(self, entity_id, target_percent)
                        if not ok:
                            _LOGGER.debug(
                                "better_thermostat %s: delegate.set_valve returned False (target=%s%%, entity=%s, source=%s)",
                                self.device_name,
                                target_percent,
                                entity_id,
                                _source,
                            )
                            # The budget was already consumed but the valve never
                            # moved; re-derive on the catch-up cycle so the write
                            # is not dropped permanently.
                            _schedule_budget_retry(
                                self,
                                entity_id,
                                _budget_remaining(self, entity_id, "valve"),
                            )
                    else:
                        # A deferred valve write re-derives on the catch-up
                        # cycle; without it the reconciler cannot see the
                        # miss (it compares against the last value written).
                        _schedule_budget_retry(
                            self, entity_id, _budget_remaining(self, entity_id, "valve")
                        )
                elif _calibration_output != CalibrationOutput.DIRECT_VALVE_BASED:
                    pass  # non-valve TRV: no valve control expected
            except Exception:
                _LOGGER.debug(
                    "better_thermostat %s: set_valve not applied for %s (unsupported or failed)",
                    self.device_name,
                    entity_id,
                    exc_info=True,
                )

            # Apply the kernel's intent: a suppression (open window/door, no heat
            # demand) forces a literal OFF; otherwise the mode follows the
            # device-specific remap of the BT mode. The intent carries the
            # distinction so no shell code re-derives it from the regions.
            if (
                trv_desired.hvac_mode == HVACMode.OFF
                and trv_desired.suppression is not None
            ):
                _new_hvac_mode = HVACMode.OFF
            else:
                _new_hvac_mode = _remapped_states.get("system_mode", None)

            # Safety override: if boost mode was active but we forced OFF (open contact/no-heat),
            # ensure valve is reset to 0% to prevent overheating. Only direct-valve
            # calibration types accept valve commands; LOCAL_BASED and
            # TARGET_TEMP_BASED control via offset / setpoint instead.
            if (
                is_boost_heating(snapshot)
                and _new_hvac_mode == HVACMode.OFF
                and _calibration_output == CalibrationOutput.DIRECT_VALVE_BASED
            ):
                _LOGGER.debug(
                    "better_thermostat %s: Boost safety override - resetting valve to 0%% because HVAC mode is OFF",
                    self.device_name,
                )
                # Closing the valve is the overheat-safe direction and skips
                # the budget gate, but it is a real write: it passes the
                # safety hull and occupies the budget slot like any other.
                _reset_percent = round(
                    _through_safety_hull(
                        snapshot, entity_id, valve_percent=0.0
                    ).valve_percent
                    or 0.0
                )
                if not _valve_at_target(self, entity_id, _reset_percent):
                    _consume_budget(self, entity_id, "valve", bypass=True)
                    ok = await set_valve(self, entity_id, _reset_percent)
                    if not ok:
                        _LOGGER.debug(
                            "better_thermostat %s: delegate.set_valve returned False for "
                            "safety reset (target=%s%%, entity=%s)",
                            self.device_name,
                            _reset_percent,
                            entity_id,
                        )
                        # The valve never moved; re-derive on a catch-up cycle
                        # so the reset is not dropped permanently. The reset
                        # bypasses the budget, so the retry keeps the normal
                        # spacing instead of a HomematicIP head's interval.
                        _schedule_budget_retry(self, entity_id, MIN_WRITE_INTERVAL_S)

            # Manage TRVs with no HVACMode.OFF
            _trv_has_no_off = _no_off_system_mode(self.real_trvs[entity_id])
            if _trv_has_no_off is True and _new_hvac_mode == HVACMode.OFF:
                _min_temp = self.real_trvs[entity_id].min_temp
                _LOGGER.debug(
                    "better_thermostat %s: sending %s°C to the TRV because this device has no system mode off and heater should be off",
                    self.device_name,
                    _min_temp,
                )
                _temperature = _min_temp

            # send new HVAC mode to TRV, if it changed. The mode is re-read
            # here: the valve writes above awaited, so the state captured at
            # the top of the cycle may already be superseded. A device that
            # dropped out in that window reports no mode at all, so there the
            # earlier reading stands in.
            _live_trv = self.hass.states.get(entity_id)
            if _live_trv is None or _live_trv.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                _live_trv = _trv
            _reported_hvac_mode = _live_trv.state
            _mode_trv = self.real_trvs[entity_id]
            if (
                _new_hvac_mode is not None
                and _new_hvac_mode == _reported_hvac_mode
                and _mode_trv.last_hvac_mode != _new_hvac_mode
            ):
                # The device already holds the mode the room wants, so an
                # earlier command for another mode is no longer the one to
                # wait for: the mode watchdog ends on the mode the device
                # holds instead of holding the channel until its timeout.
                # An unconfirmed command stays on the wire, and a slow device
                # may still apply it, so it is remembered as withdrawn.
                if _mode_trv.system_mode_received is False:
                    _mode_trv.withdrawn_hvac_mode = _mode_trv.last_hvac_mode
                    _mode_trv.withdrawn_hvac_mode_until = (
                        self.clock.monotonic() + WRITE_CONFIRM_TIMEOUT_S
                    )
                _mode_trv.last_hvac_mode = _new_hvac_mode
            if (
                _new_hvac_mode is not None
                and _new_hvac_mode != _reported_hvac_mode
                and (
                    (_trv_has_no_off is True and _new_hvac_mode != HVACMode.OFF)
                    or (_trv_has_no_off is False)
                )
            ):
                _LOGGER.debug(
                    "better_thermostat %s: TO TRV set_hvac_mode: %s from: %s to: %s",
                    self.device_name,
                    entity_id,
                    _reported_hvac_mode,
                    _new_hvac_mode,
                )
                _commanded_before = self.real_trvs[entity_id].last_hvac_mode
                self.real_trvs[entity_id].last_hvac_mode = _new_hvac_mode
                self.real_trvs[entity_id].withdrawn_hvac_mode = None
                self.real_trvs[entity_id].withdrawn_hvac_mode_until = None
                _tvr_has_quirk = await override_set_hvac_mode(
                    self, entity_id, _new_hvac_mode
                )
                _mode_refused = False
                if _tvr_has_quirk is False:
                    _mode_refused = (
                        await set_hvac_mode(self, entity_id, _new_hvac_mode) is False
                    )
                # A refused mode is written again by the next cycle, which
                # still finds the device in its old mode; there is nothing to
                # wait for until then. Until it goes through, the device holds
                # the mode it reports, and that is the mode last commanded as
                # far as the mode cache and the inbound handler are concerned:
                # the refused one would read the device's next plain report
                # as a press back to its old mode.
                if _mode_refused:
                    self.real_trvs[entity_id].last_hvac_mode = (
                        _commanded_before
                        if _reported_hvac_mode in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                        else _reported_hvac_mode
                    )
                if (
                    not _mode_refused
                    and self.real_trvs[entity_id].system_mode_received is True
                ):
                    self.real_trvs[entity_id].system_mode_received = False
                    self.task_manager.create_task(
                        check_system_mode(self, entity_id),
                        name=f"bt_check_system_mode_{entity_id}",
                    )

            # set new calibration offset
            if (
                _calibration is not None
                and _new_hvac_mode != HVACMode.OFF
                and _calibration_mode != CalibrationMode.NO_CALIBRATION
                # A disabled calibration entity is no offset channel: the
                # offset is not pursued until it is enabled again.
                and not calibration_entity_disabled(self, entity_id)
            ):
                _current_calibration_raw = await get_calibration_offset(self, entity_id)

                if _current_calibration_raw is None:
                    _LOGGER.error(
                        "better_thermostat %s: calibration fatal error %s",
                        self.device_name,
                        entity_id,
                    )
                    _stamp_heartbeat(self)
                    return True

                _current_calibration = convert_to_float(
                    str(_current_calibration_raw), self.device_name, "controlling()"
                )

                _calibration = float(str(_calibration))
                # Command boundary: the hull owns the device's calibration range.
                # A finite offset goes in and the hull only clamps it to range,
                # so a finite offset comes back out.
                _calibration = _through_safety_hull(
                    snapshot, entity_id, calibration_offset=_calibration
                ).calibration_offset
                if _calibration is None:
                    _LOGGER.debug(
                        "better_thermostat %s: safety hull yielded no offset for "
                        "%s, skipping calibration write this cycle",
                        self.device_name,
                        entity_id,
                    )

                trv = self.real_trvs[entity_id]
                _offset_tolerance = _calibration_match_tolerance(self, entity_id)

                # COMMAND: what the adapter actually put on the wire. Only
                # that value can be acknowledged; before the first write the
                # device's own report stands in for it.
                _last_sent = trv.last_calibration
                if _last_sent is None:
                    _last_sent = _current_calibration

                # Three-valued: an unreadable report neither confirms the
                # command nor proves it was dropped.
                _report_readable = (
                    _current_calibration is not None and _last_sent is not None
                )
                _command_diverged = _report_readable and (
                    abs(float(_current_calibration) - float(_last_sent))
                    > _offset_tolerance
                )
                _command_confirmed = _report_readable and not _command_diverged

                # A device holding what it was told has acknowledged it, even
                # when the state event that would have said so was suppressed.
                if trv.calibration_received is False and _command_confirmed:
                    _LOGGER.debug(
                        "better_thermostat %s: TRV %s device confirms the last "
                        "calibration command (%s), releasing the write gate",
                        self.device_name,
                        entity_id,
                        _last_sent,
                    )
                    trv.calibration_received = True

                if _calibration is not None and trv.calibration_received is True:
                    if _last_sent is None:
                        _LOGGER.debug(
                            "better_thermostat %s: no reference calibration for %s "
                            "yet, skipping calibration write this cycle",
                            self.device_name,
                            entity_id,
                        )
                    else:
                        # INTENT: the value asked for before the adapter's own
                        # clamp. A device resting at a limit it declared keeps
                        # reporting the clamped command, so comparing the
                        # intent against the command would rewrite it every
                        # cycle. Both intent values come off the same step
                        # grid, so they compare exactly; only the report lives
                        # on the device's grid and needs the tolerance.
                        _last_requested = trv.last_calibration_requested
                        if _last_requested is None:
                            _last_requested = _last_sent
                        if float(_last_requested) != _calibration or _command_diverged:
                            # A deferred offset re-derives on the next control cycle
                            # once the slot is free again.
                            if _consume_budget(self, entity_id, "offset"):
                                _LOGGER.debug(
                                    "better_thermostat %s: TO TRV set_local_temperature_calibration: %s from: %s to: %s (device reports %s)",
                                    self.device_name,
                                    entity_id,
                                    _last_sent,
                                    _calibration,
                                    _current_calibration,
                                )
                                if await set_calibration_offset(
                                    self, entity_id, _calibration
                                ):
                                    trv.calibration_received = False
                                    trv.calibration_write_generation += 1
                                    self.task_manager.create_task(
                                        check_calibration(
                                            self,
                                            entity_id,
                                            trv.calibration_write_generation,
                                        ),
                                        name=f"bt_check_calibration_{entity_id}",
                                    )
                            else:
                                _schedule_budget_retry(
                                    self,
                                    entity_id,
                                    _budget_remaining(self, entity_id, "offset"),
                                )

            # set new target temperature
            _safety_overrode_setpoint = False
            if _temperature is not None:
                _raw_temperature = float(_temperature)
                _temperature = _through_safety_hull(
                    snapshot, entity_id, setpoint=_raw_temperature
                ).setpoint
                _safety_overrode_setpoint = _temperature != _raw_temperature
            if _temperature is not None and (
                _new_hvac_mode != HVACMode.OFF or _trv_has_no_off
            ):
                # Tolerance-based comparison: the outbound value lies on the
                # device step grid, the read-back values on the 0.01 grid, so
                # exact set membership would re-send identical setpoints.
                if not matches_any_setpoint(_temperature, _current_set_temperatures):
                    trv = self.real_trvs[entity_id]
                    # Safety-relevant writes (frost floor / OFF) bypass the
                    # write budget; everything else waits for the next slot
                    # and converges via the scheduled retry.
                    if _consume_budget(
                        self,
                        entity_id,
                        "setpoint",
                        bypass=_safety_overrode_setpoint
                        or _new_hvac_mode == HVACMode.OFF,
                    ):
                        old = trv.commanded_setpoint
                        _LOGGER.debug(
                            "better_thermostat %s: TO TRV set_temperature: %s from: %s to: %s",
                            self.device_name,
                            entity_id,
                            old,
                            _temperature,
                        )
                        trv.commanded_setpoint = _temperature
                        trv.remember_setpoint_written(_temperature)
                        try:
                            _tvr_has_quirk = await override_set_temperature(
                                self, entity_id, _temperature
                            )
                            if _tvr_has_quirk is False:
                                await set_temperature(self, entity_id, _temperature)
                        finally:
                            # The delegate records the value it sent after its
                            # own rounding and clamping, which is the one the
                            # device can echo, and records it before the call
                            # goes out. Only writes of this path are
                            # remembered: maintenance drives the device through
                            # the delegate and nothing confirms those writes.
                            trv.remember_setpoint_written(trv.commanded_setpoint)
                            # Every write is watched on its own: a watchdog
                            # still waiting on an earlier write steps aside for
                            # this one rather than holding the channel for a
                            # command the device may never report. A call that
                            # raises may still have reached the device, and the
                            # earlier watchdog has already stepped aside, so
                            # this write is watched either way.
                            trv.target_temperature_received = False
                            self.task_manager.create_task(
                                check_target_temperature(
                                    self,
                                    entity_id,
                                    trv.last_setpoint_write_id,
                                    trv.commanded_setpoint,
                                ),
                                name=f"bt_check_target_temp_{entity_id}",
                            )
                    else:
                        # A deferred setpoint re-derives on the catch-up cycle
                        # once the slot is free again. Falling through to the
                        # shared exit keeps the settle sleep outside the lock,
                        # so a deferred TRV does not serialise the others.
                        _schedule_budget_retry(
                            self,
                            entity_id,
                            _budget_remaining(self, entity_id, "setpoint"),
                        )
                else:
                    # The device already holds what the room wants, whoever
                    # put it there: a knob turned while the room was off can
                    # land on the setpoint the room asks for once it heats
                    # again. That value is BT's own from here on.
                    self.real_trvs[entity_id].remember_setpoint_held(_temperature)

        # Watchdog heartbeat: the control loop demonstrably ran.
        _stamp_heartbeat(self)

        # Let TRV state updates propagate before accepting new state events
        await asyncio.sleep(TRV_STATE_SETTLE_S)
        return True
    finally:
        if _suppression_owned:
            self.real_trvs[entity_id].ignore_trv_states = False


async def check_system_mode(self: BetterThermostat, entity_id: str) -> bool:
    """Wait for TRV to confirm HVAC mode change, timeout after 6 minutes.

    Polls the TRV's live entity state every second until it matches
    last_hvac_mode or timeout is reached. Sets system_mode_received flag
    when complete. Reading the live state directly avoids depending on the
    internal hvac_mode cache, which is not refreshed while state events are
    suppressed (control cycle) or when child lock is configured.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity ID of the TRV to check

    Returns
    -------
    bool
        Always returns True
    """
    _timeout = 0
    trv = self.real_trvs[entity_id]
    state_unknown_as_available = trv_state_unknown_as_available(self, entity_id)
    while True:
        _trv_state = self.hass.states.get(entity_id)
        if (
            _trv_state is None
            or _trv_state.state == STATE_UNAVAILABLE
            or (_trv_state.state == STATE_UNKNOWN and not state_unknown_as_available)
        ):
            _LOGGER.debug(
                "better_thermostat %s: %s became unavailable during check_system_mode",
                self.device_name,
                entity_id,
            )
            break
        # A device whose quirk reads `unknown` as operating cannot report the
        # mode back: the mode it is in is not one the climate entity
        # describes. Waiting for a match it will never make would hold the
        # write open for the full confirmation budget and then warn about a
        # device that is doing exactly what it was told.
        if _trv_state.state == trv.last_hvac_mode or (
            _trv_state.state == STATE_UNKNOWN and state_unknown_as_available
        ):
            _timeout = 0
            break
        if _timeout > WRITE_CONFIRM_TIMEOUT_S:
            _LOGGER.warning(
                "better_thermostat %s: TRV %s did not confirm the system mode change "
                "after %ss (wrote=%s, last reported=%s); giving up and assuming applied",
                self.device_name,
                entity_id,
                WRITE_CONFIRM_TIMEOUT_S,
                trv.last_hvac_mode,
                _trv_state.state,
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)
    trv.system_mode_received = True
    return True


async def check_target_temperature(
    self: BetterThermostat, entity_id: str, write_id: int, setpoint: float | None
) -> bool:
    """Wait for TRV to confirm target temperature change, timeout after 6 minutes.

    Polls the TRV's temperature (and target_temp_low, when range mode is
    supported) attribute every second until either matches the awaited
    command within SETPOINT_MATCH_TOLERANCE or timeout is reached. Sets
    target_temperature_received flag when complete. The command is fixed when the
    watchdog is started: valve maintenance writes through the same delegate
    and moves ``commanded_setpoint`` on without going through the control
    path, so a maintenance value must not be able to confirm a control
    write. The id that command went out under is fixed with it, so the
    confirmation retires that write and the ones before it and leaves
    anything written while the wait ran. An unreadable setpoint ends the
    wait without confirming one.

    Each control write starts a watchdog of its own. Once a newer write has
    gone out, this one no longer speaks for the channel: it still records a
    report of its own command as confirmed, but otherwise ends without
    waiting for the timeout, and only the watchdog of the newest write
    releases ``target_temperature_received``.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity ID of the TRV to check
    write_id : int
        Id of the write this watchdog was started for
    setpoint : float | None
        The value that write sent, in °C

    Returns
    -------
    bool
        Always returns True
    """
    _timeout = 0
    trv = self.real_trvs[entity_id]
    _awaited_setpoint = setpoint
    _awaited_write_id = write_id
    state_unknown_as_available = trv_state_unknown_as_available(self, entity_id)
    while True:
        _trv_state = self.hass.states.get(entity_id)
        if (
            _trv_state is None
            or _trv_state.state == STATE_UNAVAILABLE
            or (_trv_state.state == STATE_UNKNOWN and not state_unknown_as_available)
        ):
            _LOGGER.debug(
                "better_thermostat %s: %s became unavailable during check_target_temperature",
                self.device_name,
                entity_id,
            )
            break
        # See get_current_set_temperatures() docstring for why we accept a
        # match on either the single-setpoint or range-low attribute.
        _current_set_temperatures = get_current_set_temperatures(
            self, _trv_state, "check_target_temperature()"
        )
        if _timeout == 0:
            _LOGGER.debug(
                "better_thermostat %s: %s / check_target_temp / _last: %s - _current: %s",
                self.device_name,
                entity_id,
                _awaited_setpoint,
                _current_set_temperatures,
            )
        # An empty set (no readable setpoint) ends the wait without a
        # confirmation, so the writes the device may still hold stay
        # remembered; a non-empty set is matched with a tolerance because
        # written and read-back setpoints lie on different float rounding
        # grids.
        if not _current_set_temperatures:
            _timeout = 0
            break
        if matches_any_setpoint(_awaited_setpoint, _current_set_temperatures):
            trv.remember_setpoint_confirmed(_awaited_setpoint, _awaited_write_id)
            _timeout = 0
            break
        if trv.last_setpoint_write_id != _awaited_write_id:
            _LOGGER.debug(
                "better_thermostat %s: a newer setpoint write superseded the one "
                "%s was being watched for, leaving the channel to its watchdog",
                self.device_name,
                entity_id,
            )
            return True
        if _timeout > WRITE_CONFIRM_TIMEOUT_S:
            _LOGGER.warning(
                "better_thermostat %s: TRV %s did not confirm the target temperature "
                "after %ss (wrote=%s, last reported=%s); giving up and assuming applied",
                self.device_name,
                entity_id,
                WRITE_CONFIRM_TIMEOUT_S,
                _awaited_setpoint,
                _current_set_temperatures,
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)

    if trv.last_setpoint_write_id == _awaited_write_id:
        trv.target_temperature_received = True
    return True


async def check_calibration(
    self: BetterThermostat, entity_id: str, generation: int = 0
) -> bool:
    """Wait for TRV to confirm a calibration offset write, timeout after 6 minutes.

    Polls the device's reported offset every second until it is within
    the device's own step tolerance of the value last written, or the
    timeout is reached. Sets calibration_received when complete: the
    write gate only re-asserts an offset once that flag is back, so a
    device that never acknowledges is re-asserted once per timeout
    window instead of once per control cycle.

    The reported value is deliberately not adopted as the last written
    one — that record is the integrator base the next offset is computed
    from, and taking the device's report for it would make a dropped
    write look confirmed.

    The release happens in a finally block. The mode and setpoint
    channels write regardless of their flag, but the offset write only
    goes out while calibration_received is True, so a watchdog that ended
    on an adapter error or a cancellation would silence the channel for
    the lifetime of the entity.

    Only the watchdog whose generation is still the TRV's current one
    releases the flag. A control cycle can confirm a command in-cycle and
    write a newer offset while an earlier watchdog is still winding down;
    releasing the gate from that earlier watchdog would open the channel
    for a command that is still in flight and turn one re-assert per
    confirmation window into one per write-budget slot.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity ID of the TRV to check
    generation : int, optional
        Identity of the offset command this watchdog was armed for

    Returns
    -------
    bool
        Always returns True
    """
    _timeout = 0
    trv = self.real_trvs[entity_id]
    _tolerance = _calibration_match_tolerance(self, entity_id)
    try:
        while True:
            if trv.calibration_write_generation != generation:
                _LOGGER.debug(
                    "better_thermostat %s: a newer calibration command superseded "
                    "the one %s was being watched for, leaving the write gate to "
                    "its watchdog",
                    self.device_name,
                    entity_id,
                )
                return True
            _trv_state = self.hass.states.get(entity_id)
            if _trv_state is None or _trv_state.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                _LOGGER.debug(
                    "better_thermostat %s: %s became unavailable during check_calibration",
                    self.device_name,
                    entity_id,
                )
                break
            _reported = convert_to_float(
                str(await get_calibration_offset(self, entity_id)),
                self.device_name,
                "check_calibration()",
            )
            if trv.last_calibration is None or (
                _reported is not None
                and abs(_reported - float(trv.last_calibration)) <= _tolerance
            ):
                _timeout = 0
                break
            if _timeout > WRITE_CONFIRM_TIMEOUT_S:
                _LOGGER.warning(
                    "better_thermostat %s: TRV %s did not confirm the calibration offset "
                    "after %ss (wrote=%s, last reported=%s); giving up and assuming applied",
                    self.device_name,
                    entity_id,
                    WRITE_CONFIRM_TIMEOUT_S,
                    trv.last_calibration,
                    _reported,
                )
                _timeout = 0
                break
            await asyncio.sleep(1)
            _timeout += 1
        await asyncio.sleep(2)
    finally:
        if trv.calibration_write_generation == generation:
            trv.calibration_received = True
    return True
