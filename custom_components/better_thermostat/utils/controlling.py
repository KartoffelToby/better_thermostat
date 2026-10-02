"""Controlling module for Better Thermostat."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from time import monotonic
from typing import Any

from homeassistant.components.climate.const import PRESET_BOOST, HVACMode
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Context, Event, EventStateChangedData
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import TemperatureConverter

from custom_components.better_thermostat.adapters.delegate import (
    get_current_offset,
    set_hvac_mode,
    set_offset,
    set_temperature,
    set_valve,
)
from custom_components.better_thermostat.events.cooler import cooling_writes_as_held
from custom_components.better_thermostat.events.trv import (
    convert_outbound_states,
    trigger_trv_change,
)
from custom_components.better_thermostat.model_fixes.model_quirks import (
    override_set_hvac_mode,
    override_set_temperature,
    trv_state_unknown_as_available,
)
from custom_components.better_thermostat.utils.const import (
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.helpers import (
    COOLER_SETPOINT_KEYS,
    SETPOINT_MATCH_TOLERANCE,
    TRV_SETPOINT_KEYS,
    attr_to_celsius,
    convert_to_float,
    cooling_owns_dual_role_device,
    device_offers_mode,
    dual_role_entity_id,
    get_current_set_temperatures,
    matches_any_setpoint,
    normalize_step,
    on_cooler_grid,
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
from custom_components.better_thermostat.utils.watcher import (
    UNAVAILABLE_STATES,
    UNKNOWN_STATES,
)

_LOGGER = logging.getLogger(__name__)

# How long a write channel waits for the device to confirm a command before it
# releases the in-flight flag and accepts the next write. Shared by the system
# mode, target temperature and calibration watchdogs so all three channels give
# a slow device the same window.
WRITE_CONFIRM_TIMEOUT_S = 360

# Floor for the commanded-vs-reported offset comparison. One declared offset
# step is the right window only while that step describes the grid the
# device reports on; an adapter declaring a nominal 0.01 K step describes a
# continuous range instead, and any report rounded coarser than that then reads
# as a divergence the write gate re-asserts on every control cycle. The floor
# covers those roundings and stays below the 0.1 K resolution a TRV reports its
# own temperature at, so no calibration error the room can feel hides beneath
# it. The setpoint channel's read-back tolerance describes a different
# comparison, so the offset channel carries its own floor.
OFFSET_MATCH_TOLERANCE_K = 0.05
# Pause before re-queueing a cycle in which a TRV reported failure, so a
# persistently failing device cannot spin the control queue. Each further
# failure of the same cycle doubles the pause.
FAILED_CYCLE_BACKOFF_S = 2.0
# Ceiling of that pause, the period of the periodic control tick. At that
# distance a device that refuses every write costs a few hundred attempts a
# day, while one that starts accepting the command again is still picked up
# within minutes.
FAILED_CYCLE_BACKOFF_MAX_S = 300.0
# Once a run of failed cycles has reached that ceiling it is reported as a
# warning at most this often, with how long it has lasted; the attempts in
# between go to the debug log. A device that refuses for good would otherwise
# leave the same warning every few minutes all day.
FAILED_CYCLE_WARNING_INTERVAL_S = 3600.0


def _is_boost_heating_active(self) -> bool:
    """Check if boost mode is active and heating is needed.

    Returns True when boost preset is active and current temperature
    is below target temperature.
    """
    return (
        self.preset_mode == PRESET_BOOST
        and self.cur_temp is not None
        and self.bt_target_temp is not None
        and self.cur_temp < self.bt_target_temp
    )


def _get_valve_control(
    self, heater_entity_id: str, calibration_mode, calibration_type
) -> tuple[dict | None, str | None]:
    """Determine valve control settings based on boost mode or calibration.

    Returns a tuple of (valve_settings_dict, source_name).
    valve_settings_dict contains 'valve_percent' and 'apply_valve' keys.
    Returns (None, None) if no valve control should be applied.
    """
    # Forcing the valve on a non-direct-valve TRV bypasses the calibration chain
    # and leaves the valve stuck open after boost ends.
    if (
        _is_boost_heating_active(self)
        and calibration_type == CalibrationType.DIRECT_VALVE_BASED
    ):
        _trv = self.real_trvs.get(heater_entity_id)
        max_opening = _trv.valve_max_opening if _trv is not None else 100
        if isinstance(max_opening, (int, float)):
            target_pct = max(0, min(100, int(round(float(max_opening)))))
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


class TaskManager:
    """Manages background asyncio tasks with automatic cleanup.

    Tracks created tasks and automatically removes them from the set when they complete.
    The entity cancels what is still running when it is removed; from then on
    the manager starts nothing.
    """

    def __init__(self, hass=None):
        """Initialize the task manager with an empty task set."""
        self.tasks = set()
        self.hass = hass
        self.closed = False

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

    def cancel_all(self):
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
    ``wait_s`` the pause the run has reached, ``started_at`` when its first
    failure happened, ``warned_at`` when a failure at the ceiling was last
    reported as a warning and ``retry`` the pending re-queue.

    A TRV that starts failing during a run joins it and inherits its pause,
    up to the ceiling, for the retry of its own write. The periodic control
    tick and the device's own reports queue cycles for it anyway, so that
    write is not held back any longer than those already space it.
    """

    intent: tuple[Any, ...]
    failing: frozenset[str]
    reported: frozenset[tuple[str, str]]
    count: int
    wait_s: float
    started_at: float
    warned_at: float | None = None
    retry: asyncio.Task[None] | None = None


def _user_intent(self) -> tuple[Any, ...]:
    """Return the room targets a user sets, as the failure pacing compares them."""
    return (self.bt_target_temp, self.bt_target_cooltemp, self.bt_hvac_mode)


async def _requeue_failed_cycle(self, delay_s: float) -> None:
    """Queue a control cycle once a failed cycle's pause has passed."""
    await asyncio.sleep(delay_s)
    try:
        self.control_queue_task.put_nowait(self)
    except asyncio.QueueFull:
        _LOGGER.debug(
            "better_thermostat %s: control queue is full, discarding task",
            self.device_name,
        )


