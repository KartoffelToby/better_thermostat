"""Controlling module for Better Thermostat."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import math

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.util.unit_conversion import TemperatureConverter

from custom_components.better_thermostat.adapters.delegate import (
    get_current_offset,
    set_hvac_mode,
    set_offset,
    set_temperature,
    set_valve,
)
from custom_components.better_thermostat.core.decide import decide, is_boost_heating
from custom_components.better_thermostat.core.desired import DesiredState, TrvDesired
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.core.safety import clamp as safety_clamp
from custom_components.better_thermostat.core.watchdog import (
    WATCHDOG_MAX_AGE_S,
    control_loop_stalled,
)
from custom_components.better_thermostat.events.trv import convert_outbound_states
from custom_components.better_thermostat.model_fixes.model_quirks import (
    override_set_hvac_mode,
    override_set_temperature,
)
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.helpers import (
    COOLER_SETPOINT_KEYS,
    attr_to_celsius,
    clamp_valve_percent,
    convert_to_float,
    get_current_set_temperatures,
    matches_any_setpoint,
    read_setpoint_celsius,
    state_temperature_unit,
    supports_single_target_temperature,
    supports_temperature_range,
)
from custom_components.better_thermostat.utils.hvac_action import (
    should_cool_with_tolerance,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle
from custom_components.better_thermostat.utils.snapshot import build_snapshot

_LOGGER = logging.getLogger(__name__)

# Write budget: minimum spacing between non-safety writes to one TRV.
# TRVs are battery- and radio-constrained; bursts of writes are a real
# failure cause. Safety-relevant writes (frost floor, OFF) bypass this.
MIN_WRITE_INTERVAL_S = 30.0
# Device tolerance when comparing commanded vs reported setpoints.
RECONCILE_TOLERANCE_K = 0.05
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
# Minimum width of the cooling decision band. A tolerance narrower than this
# leaves the room temperature resting on an edge, where it flips the decision —
# and produces a write — on every control cycle; the missing width is taken
# from below the cooling target to keep the switch-on edge where the tolerance
# puts it. Two steps of a 0.1 °C room sensor, which is what the flip is made
# of; and well under the 0.5 °C a cooling setpoint is set in, so the extra run
# time the band costs is finer than the user can express in the target anyway.
COOLER_MODE_HYSTERESIS_K = 0.2
# Valve deviations below this are the device's own business.
RECONCILE_VALVE_TOLERANCE_PCT = 5.0
# Pause before re-queueing a cycle in which a TRV reported failure, so a
# persistently failing device cannot spin the control queue.
FAILED_CYCLE_BACKOFF_S = 2.0
# How long a write channel waits for the device to confirm a command
# before its watchdog releases the in-flight flag and assumes the command
# applied. Shared by the mode, setpoint and calibration watchdogs, so a
# device that never confirms paces its re-assert at this interval on
# every channel.
WRITE_CONFIRM_TIMEOUT_S = 360


def _budget_open(last_write: float | None, now_monotonic: float) -> bool:
    """Whether a channel's write-budget slot is free again."""
    return last_write is None or now_monotonic - last_write >= MIN_WRITE_INTERVAL_S


# Per-channel write-budget stamp fields on the Trv.
_BUDGET_STAMPS = {
    "setpoint": "last_write_monotonic",
    "offset": "last_offset_write_monotonic",
    "valve": "last_valve_write_monotonic",
}


