"""Controlling module for Better Thermostat."""

import asyncio
from dataclasses import replace
import logging

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
)
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.helpers import (
    attr_to_celsius,
    clamp_valve_percent,
    convert_to_float,
    state_temperature_unit,
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
# Valve deviations below this are the device's own business.
RECONCILE_VALVE_TOLERANCE_PCT = 5.0


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
    return MIN_WRITE_INTERVAL_S - (self.clock.monotonic() - (last or 0.0))


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


def compute_control_cycle(self, *, record: bool = True):
    """Build one consistent observation and decision for a control cycle.

    Records the (snapshot, pre-decide state, desired) tuple in the
    flight recorder — exactly once per cycle. decide() treats its input
    state as immutable; the recorder copies what it stores. Probes (the
    reconciler) pass ``record=False`` to run the same observe-decide
    step without filling the recorder ring.
    """
    snapshot = build_snapshot(self)
    pre_state = self.kernel_state
    desired, self.kernel_state = decide(snapshot, pre_state)
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
    if unit is not None and unit != UnitOfTemperature.CELSIUS:
        step = TemperatureConverter.convert_interval(
            step, unit, UnitOfTemperature.CELSIUS
        )
    # Slack against float noise when the difference is exactly half a step.
    return max(RECONCILE_TOLERANCE_K, step / 2.0 + 1e-6)


def _offset_diverges(self, trv) -> bool:
    """Whether the device's calibration offset left the commanded value.

    Compared only once the device has confirmed the last write — an
    in-flight write is the write path's business, not the reconciler's.
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
    step = trv.local_calibration_step
    tolerance = RECONCILE_TOLERANCE_K
    if step is not None and step > 0:
        tolerance = max(tolerance, step / 2.0 + 1e-6)
    return abs(float(trv.last_calibration) - reported) > tolerance


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
        snapshot, desired = compute_control_cycle(self, record=False)
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
                    # already-pending request.
                    if result is False:
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


async def control_cooler(self, snapshot=None):
    """Control the cooler entity based on current temperature and cooling setpoint.

    Activates cooling when current temperature exceeds target cooling temperature
    minus tolerance and is above heating target. Deactivates cooling when
    temperature drops below cooling target minus tolerance or when BT HVAC mode is OFF.

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
    current_temp = cooler_state.attributes.get("temperature")

    # Determine desired state based on the world snapshot of this cycle
    if snapshot is None:
        snapshot = build_snapshot(self)
    desired_temp = snapshot.target_cooltemp

    if any(
        v is None
        for v in (
            snapshot.room_temp,
            snapshot.target_cooltemp,
            self.tolerance,
            snapshot.target_temp,
        )
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s one or more required values are None "
            "(cur_temp=%s, bt_target_cooltemp=%s, tolerance=%s, bt_target_temp=%s), "
            "defaulting to OFF",
            self.device_name,
            self.cooler_entity_id,
            snapshot.room_temp,
            snapshot.target_cooltemp,
            self.tolerance,
            snapshot.target_temp,
        )
        desired_mode = HVACMode.OFF
    elif snapshot.hvac_mode == HVACMode.OFF:
        desired_mode = HVACMode.OFF
    elif (
        snapshot.room_temp >= snapshot.target_cooltemp - snapshot.tolerance
        and snapshot.room_temp > snapshot.target_temp
    ):
        desired_mode = HVACMode.COOL
    else:
        desired_mode = HVACMode.OFF

    # Only send temperature command if it differs from current
    if current_temp is None or current_temp != desired_temp:
        if current_temp is None:
            _LOGGER.debug(
                "better_thermostat %s: cooler %s current temperature is unknown, "
                "sending set_temperature command anyway",
                self.device_name,
                self.cooler_entity_id,
            )
        else:
            _LOGGER.debug(
                "better_thermostat %s: TO COOLER set_temperature: %s from: %s to: %s",
                self.device_name,
                self.cooler_entity_id,
                current_temp,
                desired_temp,
            )
        _temp_to_set = desired_temp
        if self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT:
            _temp_to_set = TemperatureConverter.convert(
                desired_temp, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
            )
            _temp_to_set = round(_temp_to_set, 1)
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": self.cooler_entity_id, "temperature": _temp_to_set},
            blocking=True,
            context=self.context,
        )

    # Only send hvac_mode command if it differs from current
    if current_hvac_mode != desired_mode:
        _LOGGER.debug(
            "better_thermostat %s: TO COOLER set_hvac_mode: %s from: %s to: %s",
            self.device_name,
            self.cooler_entity_id,
            current_hvac_mode,
            desired_mode,
        )
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self.cooler_entity_id, "hvac_mode": desired_mode},
            blocking=True,
            context=self.context,
        )


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

    async with self._temp_lock:
        self.real_trvs[heater_entity_id].ignore_trv_states = True
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
            self.real_trvs[heater_entity_id].ignore_trv_states = False
            return True

        _current_set_temperature = attr_to_celsius(
            self, _trv, "temperature", None, "controlling()"
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
            await asyncio.sleep(2)
            self.real_trvs[heater_entity_id].ignore_trv_states = False
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
                            snapshot, heater_entity_id, valve_percent=float(target_pct)
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

        # Apply the kernel's intent: a suppression (open window, no heat
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

        # Safety override: if boost mode was active but we forced OFF (window/no-heat),
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
                await set_valve(self, heater_entity_id, _reset_pct)

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

        # send new HVAC mode to TRV, if it changed
        if (
            _new_hvac_mode is not None
            and _new_hvac_mode != _trv.state
            and (
                (_trv_has_no_off is True and _new_hvac_mode != HVACMode.OFF)
                or (_trv_has_no_off is False)
            )
        ):
            _LOGGER.debug(
                "better_thermostat %s: TO TRV set_hvac_mode: %s from: %s to: %s",
                self.device_name,
                heater_entity_id,
                _trv.state,
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
            _current_calibration_s = await get_current_offset(self, heater_entity_id)

            if _current_calibration_s is None:
                _LOGGER.error(
                    "better_thermostat %s: calibration fatal error %s",
                    self.device_name,
                    heater_entity_id,
                )
                _stamp_heartbeat(self)
                self.real_trvs[heater_entity_id].ignore_trv_states = False
                return True

            _current_calibration = convert_to_float(
                str(_current_calibration_s), self.device_name, "controlling()"
            )

            _calibration = float(str(_calibration))
            # Command boundary: the hull owns the device's calibration range.
            _calibration = _through_safety_hull(
                snapshot, heater_entity_id, offset=_calibration
            ).offset

            _old_calibration = self.real_trvs[heater_entity_id].last_calibration
            if _old_calibration is None:
                _old_calibration = _current_calibration

            # If current calibration already matches target, reset calibration_received
            # to avoid it getting stuck at False when the state event was suppressed.
            if (
                self.real_trvs[heater_entity_id].calibration_received is False
                and _current_calibration is not None
                and abs(float(_current_calibration) - float(_calibration)) < 0.5
            ):
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s calibration already at target (%s), "
                    "resetting calibration_received flag",
                    self.device_name,
                    heater_entity_id,
                    _calibration,
                )
                self.real_trvs[heater_entity_id].calibration_received = True

            if self.real_trvs[heater_entity_id].calibration_received is True and float(
                _old_calibration
            ) != float(_calibration):
                # A deferred offset re-derives on the next control cycle
                # once the slot is free again.
                if _consume_budget(self, heater_entity_id, "offset"):
                    _LOGGER.debug(
                        "better_thermostat %s: TO TRV set_local_temperature_calibration: %s from: %s to: %s",
                        self.device_name,
                        heater_entity_id,
                        _old_calibration,
                        _calibration,
                    )
                    await set_offset(self, heater_entity_id, _calibration)
                    self.real_trvs[heater_entity_id].calibration_received = False
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
            if _temperature != _current_set_temperature:
                trv_entry = self.real_trvs[heater_entity_id]
                # Safety-relevant writes (frost floor / OFF) bypass the
                # write budget; everything else waits for the next slot
                # and converges via the scheduled retry.
                if not _consume_budget(
                    self,
                    heater_entity_id,
                    "setpoint",
                    bypass=_safety_overrode_setpoint or _new_hvac_mode == HVACMode.OFF,
                ):
                    _schedule_budget_retry(
                        self,
                        heater_entity_id,
                        _budget_remaining(self, heater_entity_id, "setpoint"),
                    )
                    _stamp_heartbeat(self)
                    await asyncio.sleep(3)
                    self.real_trvs[heater_entity_id].ignore_trv_states = False
                    return True
                old = trv_entry.last_temperature
                _LOGGER.debug(
                    "better_thermostat %s: TO TRV set_temperature: %s from: %s to: %s",
                    self.device_name,
                    heater_entity_id,
                    old,
                    _temperature,
                )
                trv_entry.last_temperature = _temperature
                await set_temperature(self, heater_entity_id, _temperature)
                if self.real_trvs[heater_entity_id].target_temp_received is True:
                    self.real_trvs[heater_entity_id].target_temp_received = False
                    self.task_manager.create_task(
                        check_target_temperature(self, heater_entity_id),
                        name=f"bt_check_target_temp_{heater_entity_id}",
                    )

    # Watchdog heartbeat: the control loop demonstrably ran.
    _stamp_heartbeat(self)

    # Let TRV state updates propagate before accepting new state events
    await asyncio.sleep(3)
    self.real_trvs[heater_entity_id].ignore_trv_states = False
    return True


async def check_system_mode(self, heater_entity_id=None):
    """Wait for TRV to confirm HVAC mode change, timeout after 6 minutes.

    Polls the TRV state every second until hvac_mode matches last_hvac_mode
    or timeout is reached. Sets system_mode_received flag when complete.

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
    while _real_trv.hvac_mode != _real_trv.last_hvac_mode:
        if _timeout > 360:
            _LOGGER.warning(
                "better_thermostat %s: TRV %s did not confirm the system mode change "
                "after 360s (wrote=%s, last reported=%s); giving up and assuming applied",
                self.device_name,
                heater_entity_id,
                _real_trv.last_hvac_mode,
                _real_trv.hvac_mode,
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

    Polls the TRV temperature attribute every second until it matches
    last_temperature or timeout is reached. Sets target_temp_received flag when complete.

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
        _current_set_temperature = attr_to_celsius(
            self, _trv_state, "temperature", None, "check_target_temperature()"
        )
        if _timeout == 0:
            _LOGGER.debug(
                "better_thermostat %s: %s / check_target_temp / _last: %s - _current: %s",
                self.device_name,
                heater_entity_id,
                _real_trv.last_temperature,
                _current_set_temperature,
            )
        if (
            _current_set_temperature is None
            or _real_trv.last_temperature == _current_set_temperature
        ):
            _timeout = 0
            break
        if _timeout > 360:
            _LOGGER.warning(
                "better_thermostat %s: TRV %s did not confirm the target temperature "
                "after 360s (wrote=%s, last reported=%s); giving up and assuming applied",
                self.device_name,
                heater_entity_id,
                _real_trv.last_temperature,
                _current_set_temperature,
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)

    _real_trv.target_temp_received = True
    return True