def _controlled_cleanly(self, entity_id, dispatched) -> bool:
    """Return whether a clean cycle controlled this TRV rather than skipping it.

    A cycle leaves out a TRV the cooling channel drives and skips one that is
    unavailable, and either way reports nothing wrong without having written
    to it.
    """
    if entity_id not in dispatched:
        return False
    return not _skips_unavailable_trv(self, entity_id, self.hass.states.get(entity_id))


def _skips_unavailable_trv(self, entity_id, trv_state) -> bool:
    """Return whether a control cycle leaves this TRV alone as unavailable.

    A TRV whose quirk runs it while its entity reports unknown is written to
    in that state, and a boost that is heating writes even to an unavailable
    one.
    """
    if trv_state is None:
        return True
    unknown_as_available = trv_state_unknown_as_available(self, entity_id)
    return (
        trv_state.state == STATE_UNAVAILABLE
        or (not unknown_as_available and trv_state.state == STATE_UNKNOWN)
    ) and not _is_boost_heating_active(self)


def _pace_failed_cycle(self, run, failures, dispatched):
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
    A clean cycle before the retry fired never tried the refused write again
    and keeps the run, and so does one that skipped a failing TRV as
    unavailable or left it to the cooling channel.
    """
    if not failures:
        if run is None:
            return None
        if run.intent == _user_intent(self) and (
            (run.retry is not None and not run.retry.done())
            or not all(
                _controlled_cleanly(self, entity_id, dispatched)
                for entity_id in run.failing
            )
        ):
            return run
        if run.retry is not None:
            run.retry.cancel()
        return None

    intent = _user_intent(self)
    now = monotonic()
    if run is not None and run.intent == intent:
        count = run.count + 1
        wait_s = min(run.wait_s * 2, FAILED_CYCLE_BACKOFF_MAX_S)
        reported = run.reported
        failing = run.failing | {entity_id for entity_id, _ in failures}
        started_at = run.started_at
        warned_at = run.warned_at
    else:
        count = 1
        wait_s = FAILED_CYCLE_BACKOFF_S
        reported = frozenset()
        failing = frozenset(entity_id for entity_id, _ in failures)
        started_at = now
        warned_at = None
    # At the ceiling the run is reported hourly; below it every failure is.
    at_ceiling = wait_s >= FAILED_CYCLE_BACKOFF_MAX_S
    warn_again = not at_ceiling or (
        warned_at is None or now - warned_at >= FAILED_CYCLE_WARNING_INTERVAL_S
    )
    if run is not None and run.retry is not None:
        run.retry.cancel()

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
                wait_s,
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
                wait_s,
            )
    if at_ceiling and warn_again:
        warned_at = now
    retry = asyncio.create_task(
        _requeue_failed_cycle(self, wait_s),
        name=f"bt_failed_cycle_retry_{self.device_name}",
    )
    return _FailedCycleRun(
        intent, failing, reported, count, wait_s, started_at, warned_at, retry
    )


def advance_hvac_action(self) -> None:
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
            exc_info=True,
        )


def refresh_cached_trv_modes(self) -> None:
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
        if (trv.advanced or {}).get("child_lock"):
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


async def read_reports_held_during_cycle(self) -> None:
    """Read what every TRV reported while the cycle held the handler off.

    A setpoint turned at the device inside a cycle is otherwise read on the
    device's next report, and a cycle that starts before that report writes
    over the turn before anyone has read it. The caller runs this right after
    it releases ``ignore_states`` and before it takes the next cycle, so each
    TRV that reported meanwhile has its current state read by the inbound
    handler as a report of its own. That report replaces the state the first
    held report replaced, so a device that came back from ``unavailable``
    inside the cycle is read as a return, the way the handler reads it outside
    a cycle, and not as a setpoint turned at the device.

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
    device is known to hold, the room temperature the room sensor fallback
    takes from it, or the internal temperature it reported while the cycle
    ran. A turn the room adopts at a target it already had moves only the
    setpoint the device holds, and the cycle is what writes the device's own
    share of that target back over the turn. The handler takes the internal
    temperature as it arrives, as it does outside a cycle, unless it came too
    soon after the previous one; such a reading is taken here once that
    interval has passed, and asks for a cycle all the same. A head switched
    on inside the cycle asks for one as well, as its mode change does outside
    a cycle: the cache already holds the commanded mode, so the report moves
    nothing, yet the setpoint it was not adopted for has to be driven back to
    the room target. A child-locked device holding a setpoint or mode it was
    not sent requests one too, since the cycle is what turns it back. A
    device answering inside every cycle with a report that carries nothing
    new would otherwise keep one cycle following the next.

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
        if (
            state is None
            or state.state == STATE_UNAVAILABLE
            or (
                state.state == STATE_UNKNOWN
                and not trv_state_unknown_as_available(self, entity_id)
            )
        ):
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
        switched_on = prior_hvac_mode == HVACMode.OFF and state.state != HVACMode.OFF
        if (
            temperature_moved
            or switched_on
            or _held_report_control_inputs(self, trv) != acted_on_before
            or _locked_device_moved(self, entity_id, trv, state)
        ):
            try:
                self.control_queue_task.put_nowait(self)
            except asyncio.QueueFull:
                # A cycle is already queued, and it reads the same state.
                pass