def _consume_budget(
    self, entity_id: str, channel: str, *, bypass: bool = False
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
    if not bypass and not _budget_open(last, now):
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


def _budget_remaining(self, entity_id: str, channel: str) -> float:
    """Seconds until a channel's write-budget slot reopens."""
    trv = self.real_trvs[entity_id]
    last = getattr(trv, _BUDGET_STAMPS[channel])
    if last is None:
        # Never written on this channel, so the slot is already open.
        # Subtracting a monotonic clock from zero would yield a large
        # negative interval instead.
        return 0.0
    return MIN_WRITE_INTERVAL_S - (self.clock.monotonic() - last)


def _no_off_system_mode(trv) -> bool:
    """Whether this TRV cannot be switched off.

    Such devices receive their min temp in place of OFF and keep
    reporting a heating mode, by design. Answered by the capability
    descriptor, not by re-deriving from raw fields.
    """
    return not trv.capabilities().supports_off_mode


def _schedule_budget_retry(self, entity_id: str, retry_in_s: float) -> None:
    """Queue one control cycle for when the write budget reopens.

    A deferred setpoint write needs this follow-up: the reconciler
    compares the device against the last value actually written — which
    the device still matches — and configurations without a calibration
    tick have no other periodic trigger.
    """
    trv = self.real_trvs[entity_id]
    if trv.budget_retry_pending:
        return
    trv.budget_retry_pending = True

    async def _retry() -> None:
        try:
            await asyncio.sleep(max(retry_in_s, 0.0))
        finally:
            trv.budget_retry_pending = False
        request_control_cycle(self)

    self.task_manager.create_task(_retry(), name=f"bt_budget_retry_{entity_id}")


def _schedule_reachability_retry(self, entity_id: str) -> None:
    """Queue one control cycle for an offline TRV's next retry window.

    Consumes the reachability region's ``retry_at``: the cycle re-probes
    the device, and while it stays offline the region's step advances
    the exponential backoff. Availability events still trigger an
    immediate cycle when the device returns by itself.
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
        try:
            await asyncio.sleep(delay)
        finally:
            trv.reachability_retry_pending = False
        request_control_cycle(self)

    self.task_manager.create_task(_retry(), name=f"bt_reachability_retry_{entity_id}")


def _stamp_heartbeat(self) -> None:
    """Record that a control cycle ran to a deliberate decision.

    Skipping an unavailable TRV or deferring a write to the budget is
    such a decision; error paths that bail out without one deliberately
    leave the stamp alone so the watchdog can detect a silent hang.
    """
    self.kernel_state = replace(
        self.kernel_state, last_control_monotonic=self.clock.monotonic()
    )


def _get_valve_control(
    self, snapshot, heater_entity_id: str, calibration_mode, calibration_type
) -> tuple[dict | None, str | None]:
    """Determine valve control settings based on boost mode or calibration.

    Returns a tuple of (valve_settings_dict, source_name).
    valve_settings_dict contains 'valve_percent' and 'apply_valve' keys.
    Returns (None, None) if no valve control should be applied.
    """
    # Forcing the valve on a non-direct-valve TRV bypasses the calibration chain
    # and leaves the valve stuck open after boost ends.
    if (
        is_boost_heating(snapshot)
        and calibration_type == CalibrationType.DIRECT_VALVE_BASED
    ):
        _trv = self.real_trvs.get(heater_entity_id)
        max_opening = _trv.valve_max_opening if _trv is not None else 100
        if isinstance(max_opening, (int, float)):
            target_pct = clamp_valve_percent(max_opening)
        else:
            target_pct = 100
        return {"valve_percent": target_pct, "apply_valve": True}, "boost_mode"

    # Check calibration-based valve control
    if calibration_type != CalibrationType.DIRECT_VALVE_BASED:
        return None, None

    # Try calibration balance from various calibration modes
    cal_bal = self.real_trvs[heater_entity_id].calibration_balance
    if (
        isinstance(cal_bal, dict)
        and cal_bal.get("apply_valve")
        and cal_bal.get("valve_percent") is not None
    ):
        source_map = {
            CalibrationMode.MPC_CALIBRATION: "mpc_calibration",
            CalibrationMode.MPC_V2_CALIBRATION: "mpc_v2_calibration",
            CalibrationMode.TPI_CALIBRATION: "tpi_calibration",
            CalibrationMode.PID_CALIBRATION: "pid_calibration",
            CalibrationMode.HEATING_POWER_CALIBRATION: "heating_power_calibration",
        }
        source = source_map.get(calibration_mode)
        if source:
            return cal_bal, source

    # Fallback to raw balance
    raw_balance = self.real_trvs[heater_entity_id].balance
    if (
        isinstance(raw_balance, dict)
        and raw_balance.get("apply_valve")
        and raw_balance.get("valve_percent") is not None
    ):
        return raw_balance, "balance"

    return None, None


def compute_control_cycle(self, *, record: bool = True, commit: bool = True):
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


def _reconcile_tolerance(self, state) -> float:
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


def _calibration_match_tolerance(self, entity_id) -> float:
    """Per-device tolerance for the commanded-vs-reported offset comparison.

    Devices snap a written offset onto their own step grid; a snapped
    value sits at most half a step away from the commanded one. The base
    tolerance covers devices that report no usable step.
    """
    step = convert_to_float(
        str(self.real_trvs[entity_id].local_calibration_step),
        self.device_name,
        "controlling()",
    )
    if step is None or step <= 0:
        return RECONCILE_TOLERANCE_K
    # Slack against float noise when the difference is exactly half a step.
    return max(RECONCILE_TOLERANCE_K, step / 2.0 + 1e-6)


def _offset_diverges(self, trv) -> bool:
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


def _valve_diverges(self, trv) -> bool:
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


def _valve_at_target(self, entity_id: str, target_pct: float) -> bool:
    """Whether the valve channel already matches the intent.

    True when the last commanded percentage equals the (int-rounded)
    target and the readable position entity, if any, has not diverged
    from it — no difference, no network write.
    """
    trv = self.real_trvs[entity_id]
    if trv.last_valve_percent is None:
        return False
    if int(round(float(trv.last_valve_percent))) != int(round(float(target_pct))):
        return False
    return not _valve_diverges(self, trv)


def desired_diverges(self, snapshot, desired) -> bool:
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

        if intent.hvac_mode is not None:
            if intent.hvac_mode == HVACMode.OFF:
                # A device that cannot switch off converges on its min
                # temp instead; the setpoint comparison below covers it.
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
        commanded = trv.last_temperature
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


async def reconcile_tick(self, now=None):
    """Periodic reconciliation: re-converge devices onto the intent.

    Builds a snapshot, asks the kernel for the desired state, and
    enqueues one control cycle when any device diverges — the general
    mechanism that heals lost writes without per-case keepalives.
    """
    if self.startup_running or self.ignore_states:
        return
    if self.kernel_state.maintenance.is_blocking(self.clock.monotonic()):
        return
    try:
        if control_loop_stalled(
            self.kernel_state.last_control_monotonic, self.clock.monotonic()
        ):
            _LOGGER.error(
                "better_thermostat %s: control watchdog: no control cycle for "
                "more than %.0f minutes, forcing one",
                self.device_name,
                WATCHDOG_MAX_AGE_S / 60.0,
            )
            request_control_cycle(self)
            return
        snapshot, desired = compute_control_cycle(self, record=False, commit=False)
        desired = safety_clamp(desired, snapshot)
        if not desired_diverges(self, snapshot, desired):
            return
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
    snapshot,
    entity_id: str,
    *,
    setpoint: float | None = None,
    valve_percent=None,
    offset: float | None = None,
) -> TrvDesired:
    """Run one intent through the safety hull at the command boundary."""
    desired = DesiredState(
        trvs={
            entity_id: TrvDesired(
                entity_id=entity_id,
                setpoint=setpoint,
                valve_percent=valve_percent,
                offset=offset,
            )
        }
    )
    return safety_clamp(desired, snapshot).trvs[entity_id]


class TaskManager:
    """Manages background asyncio tasks with automatic cleanup.

    Tracks created tasks and automatically removes them from the set when they complete.
    """

    def __init__(self, hass=None):
        """Initialize the task manager with an empty task set."""
        self.tasks = set()
        self.hass = hass

    def create_task(self, coro, name=None):
        """Create and track an asyncio task with automatic cleanup on completion.

        Parameters
        ----------
        coro : Coroutine
            The coroutine to execute as a task
        name : str, optional
            A descriptive name for the background task

        Returns
        -------
        asyncio.Task
            The created task
        """
        if self.hass is not None:
            task = self.hass.async_create_background_task(
                coro, name=name or "bt_task_manager_task"
            )
        else:
            task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


async def control_queue(self):
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

    try:
        while True:
            if getattr(self, "in_maintenance", False):
                await asyncio.sleep(1)
                continue

            if self.ignore_states or self.startup_running:
                await asyncio.sleep(1)
                continue
            else:
                controls_to_process = await self.control_queue_task.get()
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
                    # observation the TRVs are controlled with.
                    if self.cooler_entity_id is not None:
                        try:
                            await control_cooler(
                                self, cycle[0] if cycle is not None else None
                            )
                        except Exception:
                            _LOGGER.exception(
                                "better_thermostat %s: ERROR controlling cooler",
                                self.device_name,
                            )

                    # Create tasks for all TRVs to run in parallel
                    tasks = []
                    for trv in self.real_trvs.keys():
                        tasks.append(control_trv(self, trv, cycle=cycle))

                    # Run all TRV controls in parallel
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    result = True
                    for i, res in enumerate(results):
                        if isinstance(res, Exception):
                            trv_id = list(self.real_trvs.keys())[i]
                            _LOGGER.error(
                                "better_thermostat %s: ERROR controlling TRV %s: %s",
                                self.device_name,
                                trv_id,
                                res,
                            )
                            result = False
                        elif res is False:
                            result = False

                    # Retry task if some TRVs failed; coalesces with any
                    # already-pending request. The backoff sits here rather
                    # than in the failing worker: a worker holds the TRV lock
                    # and would stall the rest of the cycle with it.
                    if result is False:
                        await asyncio.sleep(FAILED_CYCLE_BACKOFF_S)
                        request_control_cycle(self)

                    self.control_queue_task.task_done()
                    if not getattr(self, "in_maintenance", False):
                        self.ignore_states = False
    except asyncio.CancelledError:
        _LOGGER.debug(
            "better_thermostat %s: control_queue task cancelled, cleaning up",
            self.device_name,
        )
        raise
    finally:
        # Ensure ignore_states is reset on any exit unless maintenance wants it suppressed.
        if not getattr(self, "in_maintenance", False):
            self.ignore_states = False


def cooler_low_bound(high: float, target_temp: float | None) -> float:
    """Return the lower bound that travels with ``high`` in a range write.

    A range write needs both bounds, and Home Assistant rejects a low bound
    above the high one. The heating target is the natural lower bound; it can
    only exceed the cooling target while the two are out of sync, so it is
    capped at the value being written.
    """
    if target_temp is None:
        return high
    return min(float(target_temp), high)


def cooler_send_cache(self) -> dict:
    """Return the cooler send-cache, creating it on first use.

    Holds the last successfully sent command per channel as
    ``(value, monotonic_timestamp)`` for the resend throttle, the settled
    reading of each written channel, the mode the last cycle decided on for
    the hysteresis band, and each channel's run of consecutive send failures
    as ``(count, monotonic_timestamp, attempted_value)``. Created lazily
    because only cooler-equipped instances need it.
    """
    last_sent = getattr(self, "_cooler_last_sent", None)
    if not isinstance(last_sent, dict):
        last_sent = {}
        self._cooler_last_sent = last_sent
    return last_sent


def last_sent_cooler_temperature(self) -> float | None:
    """Return the cooling setpoint BT last wrote to the cooler, in °C."""
    value = cooler_send_cache(self).get("temperature", (None, None))[0]
    return value if isinstance(value, (int, float)) else None


def _cooler_retry_deferred(
    last_sent: dict, channel: str, wanted, now_monotonic: float
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
    failures, failed_at, failed_wanted = last_sent.get(
        f"{channel}_failed", (0, None, None)
    )
    if not failures or failed_at is None:
        return False
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
    last_sent: dict, channel: str, wanted, now_monotonic: float
) -> None:
    """Extend a channel's run of consecutive failures of one command.

    A run is a run of the same command; a different one that fails starts
    its own, so the run the counter holds and the run the gate prices always
    describe the same command. The count stops at the length the backoff can
    still tell apart.
    """
    failures, _, failed_wanted = last_sent.get(f"{channel}_failed", (0, None, None))
    if failed_wanted != wanted:
        failures = 0
    last_sent[f"{channel}_failed"] = (
        min(failures + 1, COOLER_FAILURE_BACKOFF_MAX_RUN),
        now_monotonic,
        wanted,
    )


async def control_cooler(self, snapshot=None):
    """Control the cooler entity based on current temperature and cooling setpoint.

    Activates cooling when the current temperature reaches the cooling target
    plus tolerance and is above the heating target, so a configured tolerance
    delays the switch-on instead of running the room below the cooling target.
    Deactivates cooling when the temperature falls back below the cooling
    target — or below the cooling target minus the width
    COOLER_MODE_HYSTERESIS_K borrows from underneath it whenever the tolerance
    is narrower than that minimum band — or when BT HVAC mode is OFF.

    The control queue passes the cycle's snapshot in; a standalone
    invocation observes the world itself.
    """
    # Get current cooler state to avoid sending redundant commands
    cooler_state = self.hass.states.get(self.cooler_entity_id)
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
    current_temp = read_setpoint_celsius(
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

    # Determine desired state based on the world snapshot of this cycle
    if snapshot is None:
        snapshot = build_snapshot(self)
    desired_temp = snapshot.target_cooltemp

    room_temp = snapshot.room_temp
    target_cooltemp = snapshot.target_cooltemp
    target_temp = snapshot.target_temp
    tolerance = snapshot.tolerance

    if (
        room_temp is None
        or target_cooltemp is None
        or tolerance is None
        or target_temp is None
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s one or more required values are None "
            "(cur_temp=%s, bt_target_cooltemp=%s, tolerance=%s, bt_target_temp=%s), "
            "defaulting to OFF",
            self.device_name,
            self.cooler_entity_id,
            room_temp,
            target_cooltemp,
            tolerance,
            target_temp,
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
            room_temp,
            target_cooltemp,
            tolerance,
            _previously_cooling,
            min_band=COOLER_MODE_HYSTERESIS_K,
        )
        if _cool_wanted and room_temp > target_temp:
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

    # Decide whether a temperature command is needed. When the current
    # temperature is unknown, only send if the desired value changed since
    # the last successful command; otherwise send when it differs from the
    # reported value beyond the device tolerance.
    last_temp, last_temp_ts = last_sent.get("temperature", (None, None))
    temp_changed_since_last_send = last_temp != desired_temp
    # A quantizing device settles near the sent value on its own grid. The
    # first post-send reading close to the sent value is remembered as the
    # device's answer; while it holds and the desired value is unchanged,
    # the command counts as converged.
    settled_temp = last_sent.get("temperature_settled")
    if (
        not temp_changed_since_last_send
        and last_temp is not None
        and current_temp is not None
        and settled_temp is None
        and abs(current_temp - last_temp) <= COOLER_QUANTIZATION_TOLERANCE_K
    ):
        settled_temp = current_temp
        last_sent["temperature_settled"] = settled_temp
    temp_to_send: float | None = None
    if desired_temp is None:
        _LOGGER.debug(
            "better_thermostat %s: cooler %s desired temperature is None, "
            "skipping set_temperature",
            self.device_name,
            self.cooler_entity_id,
        )
    elif current_temp is None:
        if temp_changed_since_last_send:
            temp_to_send = desired_temp
        else:
            _LOGGER.debug(
                "better_thermostat %s: cooler %s current temperature unknown and "
                "desired temperature unchanged (%s), skipping set_temperature",
                self.device_name,
                self.cooler_entity_id,
                desired_temp,
            )
    elif not matches_any_setpoint(
        current_temp, {desired_temp}, _reconcile_tolerance(self, cooler_state)
    ):
        temp_to_send = desired_temp

    # A range write carries both bounds, so a lower bound that drifted away
    # from the heating target needs a send of its own: the cooling target can
    # stay unchanged for as long as the user only moves the heating side.
    _low_bound_drifted = False
    _low_bound_changed = False
    if _write_range and desired_temp is not None:
        _low_to_set = cooler_low_bound(desired_temp, target_temp)
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
            temp_to_send = desired_temp
            _low_bound_drifted = True

    # Device quantization accepted: the reported value still sits on the
    # settled post-send reading, so the residual difference is the device's
    # own grid, not an unapplied command. That reading covers the upper bound
    # only, so a drifted lower bound is a deviation it cannot vouch for.
    if (
        temp_to_send is not None
        and not _low_bound_drifted
        and not temp_changed_since_last_send
        and settled_temp is not None
        and current_temp is not None
        and abs(current_temp - settled_temp) <= RECONCILE_TOLERANCE_K
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s settled at %s for desired %s "
            "(device quantization), skipping set_temperature",
            self.device_name,
            self.cooler_entity_id,
            settled_temp,
            desired_temp,
        )
        temp_to_send = None

    # Throttle identical resends when the device's state feedback lags. The
    # cache tracks each channel on its own, so a payload carrying a lower
    # bound that was never written before is not a resend and goes out at
    # once; a bound the device merely ignored keeps its retry pacing.
    if (
        temp_to_send is not None
        and not (_low_bound_drifted and _low_bound_changed)
        and not temp_changed_since_last_send
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
        temp_to_send = None

    # An open contact suppresses the temperature channel alongside the mode,
    # the way a suppressed TRV receives a mode command and no setpoint. The
    # unit is held OFF and converges on nothing, so a setpoint written now
    # would only overwrite whatever the user turned its own dial to. Nothing
    # is attempted, so the failure backoff below records nothing either, and
    # the channel resumes on the cycle the contact shuts.
    if temp_to_send is not None and self.contact_open:
        _LOGGER.debug(
            "better_thermostat %s: cooler %s suppressed by an open contact, "
            "skipping set_temperature",
            self.device_name,
            self.cooler_entity_id,
        )
        temp_to_send = None

    # The command the payload would carry, in °C, as the failure backoff
    # compares it: a rejected send leaves the send cache untouched, so the
    # attempted command is what tells a retry from a new command.
    _temp_wanted = None
    if temp_to_send is not None:
        _temp_wanted = (
            temp_to_send,
            cooler_low_bound(temp_to_send, target_temp) if _write_range else None,
        )
        if _cooler_retry_deferred(
            last_sent, "temperature", _temp_wanted, now_monotonic
        ):
            _LOGGER.debug(
                "better_thermostat %s: cooler %s deferring set_temperature at "
                "failure-backoff step %s",
                self.device_name,
                self.cooler_entity_id,
                last_sent["temperature_failed"][0],
            )
            temp_to_send = None

    if temp_to_send is not None:
        _LOGGER.debug(
            "better_thermostat %s: TO COOLER set_temperature: %s from: %s to: %s",
            self.device_name,
            self.cooler_entity_id,
            current_temp,
            temp_to_send,
        )
        _temp_to_set = temp_to_send
        _low_to_set = cooler_low_bound(temp_to_send, target_temp)
        if self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT:
            _temp_to_set = round(
                TemperatureConverter.convert(
                    temp_to_send,
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
                "target_temp_high": _temp_to_set,
                "target_temp_low": _low_to_set,
            }
        else:
            _payload = {"entity_id": self.cooler_entity_id, "temperature": _temp_to_set}
        # Only prime the send-cache on success. A failed call must not look
        # like a completed send, otherwise the throttle would suppress the
        # retry; its run of failures is recorded instead, which paces the
        # retry without pretending the command arrived. Any exception from
        # this one service call is isolated (cloud integrations propagate raw
        # errors such as ConnectionError) so the hvac_mode command below still
        # runs; CancelledError derives from BaseException and propagates.
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                _payload,
                blocking=True,
                context=self.context,
            )
        except Exception as err:
            _record_cooler_failure(
                last_sent, "temperature", _temp_wanted, now_monotonic
            )
            _LOGGER.warning(
                "better_thermostat %s: set_temperature for cooler %s failed (%s); "
                "will retry on a later cycle",
                self.device_name,
                self.cooler_entity_id,
                err,
            )
        else:
            last_sent["temperature"] = (temp_to_send, now_monotonic)
            last_sent.pop("temperature_failed", None)
            # A fresh send invalidates the settled reading of the channels it
            # carried; the device answers those anew. A single-setpoint
            # payload carries no lower bound, so it says nothing about the
            # bound's settled reading.
            last_sent.pop("temperature_settled", None)
            if _write_range:
                last_sent["target_temp_low"] = (
                    cooler_low_bound(temp_to_send, target_temp),
                    now_monotonic,
                )
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
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.cooler_entity_id, "hvac_mode": desired_mode},
                blocking=True,
                context=self.context,
            )
        except Exception as err:
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


async def control_trv(self, heater_entity_id=None, cycle=None):
    """Control a single TRV by setting temperature, HVAC mode, calibration, and valve position.

    All operations are executed within self._temp_lock to ensure atomic execution when
    multiple TRVs are controlled in parallel. Unavailable TRVs are skipped without
    executing any control operations.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    heater_entity_id : str, optional
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
    # Guard against missing or invalid heater_entity_id
    if not heater_entity_id or heater_entity_id not in self.real_trvs:
        return False

    if not hasattr(self, "task_manager"):
        self.task_manager = TaskManager(hass=self.hass)

    # The suppression flag is owned by the invocation that set it under the
    # lock; a caller cancelled while still waiting for the lock never set it
    # and must not clear it for a concurrent holder mid-write.
    _suppression_owned = False
    try:
        async with self._temp_lock:
            self.real_trvs[heater_entity_id].ignore_trv_states = True
            _suppression_owned = True
            try:
                # Preserve old action for change detection if attributes exist
                if hasattr(self, "attr_hvac_action"):
                    self.old_attr_hvac_action = getattr(self, "attr_hvac_action", None)
                # Recompute current hvac action (uses internal climate logic)
                if hasattr(self, "_compute_hvac_action_pure"):
                    result = self._compute_hvac_action_pure()
                    self._commit_hvac_action(result)
                    self.attr_hvac_action = result.action
            except Exception:
                _LOGGER.debug(
                    "better_thermostat %s: hvac action recompute failed (non critical)",
                    getattr(self, "device_name", "unknown"),
                )
            _trv = self.hass.states.get(heater_entity_id)

            # The cycle decision normally arrives from control_queue; a
            # standalone invocation is its own cycle.
            if cycle is None:
                cycle = compute_control_cycle(self)
            snapshot, desired = cycle
            trv_desired = desired.trvs.get(heater_entity_id)

            # The kernel addresses only reachable TRVs (boost overrides the skip).
            if _trv is None or trv_desired is None:
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s is unavailable, skipping control. "
                    "Control will resume when TRV becomes available.",
                    self.device_name,
                    heater_entity_id,
                )
                _schedule_reachability_retry(self, heater_entity_id)
                _stamp_heartbeat(self)
                return True

            # See get_current_set_temperatures() docstring for why we accept a
            # match on either the single-setpoint or range-low attribute.
            _current_set_temperatures = get_current_set_temperatures(
                self, _trv, "controlling()"
            )

            _remapped_states = convert_outbound_states(
                self, heater_entity_id, self.bt_hvac_mode
            )
            if not isinstance(_remapped_states, dict):
                _LOGGER.warning(
                    "better_thermostat %s: convert_outbound_states returned %r for %s "
                    "(expected dict) — skipping control cycle",
                    self.device_name,
                    _remapped_states,
                    heater_entity_id,
                )
                # The caller backs the retry off; sleeping here would hold
                # the lock and stall every other TRV of this cycle.
                return False

            _temperature = _remapped_states.get("temperature", None)
            _calibration = _remapped_states.get("local_temperature_calibration", None)

            _calibration_mode = self.real_trvs[heater_entity_id].advanced.get(
                "calibration_mode", CalibrationMode.MPC_CALIBRATION
            )
            _calibration_type = self.real_trvs[heater_entity_id].advanced.get(
                "calibration", CalibrationType.TARGET_TEMP_BASED
            )
            # Pair the forced 100 % valve with a max-temp setpoint so the TRV
            # firmware does not fight the valve command.
            if (
                is_boost_heating(snapshot)
                and _calibration_type == CalibrationType.DIRECT_VALVE_BASED
            ):
                _temperature = self.real_trvs[heater_entity_id].max_temp

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
                    heater_entity_id,
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
                        heater_entity_id,
                        _calibration_mode,
                        _calibration_type,
                    )
                if valve_settings is not None:
                    target_pct = int(round(valve_settings.get("valve_percent", 0)))
                    target_pct = int(
                        round(
                            _through_safety_hull(
                                snapshot,
                                heater_entity_id,
                                valve_percent=float(target_pct),
                            ).valve_percent
                            or 0.0
                        )
                    )
                    # Closing the valve (0 %) is the overheat-safe direction
                    # and bypasses the write budget; everything else waits
                    # for the next slot and converges via the next cycle.
                    if _valve_at_target(self, heater_entity_id, target_pct):
                        _LOGGER.debug(
                            "better_thermostat %s: valve of %s already at %s%%, "
                            "skipping write",
                            self.device_name,
                            heater_entity_id,
                            target_pct,
                        )
                    elif _consume_budget(
                        self, heater_entity_id, "valve", bypass=target_pct == 0
                    ):
                        _LOGGER.debug(
                            "better_thermostat %s: TO TRV set_valve: %s to: %s%% (source=%s)",
                            self.device_name,
                            heater_entity_id,
                            target_pct,
                            _source,
                        )
                        ok = await set_valve(self, heater_entity_id, target_pct)
                        if not ok:
                            _LOGGER.debug(
                                "better_thermostat %s: delegate.set_valve returned False (target=%s%%, entity=%s, source=%s)",
                                self.device_name,
                                target_pct,
                                heater_entity_id,
                                _source,
                            )
                            # The budget was already consumed but the valve never
                            # moved; re-derive on the catch-up cycle so the write
                            # is not dropped permanently.
                            _schedule_budget_retry(
                                self,
                                heater_entity_id,
                                _budget_remaining(self, heater_entity_id, "valve"),
                            )
                    else:
                        # A deferred valve write re-derives on the catch-up
                        # cycle; without it the reconciler cannot see the
                        # miss (it compares against the last value written).
                        _schedule_budget_retry(
                            self,
                            heater_entity_id,
                            _budget_remaining(self, heater_entity_id, "valve"),
                        )
                elif _calibration_type != CalibrationType.DIRECT_VALVE_BASED:
                    pass  # non-valve TRV: no valve control expected
            except Exception:
                _LOGGER.debug(
                    "better_thermostat %s: set_valve not applied for %s (unsupported or failed)",
                    self.device_name,
                    heater_entity_id,
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
                and _calibration_type == CalibrationType.DIRECT_VALVE_BASED
            ):
                _LOGGER.debug(
                    "better_thermostat %s: Boost safety override - resetting valve to 0%% because HVAC mode is OFF",
                    self.device_name,
                )
                # Closing the valve is the overheat-safe direction and skips
                # the budget gate, but it is a real write: it passes the
                # safety hull and occupies the budget slot like any other.
                _reset_pct = int(
                    round(
                        _through_safety_hull(
                            snapshot, heater_entity_id, valve_percent=0.0
                        ).valve_percent
                        or 0.0
                    )
                )
                if not _valve_at_target(self, heater_entity_id, _reset_pct):
                    _consume_budget(self, heater_entity_id, "valve", bypass=True)
                    ok = await set_valve(self, heater_entity_id, _reset_pct)
                    if not ok:
                        _LOGGER.debug(
                            "better_thermostat %s: delegate.set_valve returned False for "
                            "safety reset (target=%s%%, entity=%s)",
                            self.device_name,
                            _reset_pct,
                            heater_entity_id,
                        )
                        # The budget slot is stamped but the valve never moved;
                        # re-derive on the catch-up cycle so the reset is not
                        # dropped permanently.
                        _schedule_budget_retry(
                            self,
                            heater_entity_id,
                            _budget_remaining(self, heater_entity_id, "valve"),
                        )

            # Manage TRVs with no HVACMode.OFF
            _trv_has_no_off = _no_off_system_mode(self.real_trvs[heater_entity_id])
            if _trv_has_no_off is True and _new_hvac_mode == HVACMode.OFF:
                _min_temp = self.real_trvs[heater_entity_id].min_temp
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
            _live_trv = self.hass.states.get(heater_entity_id)
            if _live_trv is None or _live_trv.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                _live_trv = _trv
            _reported_hvac_mode = _live_trv.state
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
                    heater_entity_id,
                    _reported_hvac_mode,
                    _new_hvac_mode,
                )
                self.real_trvs[heater_entity_id].last_hvac_mode = _new_hvac_mode
                _tvr_has_quirk = await override_set_hvac_mode(
                    self, heater_entity_id, _new_hvac_mode
                )
                if _tvr_has_quirk is False:
                    await set_hvac_mode(self, heater_entity_id, _new_hvac_mode)
                if self.real_trvs[heater_entity_id].system_mode_received is True:
                    self.real_trvs[heater_entity_id].system_mode_received = False
                    self.task_manager.create_task(
                        check_system_mode(self, heater_entity_id),
                        name=f"bt_check_system_mode_{heater_entity_id}",
                    )

            # set new calibration offset
            if (
                _calibration is not None
                and _new_hvac_mode != HVACMode.OFF
                and _calibration_mode != CalibrationMode.NO_CALIBRATION
            ):
                _current_calibration_s = await get_current_offset(
                    self, heater_entity_id
                )

                if _current_calibration_s is None:
                    _LOGGER.error(
                        "better_thermostat %s: calibration fatal error %s",
                        self.device_name,
                        heater_entity_id,
                    )
                    _stamp_heartbeat(self)
                    return True

                _current_calibration = convert_to_float(
                    str(_current_calibration_s), self.device_name, "controlling()"
                )

                _calibration = float(str(_calibration))
                # Command boundary: the hull owns the device's calibration range.
                # A finite offset goes in and the hull only clamps it to range,
                # so a finite offset comes back out.
                _calibration = _through_safety_hull(
                    snapshot, heater_entity_id, offset=_calibration
                ).offset
                if _calibration is None:
                    _LOGGER.debug(
                        "better_thermostat %s: safety hull yielded no offset for "
                        "%s, skipping calibration write this cycle",
                        self.device_name,
                        heater_entity_id,
                    )

                trv_entry = self.real_trvs[heater_entity_id]
                _offset_tolerance = _calibration_match_tolerance(self, heater_entity_id)

                # COMMAND: what the adapter actually put on the wire. Only
                # that value can be acknowledged; before the first write the
                # device's own report stands in for it.
                _last_sent = trv_entry.last_calibration
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
                if trv_entry.calibration_received is False and _command_confirmed:
                    _LOGGER.debug(
                        "better_thermostat %s: TRV %s device confirms the last "
                        "calibration command (%s), releasing the write gate",
                        self.device_name,
                        heater_entity_id,
                        _last_sent,
                    )
                    trv_entry.calibration_received = True

                if _calibration is not None and trv_entry.calibration_received is True:
                    if _last_sent is None:
                        _LOGGER.debug(
                            "better_thermostat %s: no reference calibration for %s "
                            "yet, skipping calibration write this cycle",
                            self.device_name,
                            heater_entity_id,
                        )
                    else:
                        # INTENT: the value asked for before the adapter's own
                        # clamp. A device resting at a limit it declared keeps
                        # reporting the clamped command, so comparing the
                        # intent against the command would rewrite it every
                        # cycle. Both intent values come off the same step
                        # grid, so they compare exactly; only the report lives
                        # on the device's grid and needs the tolerance.
                        _last_requested = trv_entry.last_calibration_requested
                        if _last_requested is None:
                            _last_requested = _last_sent
                        if float(_last_requested) != _calibration or _command_diverged:
                            # A deferred offset re-derives on the next control cycle
                            # once the slot is free again.
                            if _consume_budget(self, heater_entity_id, "offset"):
                                _LOGGER.debug(
                                    "better_thermostat %s: TO TRV set_local_temperature_calibration: %s from: %s to: %s (device reports %s)",
                                    self.device_name,
                                    heater_entity_id,
                                    _last_sent,
                                    _calibration,
                                    _current_calibration,
                                )
                                if await set_offset(
                                    self, heater_entity_id, _calibration
                                ):
                                    trv_entry.calibration_received = False
                                    self.task_manager.create_task(
                                        check_calibration(self, heater_entity_id),
                                        name=f"bt_check_calibration_{heater_entity_id}",
                                    )
                            else:
                                _schedule_budget_retry(
                                    self,
                                    heater_entity_id,
                                    _budget_remaining(self, heater_entity_id, "offset"),
                                )

            # set new target temperature
            _safety_overrode_setpoint = False
            if _temperature is not None:
                _raw_temperature = float(_temperature)
                _temperature = _through_safety_hull(
                    snapshot, heater_entity_id, setpoint=_raw_temperature
                ).setpoint
                _safety_overrode_setpoint = _temperature != _raw_temperature
            if _temperature is not None and (
                _new_hvac_mode != HVACMode.OFF or _trv_has_no_off
            ):
                # Tolerance-based comparison: the outbound value lies on the
                # device step grid, the read-back values on the 0.01 grid, so
                # exact set membership would re-send identical setpoints.
                if not matches_any_setpoint(_temperature, _current_set_temperatures):
                    trv_entry = self.real_trvs[heater_entity_id]
                    # Safety-relevant writes (frost floor / OFF) bypass the
                    # write budget; everything else waits for the next slot
                    # and converges via the scheduled retry.
                    if _consume_budget(
                        self,
                        heater_entity_id,
                        "setpoint",
                        bypass=_safety_overrode_setpoint
                        or _new_hvac_mode == HVACMode.OFF,
                    ):
                        old = trv_entry.last_temperature
                        _LOGGER.debug(
                            "better_thermostat %s: TO TRV set_temperature: %s from: %s to: %s",
                            self.device_name,
                            heater_entity_id,
                            old,
                            _temperature,
                        )
                        trv_entry.last_temperature = _temperature
                        _tvr_has_quirk = await override_set_temperature(
                            self, heater_entity_id, _temperature
                        )
                        if _tvr_has_quirk is False:
                            await set_temperature(self, heater_entity_id, _temperature)
                        if trv_entry.target_temp_received is True:
                            trv_entry.target_temp_received = False
                            self.task_manager.create_task(
                                check_target_temperature(self, heater_entity_id),
                                name=f"bt_check_target_temp_{heater_entity_id}",
                            )
                    else:
                        # A deferred setpoint re-derives on the catch-up cycle
                        # once the slot is free again. Falling through to the
                        # shared exit keeps the settle sleep outside the lock,
                        # so a deferred TRV does not serialise the others.
                        _schedule_budget_retry(
                            self,
                            heater_entity_id,
                            _budget_remaining(self, heater_entity_id, "setpoint"),
                        )

        # Watchdog heartbeat: the control loop demonstrably ran.
        _stamp_heartbeat(self)

        # Let TRV state updates propagate before accepting new state events
        await asyncio.sleep(3)
        return True
    finally:
        if _suppression_owned:
            self.real_trvs[heater_entity_id].ignore_trv_states = False