def _locked_device_moved(self, entity_id, trv, state) -> bool:
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
    if not (trv.advanced or {}).get("child_lock"):
        return False
    if state.state != STATE_UNKNOWN:
        if cooling_owns_dual_role_device(self, entity_id):
            commanded_mode = self.last_sent_cooler_hvac_mode
        elif trv.system_mode_received:
            commanded_mode = trv.last_hvac_mode
        else:
            commanded_mode = None
        if commanded_mode is not None and state.state != commanded_mode:
            return True
    reported = read_setpoint_celsius(
        self, state, TRV_SETPOINT_KEYS, "read_reports_held_during_cycle()"
    )
    step = normalize_step(trv.target_temp_step or self.bt_target_temp_step)
    known = [trv.last_temperature, trv.confirmed_setpoint, *trv.echo_setpoint_values()]
    if entity_id == dual_role_entity_id(self):
        # The cooling channel's writes as the device holds them, on the grid
        # the cooling channel sends on, the way the inbound handler compares
        # them.
        known += cooling_writes_as_held(self, state)
    known_values = [float(value) for value in known if value is not None]
    if reported is None or not known_values:
        return False
    window = setpoint_echo_window(step)
    return all(abs(reported - value) >= window for value in known_values)


def _held_report_control_inputs(self, trv) -> tuple:
    """Return what a report read at cycle end can move that a cycle acts on."""
    return (
        self.bt_target_temp,
        self.bt_target_cooltemp,
        self.bt_hvac_mode,
        trv.hvac_mode,
        trv.confirmed_setpoint,
        self.last_sent_cooler_temp,
        trv.current_temperature,
        self.cur_temp,
    )


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

    failed_run = None
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

                        # Handle cooler logic once per cycle. A cooler that is also
                        # a TRV still awaiting its initialisation joins the cycles
                        # only once that is done, like the heating channel.
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
                                await control_cooler(self)
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
                        for trv, trv_data in self.real_trvs.items():
                            # A TRV startup went ahead without carries none of
                            # the values a control decision reads, and boost
                            # would address it even while it is unreachable.
                            if trv_data.awaiting_initialization:
                                continue
                            if _cooling_owns_shared and trv == _shared_entity_id:
                                _LOGGER.debug(
                                    "better_thermostat %s: %s is driven by the cooling "
                                    "channel this cycle, leaving the heating channel out",
                                    self.device_name,
                                    trv,
                                )
                                continue
                            controlled_trvs.append(trv)
                            tasks.append(control_trv(self, trv))

                        if _cooling_owns_shared and not tasks:
                            # The heating action and its hysteresis are advanced
                            # inside control_trv, so a cycle whose only device went
                            # to the cooling channel advances them here instead of
                            # leaving the band on the state the previous cycle left.
                            advance_hvac_action(self)

                        # Run all TRV controls in parallel
                        results = await asyncio.gather(*tasks, return_exceptions=True)

                        failures = [
                            (controlled_trvs[i], res)
                            for i, res in enumerate(results)
                            if isinstance(res, Exception) or res is False
                        ]

                        # Retry the cycle if some TRVs failed. The backoff sits
                        # here rather than in the failing worker: a worker holds
                        # the TRV lock and would stall the rest of the cycle with
                        # it.
                        failed_run = _pace_failed_cycle(
                            self, failed_run, failures, controlled_trvs
                        )

                        if not getattr(self, "in_maintenance", False):
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
                    # carries no cycle and one whose handling is cancelled, so a
                    # later join() does not wait on an item nobody finishes.
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
        if not getattr(self, "in_maintenance", False):
            self.ignore_states = False


def _device_setpoint_tolerance(self, state) -> float:
    """Return the tolerance for comparing a commanded setpoint to a reported one.

    A device snaps a written setpoint onto its own step grid, so a snapped
    value sits at most half a step away from the value that was commanded.
    SETPOINT_MATCH_TOLERANCE is the floor: it covers the read-back grid for
    devices that report no usable step.
    """
    raw_step = state.attributes.get("target_temp_step")
    step = (
        convert_to_float(str(raw_step), self.device_name, "control_cooler()")
        if raw_step is not None
        else None
    )
    if step is None or step <= 0:
        return SETPOINT_MATCH_TOLERANCE
    if (
        state_temperature_unit(
            state.attributes, self.hass.config.units.temperature_unit
        )
        == UnitOfTemperature.FAHRENHEIT
    ):
        step = round(step * 5.0 / 9.0, 4)
    # Slack against float noise when the difference is exactly half a step.
    return max(SETPOINT_MATCH_TOLERANCE, step / 2.0 + 1e-6)


def _calibration_match_tolerance(self, entity_id) -> float:
    """Return the tolerance for comparing a written offset to a reported one.

    A written offset travels to the device as a count of its step, and the
    ZHA number platform truncates onto that grid (``int(value / step)``): a
    written 6.3 on a 0.1 K step becomes 62 counts and is reported as 6.2. A
    device that truncates on its own grid does the same. The truncated count
    lands one step nearer zero than the command, so a report within one step
    of the command is the command or its truncated neighbour.
    OFFSET_MATCH_TOLERANCE_K is the floor: it covers devices that report no
    usable step and those whose declared step is finer than the grid they
    actually report on.

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


async def control_cooler(self):
    """Control the cooler entity based on current temperature and cooling setpoint.

    Activates cooling when the current temperature reaches the cooling target plus
    tolerance and is above the heating target, so a configured tolerance delays the
    switch-on instead of running the room below the cooling target. Deactivates
    cooling when the temperature falls back below the cooling target — or below the
    cooling target minus the width ``COOLER_MODE_HYSTERESIS_K`` borrows from
    underneath it whenever the tolerance is narrower than that minimum band — or
    when BT HVAC mode is OFF, or while a window or door contact is open.
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
    # Resolve the cooler's reported setpoint to Celsius before comparing it
    # against the Celsius desired value; on a Fahrenheit system the raw
    # attribute would never match and defeat the redundant-send dedup.
    current_temp = read_setpoint_celsius(
        self, cooler_state, COOLER_SETPOINT_KEYS, "control_cooler()"
    )

    # A cooler that only advertises the range feature rejects a "temperature"
    # payload with a ServiceValidationError, so it never receives a setpoint.
    # Devices that advertise neither bit use the single-setpoint payload.
    # A cooler advertising both publishes the channel it does not drive as
    # None, so the write follows the channel the reading came from: writing
    # the other one leaves the two sides permanently out of sync.
    _write_range = supports_temperature_range(cooler_state) and (
        not supports_single_target_temperature(cooler_state)
        or (
            cooler_state.attributes.get("temperature") is None
            and cooler_state.attributes.get("target_temp_high") is not None
        )
    )

    min_resend_interval_s = self.min_cooler_resend_interval_s
    now_ts = monotonic()

    # Determine desired state based on current conditions. The cooler holds
    # setpoints on its own grid only, so the command is the cooling target
    # rounded once onto that grid; every comparison below and the send cache
    # work with the value the device is actually sent.
    desired_temp = self.bt_target_cooltemp
    if isinstance(desired_temp, (int, float)):
        desired_temp = on_cooler_grid(self, cooler_state, float(desired_temp))

    # A range write needs both bounds, and Home Assistant rejects a low bound
    # above the high one. The heating target is the natural lower bound; it can
    # only exceed the cooling target while the two are out of sync, so it is
    # capped at the value being written.
    _low_to_set = desired_temp
    if (
        _write_range
        and desired_temp is not None
        and isinstance(self.bt_target_temp, (int, float))
    ):
        _low_to_set = min(
            on_cooler_grid(self, cooler_state, float(self.bt_target_temp)), desired_temp
        )

    if any(
        v is None
        for v in (
            self.cur_temp,
            self.bt_target_cooltemp,
            self.tolerance,
            self.bt_target_temp,
        )
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s one or more required values are None "
            "(cur_temp=%s, bt_target_cooltemp=%s, tolerance=%s, bt_target_temp=%s), "
            "defaulting to OFF",
            self.device_name,
            self.cooler_entity_id,
            self.cur_temp,
            self.bt_target_cooltemp,
            self.tolerance,
            self.bt_target_temp,
        )
        desired_mode = HVACMode.OFF
    elif self.bt_hvac_mode == HVACMode.OFF:
        desired_mode = HVACMode.OFF
    elif self.contact_open:
        # An open window or door suppresses the cooler for the same reason it
        # suppresses the TRVs: the room cannot reach its target, so the unit
        # would run against an unbounded load. On a device that carries both
        # roles the decision is what hands it over, so a cooler left running
        # here would also keep the heating channel — the only channel that
        # switches a device off for an open contact — out of the cycle.
        desired_mode = HVACMode.OFF
    else:
        # The tolerance delays the switch-on instead of advancing it: cooling
        # starts only once the room reaches cool_target + tolerance and then
        # runs down to cool_target, so the room settles at or above the cooling
        # target rather than below it. A band narrower than
        # COOLER_MODE_HYSTERESIS_K takes the missing width from below the
        # target, because a room resting on an edge would otherwise flip the
        # decision — and with it the write — on every cycle; the switch-on edge
        # never moves for that guard, which buys decision stability and not a
        # colder room. The heating target stays a hard floor: cooling below it
        # would fight the TRVs.
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
        _previously_cooling = (
            self.last_cooler_mode_decided == HVACMode.COOL
            if self.last_cooler_mode_decided is not None
            else current_hvac_mode == HVACMode.COOL
        )
        _cool_wanted = should_cool_with_tolerance(
            self.cur_temp,
            self.bt_target_cooltemp,
            self.tolerance,
            _previously_cooling,
            min_band=COOLER_MODE_HYSTERESIS_K,
        )
        if _cool_wanted and self.cur_temp > self.bt_target_temp:
            desired_mode = HVACMode.COOL
        else:
            desired_mode = HVACMode.OFF

    # The band's state is the decision, not the send: last_sent_cooler_hvac_mode
    # is written only after a successful service call, so a device that rejects
    # every command would keep the hold edge unreachable and make the decision
    # flap between the two commands the device is refusing. Recorded for every
    # branch above, so a cycle that fell into a guard leaves the band where that
    # guard put it instead of on a stale value. Each of those guards stops the
    # cooler outright, so the run ends there and the way back in is the
    # switch-on edge — the same contract the heating hysteresis in
    # compute_hvac_action applies to the same three guards, where a missing
    # reading, an open contact and a mode of OFF each return the band to IDLE.
    self.last_cooler_mode_decided = desired_mode

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
        # so the send-cache no longer describes what the device holds. Dropping
        # the timestamps keeps the resend throttle from suppressing the first
        # write of the next cooling period as a repeat of one the heating
        # channel has since replaced.
        self.last_sent_cooler_temp_ts = None
        self.last_sent_cooler_hvac_mode_ts = None
        return

    if _shared_entity_id is not None:
        # The heating channel's pending confirmations wait on a setpoint and a
        # mode this cycle supersedes, and while they are outstanding the
        # inbound handler declines every reported setpoint as unconfirmed.
        # Taking the device over releases them.
        _shared_trv = self.real_trvs[_shared_entity_id]
        _shared_trv.target_temp_received = True
        _shared_trv.system_mode_received = True

    # Decide whether a temperature command is needed. When the current
    # temperature is unknown, only send if the desired value changed since the
    # last successful command; otherwise send when it differs from current.
    # The reported value comes back on convert_to_float's 0.01 grid, on a
    # Fahrenheit system through a unit conversion on top, and snapped onto the
    # device's own step grid, so both bounds are compared with the tolerance
    # that grid allows.
    _match_tolerance = _device_setpoint_tolerance(self, cooler_state)
    temp_changed_since_last_send = self.last_sent_cooler_temp != desired_temp
    should_send_temp = False
    if desired_temp is None:
        _LOGGER.debug(
            "better_thermostat %s: cooler %s desired temperature is None, "
            "skipping set_temperature",
            self.device_name,
            self.cooler_entity_id,
        )
    elif current_temp is None:
        should_send_temp = temp_changed_since_last_send
        if not should_send_temp:
            _LOGGER.debug(
                "better_thermostat %s: cooler %s current temperature unknown and "
                "desired temperature unchanged (%s), skipping set_temperature",
                self.device_name,
                self.cooler_entity_id,
                desired_temp,
            )
    elif not matches_any_setpoint(current_temp, {desired_temp}, _match_tolerance):
        should_send_temp = True

    # A range write carries both bounds, so a lower bound that drifted away
    # from the heating target needs a send of its own: the cooling target can
    # stay unchanged for as long as the user only moves the heating side.
    _low_bound_drifted = False
    if _write_range and desired_temp is not None and not should_send_temp:
        current_low = attr_to_celsius(
            self, cooler_state, "target_temp_low", None, "control_cooler()"
        )
        # The reported bound comes back on the device's grid while _low_to_set
        # is BT's raw value, so the two are compared with the same tolerance
        # the upper bound uses.
        if current_low is not None and not matches_any_setpoint(
            current_low, {_low_to_set}, _match_tolerance
        ):
            _LOGGER.debug(
                "better_thermostat %s: cooler %s lower bound %s differs from %s, "
                "sending both bounds",
                self.device_name,
                self.cooler_entity_id,
                current_low,
                _low_to_set,
            )
            should_send_temp = True
            _low_bound_drifted = True

    # Throttle identical resends when the state feedback lags behind.
    # last_sent_cooler_temp tracks the upper bound alone, so a send armed by
    # the lower bound carries a payload that was never written before and is
    # not a resend.
    if (
        should_send_temp
        and min_resend_interval_s > 0
        and not temp_changed_since_last_send
        and not _low_bound_drifted
    ):
        last_temp_ts = self.last_sent_cooler_temp_ts
        if last_temp_ts is not None and (now_ts - last_temp_ts) < min_resend_interval_s:
            _LOGGER.debug(
                "better_thermostat %s: cooler %s skipping set_temperature due to "
                "min_cooler_resend_interval_s=%ss",
                self.device_name,
                self.cooler_entity_id,
                min_resend_interval_s,
            )
            should_send_temp = False

    # An open contact suppresses the temperature channel alongside the mode,
    # the way a suppressed TRV receives a mode command and no setpoint. The
    # unit is held OFF and converges on nothing, so a setpoint written now
    # would only overwrite whatever the user turned its own dial to. Nothing
    # is attempted, so the send cache records nothing either, and the channel
    # resumes on the cycle the contact shuts.
    if should_send_temp and self.contact_open:
        _LOGGER.debug(
            "better_thermostat %s: cooler %s suppressed by an open contact, "
            "skipping set_temperature",
            self.device_name,
            self.cooler_entity_id,
        )
        should_send_temp = False

    if should_send_temp:
        _LOGGER.debug(
            "better_thermostat %s: TO COOLER set_temperature: %s from: %s to: %s",
            self.device_name,
            self.cooler_entity_id,
            current_temp,
            desired_temp,
        )
        _temp_to_set = desired_temp
        if self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT:
            _temp_to_set = round(
                TemperatureConverter.convert(
                    desired_temp,
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
        # The device can report the write back while the call is still in
        # flight, so the value is recorded as sent before the call goes out.
        # A failed call must not look like a completed send, otherwise the
        # nil-guard would suppress the retry, so a failure puts the previous
        # record back.
        _previous_send = (self.last_sent_cooler_temp, self.last_sent_cooler_temp_ts)
        self.last_sent_cooler_temp = desired_temp
        self.last_sent_cooler_temp_ts = now_ts
        _sent = False
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                _payload,
                blocking=True,
                context=self.context,
            )
            _sent = True
        except HomeAssistantError as err:
            _LOGGER.warning(
                "better_thermostat %s: set_temperature for cooler %s failed (%s); "
                "will retry on the next cycle",
                self.device_name,
                self.cooler_entity_id,
                err,
            )
        finally:
            if not _sent:
                self.last_sent_cooler_temp, self.last_sent_cooler_temp_ts = (
                    _previous_send
                )

    # Decide whether an hvac_mode command is needed, throttling identical
    # resends the same way as temperature commands.
    mode_changed_since_last_send = self.last_sent_cooler_hvac_mode != desired_mode
    should_send_mode = current_hvac_mode != desired_mode

    if (
        should_send_mode
        and min_resend_interval_s > 0
        and not mode_changed_since_last_send
    ):
        last_mode_ts = self.last_sent_cooler_hvac_mode_ts
        if last_mode_ts is not None and (now_ts - last_mode_ts) < min_resend_interval_s:
            _LOGGER.debug(
                "better_thermostat %s: cooler %s skipping set_hvac_mode due to "
                "min_cooler_resend_interval_s=%ss",
                self.device_name,
                self.cooler_entity_id,
                min_resend_interval_s,
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
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.cooler_entity_id, "hvac_mode": desired_mode},
                blocking=True,
                context=self.context,
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "better_thermostat %s: set_hvac_mode for cooler %s failed (%s); "
                "will retry on the next cycle",
                self.device_name,
                self.cooler_entity_id,
                err,
            )
        else:
            self.last_sent_cooler_hvac_mode = desired_mode
            self.last_sent_cooler_hvac_mode_ts = now_ts