async def check_system_mode(self, heater_entity_id=None):
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
    heater_entity_id : str, optional
        Entity ID of the TRV to check

    Returns
    -------
    bool
        Always returns True
    """
    _timeout = 0
    _real_trv = self.real_trvs[heater_entity_id]
    while True:
        _trv_state = self.hass.states.get(heater_entity_id)
        if _trv_state is None or _trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                "better_thermostat %s: %s became unavailable during check_system_mode",
                self.device_name,
                heater_entity_id,
            )
            break
        if _trv_state.state == _real_trv.last_hvac_mode:
            _timeout = 0
            break
        if _timeout > WRITE_CONFIRM_TIMEOUT_S:
            _LOGGER.warning(
                "better_thermostat %s: TRV %s did not confirm the system mode change "
                "after %ss (wrote=%s, last reported=%s); giving up and assuming applied",
                self.device_name,
                heater_entity_id,
                WRITE_CONFIRM_TIMEOUT_S,
                _real_trv.last_hvac_mode,
                _trv_state.state,
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)
    _real_trv.system_mode_received = True
    return True


async def check_target_temperature(self, heater_entity_id=None):
    """Wait for TRV to confirm target temperature change, timeout after 6 minutes.

    Polls the TRV's temperature (and target_temp_low, when range mode is
    supported) attribute every second until either matches last_temperature
    within SETPOINT_MATCH_TOLERANCE or timeout is reached. Sets
    target_temp_received flag when complete.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    heater_entity_id : str, optional
        Entity ID of the TRV to check

    Returns
    -------
    bool
        Always returns True
    """
    _timeout = 0
    _real_trv = self.real_trvs[heater_entity_id]
    while True:
        _trv_state = self.hass.states.get(heater_entity_id)
        if _trv_state is None or _trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                "better_thermostat %s: %s became unavailable during check_target_temperature",
                self.device_name,
                heater_entity_id,
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
                heater_entity_id,
                _real_trv.last_temperature,
                _current_set_temperatures,
            )
        # An empty set (no readable setpoint) is treated as confirmed; a
        # non-empty set is matched with a tolerance because written and
        # read-back setpoints lie on different float rounding grids.
        if not _current_set_temperatures or matches_any_setpoint(
            _real_trv.last_temperature, _current_set_temperatures
        ):
            _timeout = 0
            break
        if _timeout > WRITE_CONFIRM_TIMEOUT_S:
            _LOGGER.warning(
                "better_thermostat %s: TRV %s did not confirm the target temperature "
                "after %ss (wrote=%s, last reported=%s); giving up and assuming applied",
                self.device_name,
                heater_entity_id,
                WRITE_CONFIRM_TIMEOUT_S,
                _real_trv.last_temperature,
                _current_set_temperatures,
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)

    _real_trv.target_temp_received = True
    return True


async def check_calibration(self, heater_entity_id=None):
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

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    heater_entity_id : str, optional
        Entity ID of the TRV to check

    Returns
    -------
    bool
        Always returns True
    """
    _timeout = 0
    _real_trv = self.real_trvs[heater_entity_id]
    _tolerance = _calibration_match_tolerance(self, heater_entity_id)
    while True:
        _trv_state = self.hass.states.get(heater_entity_id)
        if _trv_state is None or _trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                "better_thermostat %s: %s became unavailable during check_calibration",
                self.device_name,
                heater_entity_id,
            )
            break
        _reported = convert_to_float(
            str(await get_current_offset(self, heater_entity_id)),
            self.device_name,
            "check_calibration()",
        )
        if _real_trv.last_calibration is None or (
            _reported is not None
            and abs(_reported - float(_real_trv.last_calibration)) <= _tolerance
        ):
            _timeout = 0
            break
        if _timeout > WRITE_CONFIRM_TIMEOUT_S:
            _LOGGER.warning(
                "better_thermostat %s: TRV %s did not confirm the calibration offset "
                "after %ss (wrote=%s, last reported=%s); giving up and assuming applied",
                self.device_name,
                heater_entity_id,
                WRITE_CONFIRM_TIMEOUT_S,
                _real_trv.last_calibration,
                _reported,
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)
    _real_trv.calibration_received = True
    return True