async def control_trv(self, heater_entity_id=None):
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
        True if control succeeded or the TRV was skipped (unavailable, or no
        current calibration offset could be read).
        False if heater_entity_id is missing or not in real_trvs, if
        convert_outbound_states did not return a dict, if a valve write
        (the regular valve position or the boost safety reset to 0 %) spent
        its attempts, or if the TRV refused the mode. The remaining writes of
        the cycle still go out in the last two cases. control_queue counts a
        False result, like a raised exception, as a failed cycle and retries
        it after its backoff.
    """
    # Guard against missing or invalid heater_entity_id
    if not heater_entity_id or heater_entity_id not in self.real_trvs:
        return False

    if not hasattr(self, "task_manager"):
        self.task_manager = TaskManager(hass=self.hass)

    # A valve write that spent its attempts or a refused mode fails the
    # cycle, so the queue retries it after its backoff instead of leaving the
    # device where it was until some later event.
    _valve_write_failed = False
    _mode_refused = False
    async with self._temp_lock:
        self.real_trvs[heater_entity_id].ignore_trv_states = True
        try:
            advance_hvac_action(self)
            _trv = self.hass.states.get(heater_entity_id)
            # Check if TRV is available before attempting to control it
            if _skips_unavailable_trv(self, heater_entity_id, _trv):
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s is unavailable, skipping control. "
                    "Control will resume when TRV becomes available.",
                    self.device_name,
                    heater_entity_id,
                )
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
                return False

            _temperature = _remapped_states.get("temperature", None)
            _calibration = _remapped_states.get("local_temperature_calibration", None)
            _calibration_mode = self.real_trvs[heater_entity_id].advanced.get(
                "calibration_mode", DEFAULT_CALIBRATION_MODE
            )
            _calibration_type = self.real_trvs[heater_entity_id].advanced.get(
                "calibration", CalibrationType.TARGET_TEMP_BASED
            )
            # Pair the forced 100 % valve with a max-temp setpoint so the TRV
            # firmware does not fight the valve command.
            if (
                _is_boost_heating_active(self)
                and _calibration_type == CalibrationType.DIRECT_VALVE_BASED
            ):
                _temperature = self.real_trvs[heater_entity_id].max_temp

            # Optional: set valve position if supported (e.g., MQTT/Z2M)
            try:
                valve_settings, _source = _get_valve_control(
                    self, heater_entity_id, _calibration_mode, _calibration_type
                )
                if valve_settings is not None:
                    target_pct = int(round(valve_settings.get("valve_percent", 0)))
                    _LOGGER.debug(
                        "better_thermostat %s: TO TRV set_valve: %s to: %s%% (source=%s)",
                        self.device_name,
                        heater_entity_id,
                        target_pct,
                        _source,
                    )
                    ok = await set_valve(self, heater_entity_id, target_pct)
                    if ok is False:
                        _valve_write_failed = True
                    elif not ok:
                        _LOGGER.debug(
                            "better_thermostat %s: no valve channel took %s%% for %s (source=%s)",
                            self.device_name,
                            target_pct,
                            heater_entity_id,
                            _source,
                        )
                elif _calibration_type != CalibrationType.DIRECT_VALVE_BASED:
                    pass  # non-valve TRV: no valve control expected
            except Exception:
                _LOGGER.debug(
                    "better_thermostat %s: set_valve not applied for %s (unsupported or failed)",
                    self.device_name,
                    heater_entity_id,
                )

            _new_hvac_mode = handle_contact_open(self, _remapped_states)

            # if we don't need to heat, we force HVACMode to be off
            if self.call_for_heat is False:
                _new_hvac_mode = HVACMode.OFF

            # Safety override: if boost mode was active but we forced OFF (open contact/no-heat),
            # ensure valve is reset to 0% to prevent overheating. Only direct-valve
            # calibration types accept valve commands; LOCAL_BASED and
            # TARGET_TEMP_BASED control via offset / setpoint instead.
            if (
                _is_boost_heating_active(self)
                and _new_hvac_mode == HVACMode.OFF
                and _calibration_type == CalibrationType.DIRECT_VALVE_BASED
            ):
                _LOGGER.debug(
                    "better_thermostat %s: Boost safety override - resetting valve to 0%% because HVAC mode is OFF",
                    self.device_name,
                )
                if await set_valve(self, heater_entity_id, 0) is False:
                    _valve_write_failed = True

            # Manage TRVs with no HVACMode.OFF
            # The cache holds the device's own spelling, so whether it offers OFF
            # is decided on the normalized list, like every other capability
            # check. An unreported list counts as no-off.
            _hvac_modes = self.real_trvs[heater_entity_id].hvac_modes or []
            _offers_off = device_offers_mode(_hvac_modes, HVACMode.OFF)
            _no_off_system_mode = not _offers_off or (
                self.real_trvs[heater_entity_id].advanced.get(
                    "no_off_system_mode", False
                )
                is True
            )
            if _no_off_system_mode is True and _new_hvac_mode == HVACMode.OFF:
                _min_temp = self.real_trvs[heater_entity_id].min_temp
                _LOGGER.debug(
                    "better_thermostat %s: sending %s°C to the TRV because this device has no system mode off and heater should be off",
                    self.device_name,
                    _min_temp,
                )
                _temperature = _min_temp

            _mode_trv = self.real_trvs[heater_entity_id]
            if (
                _new_hvac_mode is not None
                and _new_hvac_mode == _trv.state
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
                        monotonic() + WRITE_CONFIRM_TIMEOUT_S
                    )
                _mode_trv.last_hvac_mode = _new_hvac_mode

            # send new HVAC mode to TRV, if it changed
            if (
                _new_hvac_mode is not None
                and _new_hvac_mode != _trv.state
                and (
                    (_no_off_system_mode is True and _new_hvac_mode != HVACMode.OFF)
                    or (_no_off_system_mode is False)
                )
            ):
                _LOGGER.debug(
                    "better_thermostat %s: TO TRV set_hvac_mode: %s from: %s to: %s",
                    self.device_name,
                    heater_entity_id,
                    _trv.state,
                    _new_hvac_mode,
                )
                _commanded_before = self.real_trvs[heater_entity_id].last_hvac_mode
                self.real_trvs[heater_entity_id].last_hvac_mode = _new_hvac_mode
                self.real_trvs[heater_entity_id].withdrawn_hvac_mode = None
                self.real_trvs[heater_entity_id].withdrawn_hvac_mode_until = None
                _tvr_has_quirk = await override_set_hvac_mode(
                    self, heater_entity_id, _new_hvac_mode
                )
                if _tvr_has_quirk is False:
                    _mode_refused = (
                        await set_hvac_mode(self, heater_entity_id, _new_hvac_mode)
                        is False
                    )
                # A refused mode fails the cycle, and the cycle the queue
                # retries after its backoff still finds the device in its old
                # mode and writes it again; there is nothing to wait for until
                # then. Until it goes through, the device holds the mode it
                # reports, and that is the mode last commanded as far as the
                # mode cache and the inbound handler are concerned:
                # the refused one would read the device's next plain report
                # as a press back to its old mode.
                if _mode_refused:
                    self.real_trvs[heater_entity_id].last_hvac_mode = (
                        _commanded_before
                        if _trv.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                        else _trv.state
                    )
                if (
                    not _mode_refused
                    and self.real_trvs[heater_entity_id].system_mode_received is True
                ):
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
                    return True

                _current_calibration = convert_to_float(
                    str(_current_calibration_s), self.device_name, "controlling()"
                )

                _calibration = float(str(_calibration))

                trv_entry = self.real_trvs[heater_entity_id]
                _offset_tolerance = _calibration_match_tolerance(self, heater_entity_id)

                # The offset channel carries three values: the INTENT this
                # cycle computed (_calibration), the COMMAND the adapter put on
                # the wire after its own clamp (last_calibration), and the
                # REPORT the device publishes (_current_calibration). A write
                # goes out when the intent moved away from the last requested
                # value, or when the report diverged from the command. A report
                # that equals a clamped command means the device sits at a limit
                # it declared, and that is not a reason to write.
                _last_sent = trv_entry.last_calibration
                if _last_sent is None:
                    _last_sent = _current_calibration

                # An unreadable report neither confirms nor diverges.
                _report_readable = (
                    _current_calibration is not None and _last_sent is not None
                )
                _command_diverged = _report_readable and (
                    abs(float(_current_calibration) - float(_last_sent))
                    > _offset_tolerance
                )
                _command_confirmed = _report_readable and not _command_diverged

                if trv_entry.calibration_received is False and _command_confirmed:
                    _LOGGER.debug(
                        "better_thermostat %s: TRV %s device confirms the last "
                        "calibration command (%s), resetting calibration_received flag",
                        self.device_name,
                        heater_entity_id,
                        _last_sent,
                    )
                    trv_entry.calibration_received = True

                if trv_entry.calibration_received is True:
                    if _last_sent is None:
                        _LOGGER.debug(
                            "better_thermostat %s: no reference calibration for %s yet, "
                            "skipping calibration write this cycle",
                            self.device_name,
                            heater_entity_id,
                        )
                    else:
                        # Intent and last requested value come off the same
                        # step grid, so they compare exactly; the tolerance
                        # belongs where the two different grids of command and
                        # report meet.
                        _last_requested = trv_entry.last_calibration_requested
                        if _last_requested is None:
                            _last_requested = _last_sent
                        if float(_last_requested) != _calibration or _command_diverged:
                            _LOGGER.debug(
                                "better_thermostat %s: TO TRV "
                                "set_local_temperature_calibration: %s from: %s to: %s "
                                "(device reports %s)",
                                self.device_name,
                                heater_entity_id,
                                _last_sent,
                                _calibration,
                                _current_calibration,
                            )
                            if await set_offset(self, heater_entity_id, _calibration):
                                trv_entry.calibration_received = False
                                trv_entry.calibration_write_generation += 1
                                self.task_manager.create_task(
                                    check_calibration(
                                        self,
                                        heater_entity_id,
                                        trv_entry.calibration_write_generation,
                                    ),
                                    name=f"bt_check_calibration_{heater_entity_id}",
                                )

            # set new target temperature
            if _temperature is not None and (
                _new_hvac_mode != HVACMode.OFF or _no_off_system_mode
            ):
                # Tolerance-based comparison: the outbound value lies on the
                # device step grid, the read-back values on the 0.01 grid, so
                # exact set membership would re-send identical setpoints.
                if not matches_any_setpoint(_temperature, _current_set_temperatures):
                    old = self.real_trvs[heater_entity_id].last_temperature
                    _LOGGER.debug(
                        "better_thermostat %s: TO TRV set_temperature: %s from: %s to: %s",
                        self.device_name,
                        heater_entity_id,
                        old,
                        _temperature,
                    )
                    self.real_trvs[heater_entity_id].last_temperature = _temperature
                    self.real_trvs[heater_entity_id].remember_setpoint_written(
                        _temperature
                    )
                    try:
                        _tvr_has_quirk = await override_set_temperature(
                            self, heater_entity_id, _temperature
                        )
                        if _tvr_has_quirk is False:
                            await set_temperature(self, heater_entity_id, _temperature)
                    finally:
                        # The delegate stores the value it put on the wire, which
                        # its rounding and clamping may have moved off the
                        # intent; the device echoes that value, so it is
                        # remembered as well, a failed call included: the device
                        # may have taken the value anyway. Every write is watched
                        # on its own for the same reason, and a watchdog still
                        # waiting on an earlier write steps aside for this one
                        # rather than holding the channel for a command the
                        # device may never report.
                        _written_trv = self.real_trvs[heater_entity_id]
                        if _written_trv.last_temperature is not None:
                            _written_trv.remember_setpoint_written(
                                _written_trv.last_temperature
                            )
                        _written_trv.target_temp_received = False
                        self.task_manager.create_task(
                            check_target_temperature(
                                self,
                                heater_entity_id,
                                _written_trv.last_setpoint_write_id,
                                _written_trv.last_temperature,
                            ),
                            name=f"bt_check_target_temp_{heater_entity_id}",
                        )
                else:
                    # The device already holds what the room wants, whoever
                    # put it there: a knob turned while the room was off can
                    # land on the setpoint the room asks for once it heats
                    # again. That value is BT's own from here on.
                    self.real_trvs[heater_entity_id].remember_setpoint_held(
                        _temperature
                    )

            # Let TRV state updates propagate before accepting new state events
            await asyncio.sleep(3)
        finally:
            self.real_trvs[heater_entity_id].ignore_trv_states = False
    return not (_valve_write_failed or _mode_refused)


def handle_contact_open(self, _remapped_states):
    """Override HVAC mode to OFF when a window or door contact is open.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    _remapped_states : dict
        Dictionary containing remapped TRV states including system_mode

    Returns
    -------
    HVACMode
        HVACMode.OFF if a window or door is open, otherwise the remapped
        system_mode
    """
    if self.contact_open:
        return HVACMode.OFF
    return _remapped_states.get("system_mode", None)


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
    _awaited_setpoint = _real_trv.last_temperature
    _awaited_write_id = _real_trv.last_setpoint_write_id
    state_unknown_as_available = trv_state_unknown_as_available(self, heater_entity_id)
    while True:
        _trv_state = self.hass.states.get(heater_entity_id)
        if (
            _trv_state is None
            or _trv_state.state == STATE_UNAVAILABLE
            or ((not state_unknown_as_available) and _trv_state.state == STATE_UNKNOWN)
        ):
            _LOGGER.debug(
                "better_thermostat %s: %s became unavailable during check_system_mode",
                self.device_name,
                heater_entity_id,
            )
            break
        if (
            _trv_state.state == STATE_UNKNOWN and state_unknown_as_available
        ) or _trv_state.state == _real_trv.last_hvac_mode:
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


async def check_target_temperature(
    self, heater_entity_id: str, write_id: int, setpoint: float | None
):
    """Wait for TRV to confirm target temperature change, timeout after 6 minutes.

    Polls the TRV's temperature (and target_temp_low, when range mode is
    supported) attribute every second until either matches the awaited
    command within SETPOINT_MATCH_TOLERANCE or timeout is reached. Sets
    target_temp_received flag when complete. The command is fixed when the
    watchdog is started: valve maintenance writes through the same delegate
    and moves ``last_temperature`` on without going through the control
    path, so a maintenance value must not be able to confirm a control
    write. The id that command went out under is fixed with it, so the
    confirmation retires that write and the ones before it and leaves
    anything written while the wait ran. A timeout or an unreadable setpoint
    retires nothing, since the device may still hold any of them.

    Each control write starts a watchdog of its own. Once a newer write has
    gone out, this one no longer speaks for the channel: it still records a
    report of its own command as confirmed, but otherwise ends without
    waiting for the timeout, and only the watchdog of the newest write
    releases ``target_temp_received``.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    heater_entity_id : str
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
    _real_trv = self.real_trvs[heater_entity_id]
    _awaited_setpoint = setpoint
    _awaited_write_id = write_id
    state_unknown_as_available = trv_state_unknown_as_available(self, heater_entity_id)
    while True:
        _trv_state = self.hass.states.get(heater_entity_id)
        if (
            _trv_state is None
            or _trv_state.state == STATE_UNAVAILABLE
            or ((not state_unknown_as_available) and _trv_state.state == STATE_UNKNOWN)
        ):
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
                _awaited_setpoint,
                _current_set_temperatures,
            )
        # An empty set (no readable setpoint) ends the wait without a
        # confirmation, so the writes the device may still hold stay
        # remembered. A non-empty set is matched with a tolerance because
        # written and read-back setpoints lie on different float rounding
        # grids.
        if not _current_set_temperatures:
            _timeout = 0
            break
        if matches_any_setpoint(_awaited_setpoint, _current_set_temperatures):
            _real_trv.remember_setpoint_confirmed(_awaited_setpoint, _awaited_write_id)
            _timeout = 0
            break
        if _real_trv.last_setpoint_write_id != _awaited_write_id:
            _LOGGER.debug(
                "better_thermostat %s: a newer setpoint write superseded the one "
                "%s was being watched for, leaving the channel to its watchdog",
                self.device_name,
                heater_entity_id,
            )
            return True
        if _timeout > WRITE_CONFIRM_TIMEOUT_S:
            _LOGGER.warning(
                "better_thermostat %s: TRV %s did not confirm the target temperature "
                "after %ss (wrote=%s, last reported=%s); giving up and assuming applied",
                self.device_name,
                heater_entity_id,
                WRITE_CONFIRM_TIMEOUT_S,
                _awaited_setpoint,
                _current_set_temperatures,
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)

    if _real_trv.last_setpoint_write_id == _awaited_write_id:
        _real_trv.target_temp_received = True
    return True


async def check_calibration(self, heater_entity_id=None, generation=0):
    """Wait for TRV to confirm the calibration offset, timeout after 6 minutes.

    Polls the TRV's reported offset every second until it matches
    last_calibration within the tolerance the device's own offset step allows,
    or the timeout is reached. Sets calibration_received when complete, which
    is what releases the write gate for the next offset command; without it a
    device that never acknowledges an offset wedges the channel.

    The reported value is deliberately not adopted into last_calibration on
    timeout: last_calibration is the reference the local calibration integrates
    from, and adopting a dropped write's report would make the next cycle treat
    it as confirmed.

    The flag is released in a finally block. Unlike the system mode and target
    temperature channels, whose writes go out regardless of their flag, the
    offset write only happens while calibration_received is True, so a watchdog
    that ended without releasing it would wedge the channel for good.

    Only the watchdog whose generation is still the TRV's current one releases
    the flag. A control cycle can confirm a command in-cycle and write a newer
    offset while an earlier watchdog is still winding down; releasing the gate
    from that earlier watchdog would open the channel for a command that is
    still in flight and turn one re-assert per confirmation window into one per
    control cycle.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    heater_entity_id : str, optional
        Entity ID of the TRV to check
    generation : int, optional
        Identity of the offset command this watchdog was armed for

    Returns
    -------
    bool
        Always returns True
    """
    _timeout = 0
    _real_trv = self.real_trvs[heater_entity_id]
    _tolerance = _calibration_match_tolerance(self, heater_entity_id)
    try:
        while True:
            if _real_trv.calibration_write_generation != generation:
                _LOGGER.debug(
                    "better_thermostat %s: a newer calibration command superseded "
                    "the one %s was being watched for, leaving the write gate to "
                    "its watchdog",
                    self.device_name,
                    heater_entity_id,
                )
                return True
            _trv_state = self.hass.states.get(heater_entity_id)
            if _trv_state is None or _trv_state.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
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
    finally:
        if _real_trv.calibration_write_generation == generation:
            _real_trv.calibration_received = True
    return True
