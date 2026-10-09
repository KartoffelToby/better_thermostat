"""Stateful property test of the decision kernel over event sequences.

Hypothesis draws random sequences of world events (window and door
contacts, room-sensor and TRV outages, TRV-reported setpoints, mode and
preset changes, calibration output, maintenance runs, restarts and
passing time) and feeds them through the same pure functions the shell
calls, in the order the shell calls them:

* contact events step the window/door region, and elapsed debounce
  delays settle it (``events/contact.py``),
* every availability change runs the watcher's two ladder steps
  (``utils/watcher.py``),
* a control cycle runs ``decide()`` and the safety hull
  (``utils/controlling.py``) and records the cycle in the flight
  recorder.

The oracle is a set of reference models kept next to the kernel, not
derived from it: the debounced contact state is recomputed from the raw
sensor history, the ladder's commits are checked against the
observation history, and the expected cascade tier follows from the
modelled regions. After every control cycle the kernel's output must
match the models, the hull must keep every intent inside its bounds, and
the exported recorder entry must replay to the same decision.

The single-step cross-products live in ``test_fsm_sweeps.py``; this
module covers what they cannot reach: behaviour that only emerges over a
sequence of steps and elapsed time.

The suite runs 200 sequences from a fixed seed, so every run replays the
same sequences and a red result reproduces. A longer search is a manual
run with a fresh random seed per run::

    BT_STATEFUL_EXAMPLES=20000 uv run pytest tests/unit/test_kernel_state_machine.py
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import json
import math
import os

from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)
import pytest

from custom_components.better_thermostat.core.decide import (
    PRESET_BOOST,
    KernelState,
    decide,
)
from custom_components.better_thermostat.core.desired import (
    DesiredState,
    Suppression,
    TrvDesired,
)
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    LadderParams,
    step as control_mode_step,
    step_ladder,
)
from custom_components.better_thermostat.core.fsm.lifecycle import (
    LifecyclePhase,
    startup_finished,
)
from custom_components.better_thermostat.core.fsm.maintenance import (
    MAX_RUN_S,
    MaintenancePhase,
    evaluate_tick,
    finish_run,
    start_run,
)
from custom_components.better_thermostat.core.fsm.mode import set_hvac_mode, set_preset
from custom_components.better_thermostat.core.fsm.reachability import (
    RETRY_MAX_S,
    ReachabilityState,
)
from custom_components.better_thermostat.core.fsm.window import (
    WindowParams,
    WindowPhase,
    WindowState,
    step as window_step,
)
from custom_components.better_thermostat.core.recorder import FlightRecorder, replay
from custom_components.better_thermostat.core.safety import (
    FALLBACK_MAX_OFFSET,
    FALLBACK_MAX_SETPOINT,
    FALLBACK_MIN_OFFSET,
    FALLBACK_MIN_SETPOINT,
    clamp,
)
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    TrvReported,
    WorldSnapshot,
)
from custom_components.better_thermostat.core.watchdog import CONTROL_TICK_S

# For the fixed-seed run Hypothesis registers its own PRNG, which lives in
# thread-local storage. Its liveness check looks for referrers through
# ``gc.get_referrers``, which does not see that holder, and warns although
# the object stays alive. The test draws nothing from ``random`` either.
pytestmark = pytest.mark.filterwarnings(
    "ignore:It looks like `register_random` was passed an object"
    ":hypothesis.errors.HypothesisWarning"
)

TRV_IDS = ("climate.trv_a", "climate.trv_b")
ROOM_SENSOR = "sensor.room"
WALL_EPOCH = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
LADDER = LadderParams()

# Inputs as the shell can deliver them: HA mode strings including ones
# the core does not know, and preset names including the "no preset"
# spellings.
HVAC_MODE_INPUTS = ("off", "heat", "cool", "heat_cool", "auto", "dry", "", None)
PRESET_INPUTS = ("none", "", None, "eco", "comfort", PRESET_BOOST, "away")

temperatures = st.floats(min_value=5.0, max_value=30.0).map(lambda t: round(t, 1))
# Device bounds as TRVs report them, including garbage the hull must survive.
device_bounds = st.sampled_from((None, math.nan, math.inf, 5.0, 7.0, 25.0, 30.0))
calibration_values = st.one_of(
    st.floats(min_value=-60.0, max_value=160.0),
    st.sampled_from((math.nan, math.inf, -math.inf)),
)
delays = st.sampled_from((0.0, 15.0, 60.0, 300.0))


@dataclass
class ContactModel:
    """Reference model of one debounced contact (window or door).

    The committed state follows the raw reading once the reading has
    persisted for the delay of its direction; a reading that flips back
    before the delay runs out never commits.
    """

    raw_open: bool = False
    raw_since: float = 0.0
    committed_open: bool = False

    def observe(self, now: float, params: WindowParams) -> None:
        """Commit the raw reading if it has persisted long enough at ``now``."""
        if self.raw_open == self.committed_open:
            return
        delay = (
            params.open_delay_seconds if self.raw_open else params.close_delay_seconds
        )
        if now - self.raw_since >= delay:
            self.committed_open = self.raw_open


@dataclass
class TrvWorld:
    """What one TRV reports to Home Assistant."""

    available: bool = True
    current_temperature: float | None = 20.0
    setpoint: float | None = None
    min_temp: float | None = 5.0
    max_temp: float | None = 30.0
    minimum_calibration: float | None = None
    calibration_max: float | None = None
    valve_max_opening: float | None = None


@dataclass
class Calibration:
    """Numbers a calibration strategy put onto the heating intents."""

    setpoint: float | None = None
    calibration_offset: float | None = None
    valve: float | None = None


@dataclass
class LadderModel:
    """Observation history the ladder's commits are checked against.

    ``deeper_since[d]`` is the start of the current run of observations
    at depth >= d, ``shallower_since[d]`` of the run at depth <= d.
    """

    observed: ControlMode = ControlMode.OPTIMAL
    observed_since: float = 0.0
    deeper_since: dict[int, float | None] = field(default_factory=dict)
    shallower_since: dict[int, float | None] = field(default_factory=dict)

    def observe(self, rung: ControlMode, now: float) -> None:
        """Record the rung the current capabilities support at ``now``."""
        depth = _depth(rung)
        for level in range(3):
            if depth >= level:
                if self.deeper_since.get(level) is None:
                    self.deeper_since[level] = now
            else:
                self.deeper_since[level] = None
            if depth <= level:
                if self.shallower_since.get(level) is None:
                    self.shallower_since[level] = now
            else:
                self.shallower_since[level] = None
        if rung != self.observed:
            self.observed = rung
            self.observed_since = now


def _depth(rung: ControlMode) -> int:
    return (ControlMode.OPTIMAL, ControlMode.SENSOR_FALLBACK, ControlMode.HOLD).index(
        rung
    )


def _span(*values: float | None) -> tuple[float, float]:
    finite = [v for v in values if v is not None and math.isfinite(v)]
    return min(finite), max(finite)


class KernelMachine(RuleBasedStateMachine):
    """Drive the kernel through random event sequences, checking every cycle."""

    # -- setup ---------------------------------------------------------------

    @initialize(
        window_params=st.builds(
            WindowParams, open_delay_seconds=delays, close_delay_seconds=delays
        ),
        door_params=st.builds(
            WindowParams, open_delay_seconds=delays, close_delay_seconds=delays
        ),
        hvac_mode=st.sampled_from(("heat", "off")),
        started=st.booleans(),
        calibration=st.builds(
            Calibration,
            setpoint=calibration_values,
            calibration_offset=calibration_values,
            valve=calibration_values,
        ),
    )
    def boot(
        self,
        window_params: WindowParams,
        door_params: WindowParams,
        hvac_mode: str,
        started: bool,
        calibration: Calibration,
    ) -> None:
        """Start the world and the entity for the first time.

        ``started`` skips ahead past the startup sequence, so half the
        runs begin in normal operation rather than behind the lifecycle
        gate.
        """
        self.wall = WALL_EPOCH
        self.mono = 1_000.0
        self.window_params = window_params
        self.door_params = door_params
        self.window = ContactModel(raw_since=self.mono)
        self.door = ContactModel(raw_since=self.mono)
        self.room_available = True
        self.room_temperature: float | None = 19.0
        self.heat_target_temperature: float | None = 21.0
        self.call_for_heat = True
        self.trvs = {entity_id: TrvWorld() for entity_id in TRV_IDS}
        self.calibration = calibration
        self.recorder = FlightRecorder(capacity=8)
        self.requested_mode = hvac_mode
        self.requested_preset: str | None = None
        self._restart()
        if started:
            self._finish_startup(grace_seconds=0)

    def _restart(self) -> None:
        """Rebuild the entity as ``async_added_to_hass`` does.

        The kernel regions are not persisted: the contacts are seeded
        from the sensors without debounce, the mode from the restored
        state, and everything else starts fresh.
        """
        self.kernel = KernelState()
        self.kernel = replace(
            self.kernel,
            window=self._seed(self.window),
            door=self._seed(self.door),
            mode=set_preset(
                set_hvac_mode(self.kernel.mode, self.requested_mode),
                self.requested_preset,
            ),
        )
        self.model_mode = self.kernel.mode.hvac_mode
        self.model_preset = self.kernel.mode.preset
        self.model_initialising = True
        self.model_maintenance_since: float | None = None
        self.model_offline_since: dict[str, float] = {}
        self.next_tick = self.mono + CONTROL_TICK_S
        self.ladder = LadderModel(observed_since=self.mono)
        self.ladder.observe(ControlMode.OPTIMAL, self.mono)
        self._watch()

    def _seed(self, contact: ContactModel) -> WindowState:
        contact.committed_open = contact.raw_open
        contact.raw_since = self.mono
        return WindowState(
            phase=WindowPhase.OPEN if contact.raw_open else WindowPhase.CLOSED
        )

    # -- shell emulation -----------------------------------------------------

    def _contact_event(self, kind: str, sensor_open: bool) -> None:
        contact, _ = self._contact(kind)
        if sensor_open == contact.raw_open:
            return
        contact.raw_open = sensor_open
        contact.raw_since = self.mono
        self._step_contact(kind, self.mono)
        self._control_cycle()

    def _contact(self, kind: str) -> tuple[ContactModel, WindowParams]:
        if kind == "window":
            return self.window, self.window_params
        return self.door, self.door_params

    def _step_contact(self, kind: str, now: float) -> None:
        contact, params = self._contact(kind)
        region = getattr(self.kernel, kind)
        stepped = window_step(region, contact.raw_open, now, params)
        self.kernel = replace(self.kernel, **{kind: stepped})
        contact.observe(now, params)
        assert stepped.effective_open == contact.committed_open, (
            f"{kind} region {stepped} disagrees with the debounced sensor "
            f"history {contact} at t={now}"
        )

    def _contact_due(self, kind: str) -> float | None:
        """When the settle task for ``kind`` wakes up, if one is pending."""
        _, params = self._contact(kind)
        region: WindowState = getattr(self.kernel, kind)
        if region.pending_since is None:
            return None
        delay = (
            params.open_delay_seconds
            if region.phase == WindowPhase.OPENING
            else params.close_delay_seconds
        )
        return region.pending_since + delay

    def _contact_dues(self) -> list[float]:
        dues = (self._contact_due(kind) for kind in ("window", "door"))
        return [due for due in dues if due is not None]

    def _settle_due_contacts(self) -> None:
        """Fire the settle task's wake-up for every elapsed debounce delay."""
        for kind in ("window", "door"):
            due = self._contact_due(kind)
            if due is not None and due <= self.mono:
                self._step_contact(kind, self.mono)

    def _trv_temperature_ok(self) -> bool:
        return any(
            trv.available
            and trv.current_temperature is not None
            and math.isfinite(trv.current_temperature)
            for trv in self.trvs.values()
        )

    def _watch(self) -> None:
        """Run the watcher's availability check and both ladder steps."""
        unavailable = [] if self.room_available else [ROOM_SENSOR]
        before = self.kernel.control_mode
        state = control_mode_step(before, unavailable, self.mono)
        state = step_ladder(
            state,
            room_sensor_ok=self.room_available,
            trv_temperature_ok=self._trv_temperature_ok(),
            now=self.mono,
            params=LADDER,
        )
        self.kernel = replace(self.kernel, control_mode=state)

        observed = (
            ControlMode.OPTIMAL
            if self.room_available
            else ControlMode.SENSOR_FALLBACK
            if self._trv_temperature_ok()
            else ControlMode.HOLD
        )
        self.ladder.observe(observed, self.mono)
        self._check_ladder_commit(before.mode, state.mode)

        # Annunciation half: degraded exactly while the room sensor is
        # away, and the start of the degradation survives rung commits.
        assert state.degraded == (not self.room_available)
        if before.degraded and state.degraded:
            assert state.degraded_since == before.degraded_since
        if state.degraded:
            assert state.degraded_since is not None
            assert state.degraded_since <= self.mono

    def _check_ladder_commit(self, old: ControlMode, new: ControlMode) -> None:
        if old == new:
            return
        level = _depth(new)
        if _depth(new) > _depth(old):
            since = self.ladder.deeper_since.get(level)
            window = LADDER.down_debounce_seconds
        else:
            since = self.ladder.shallower_since.get(level)
            window = LADDER.up_stability_seconds
        assert since is not None and self.mono - since >= window, (
            f"ladder committed {old} -> {new} at t={self.mono} although the "
            f"observations supported {new} only since {since} "
            f"(needs {window}s)"
        )

    def _snapshot(self) -> WorldSnapshot:
        return WorldSnapshot(
            now=self.wall,
            now_monotonic=self.mono,
            heat_target_temperature=self.heat_target_temperature,
            hvac_mode=self.kernel.mode.hvac_mode,
            room_temperature=self.room_temperature,
            call_for_heat=self.call_for_heat,
            window_open=self.window.raw_open,
            preset_mode=self.kernel.mode.preset,
            trvs={
                entity_id: TrvReported(
                    entity_id=entity_id,
                    available=trv.available,
                    hvac_mode=HvacMode.HEAT,
                    current_temperature=trv.current_temperature,
                    setpoint=trv.setpoint,
                    min_temp=trv.min_temp,
                    max_temp=trv.max_temp,
                    valve_max_opening=trv.valve_max_opening,
                    min_local_calibration=trv.minimum_calibration,
                    max_local_calibration=trv.calibration_max,
                )
                for entity_id, trv in self.trvs.items()
            },
        )

    # -- world events --------------------------------------------------------

    @rule(sensor_open=st.booleans())
    def window_contact(self, sensor_open: bool) -> None:
        """Window opens or closes."""
        self._contact_event("window", sensor_open)

    @rule(sensor_open=st.booleans())
    def door_contact(self, sensor_open: bool) -> None:
        """Door opens or closes."""
        self._contact_event("door", sensor_open)

    @rule(
        seconds=st.one_of(
            st.integers(min_value=1, max_value=120),
            st.sampled_from((15, 60, 120, 300, 600, 3_600, 4_000)),
        )
    )
    def time_passes(self, seconds: int) -> None:
        """Let time pass, firing every timer that falls due on the way.

        The contact settle tasks wake exactly when their debounce delay
        runs out, and the periodic tick runs the watcher every
        ``CONTROL_TICK_S``; both fire in time order.
        """
        end = self.mono + seconds
        while True:
            due = min([self.next_tick, *self._contact_dues()], default=math.inf)
            if due > end:
                break
            self._advance_to(due)
            self._settle_due_contacts()
            # The settle task loops until nothing is pending; a wake-up
            # that leaves an overdue transition behind would spin forever.
            assert all(d > self.mono for d in self._contact_dues()), (
                f"settle wake-up at t={self.mono} left an overdue transition: "
                f"window={self.kernel.window} door={self.kernel.door}"
            )
            if self.next_tick <= self.mono:
                self.next_tick += CONTROL_TICK_S
                self._watch()
            self._control_cycle()
        self._advance_to(end)

    def _advance_to(self, when: float) -> None:
        self.wall += timedelta(seconds=when - self.mono)
        self.mono = when

    @rule(available=st.booleans(), temperature=st.one_of(st.none(), temperatures))
    def room_sensor(self, available: bool, temperature: float | None) -> None:
        """Room sensor drops out, returns, or reports a new temperature.

        The entity keeps its last known room temperature through an
        outage, so an unavailable sensor does not clear it.
        """
        self.room_available = available
        if available and temperature is not None:
            self.room_temperature = temperature
        self._watch()
        self._control_cycle()

    @rule(
        entity_id=st.sampled_from(TRV_IDS),
        available=st.booleans(),
        current_temperature=st.one_of(st.none(), temperatures, st.just(math.nan)),
    )
    def trv_report(
        self, entity_id: str, available: bool, current_temperature: float | None
    ) -> None:
        """A TRV goes away, comes back, or reports its internal temperature."""
        trv = self.trvs[entity_id]
        trv.available = available
        trv.current_temperature = current_temperature
        self._watch()
        self._control_cycle()

    @rule(
        entity_id=st.sampled_from(TRV_IDS), setpoint=st.one_of(st.none(), temperatures)
    )
    def trv_reports_own_setpoint(self, entity_id: str, setpoint: float | None) -> None:
        """Someone turns the knob on the TRV; it reports its own setpoint."""
        self.trvs[entity_id].setpoint = setpoint
        self._control_cycle()

    @rule(
        entity_id=st.sampled_from(TRV_IDS),
        min_temp=device_bounds,
        max_temp=device_bounds,
        minimum_calibration=st.sampled_from((None, math.nan, -12.7, -5.0, 3.0)),
        calibration_max=st.sampled_from((None, math.nan, 12.7, 5.0, -3.0)),
        valve_max_opening=st.sampled_from((None, math.nan, 0.0, 60.0, 100.0)),
    )
    def trv_reports_limits(
        self,
        entity_id: str,
        min_temp: float | None,
        max_temp: float | None,
        minimum_calibration: float | None,
        calibration_max: float | None,
        valve_max_opening: float | None,
    ) -> None:
        """A TRV (re-)reports its limits, plausible or not."""
        trv = self.trvs[entity_id]
        trv.min_temp = min_temp
        trv.max_temp = max_temp
        trv.minimum_calibration = minimum_calibration
        trv.calibration_max = calibration_max
        trv.valve_max_opening = valve_max_opening

    @rule(mode=st.sampled_from(HVAC_MODE_INPUTS))
    def user_sets_hvac_mode(self, mode: str | None) -> None:
        """The user (or an automation) sets the HVAC mode."""
        self.kernel = replace(self.kernel, mode=set_hvac_mode(self.kernel.mode, mode))
        if mode in {m.value for m in HvacMode}:
            self.requested_mode = mode
            self.model_mode = HvacMode(mode)
        self._control_cycle()

    @rule(preset=st.sampled_from(PRESET_INPUTS))
    def user_sets_preset(self, preset: str | None) -> None:
        """The user switches the preset."""
        self.kernel = replace(self.kernel, mode=set_preset(self.kernel.mode, preset))
        self.requested_preset = preset
        self.model_preset = None if preset in (None, "none", "") else preset
        self._control_cycle()

    @rule(new_target=st.one_of(st.none(), temperatures))
    def user_sets_target(self, new_target: float | None) -> None:
        """The room target changes (user, preset or schedule)."""
        self.heat_target_temperature = new_target
        self._control_cycle()

    @rule(demand=st.booleans())
    def call_for_heat_changes(self, demand: bool) -> None:
        """The heat-demand switch flips."""
        self.call_for_heat = demand
        self._control_cycle()

    @rule(
        setpoint=st.one_of(st.none(), calibration_values),
        calibration_offset=st.one_of(st.none(), calibration_values),
        valve=st.one_of(st.none(), calibration_values),
    )
    def calibration_output(
        self,
        setpoint: float | None,
        calibration_offset: float | None,
        valve: float | None,
    ) -> None:
        """A calibration strategy produces new numbers, sane or not."""
        self.calibration = Calibration(
            setpoint=setpoint, calibration_offset=calibration_offset, valve=valve
        )

    @rule(host_reboot=st.booleans())
    def home_assistant_restarts(self, host_reboot: bool) -> None:
        """HA restarts; after a host reboot the monotonic clock starts over."""
        if host_reboot:
            self.mono = 5.0
        self._restart()

    @precondition(lambda self: self.model_initialising)
    @rule(grace_seconds=st.sampled_from((0, 60, 900)))
    def startup_completes(self, grace_seconds: int) -> None:
        """The startup sequence finishes and arms the annunciation grace."""
        self._finish_startup(grace_seconds)
        self._control_cycle()

    def _finish_startup(self, grace_seconds: int) -> None:
        self.kernel = replace(
            self.kernel,
            lifecycle=startup_finished(
                self.kernel.lifecycle, self.wall + timedelta(seconds=grace_seconds)
            ),
        )
        self.model_initialising = False

    @rule(has_enabled_trvs=st.booleans())
    def maintenance_scheduler_ticks(self, has_enabled_trvs: bool) -> None:
        """The valve-maintenance scheduler evaluates its schedule."""
        self.kernel = replace(
            self.kernel,
            maintenance=evaluate_tick(
                self.kernel.maintenance,
                self.wall,
                window_open=self.kernel.window.effective_open,
                has_enabled_trvs=has_enabled_trvs,
            ),
        )

    @precondition(lambda self: self.kernel.maintenance.phase == MaintenancePhase.DUE)
    @rule()
    def maintenance_starts(self) -> None:
        """A due valve exercise starts."""
        self.kernel = replace(
            self.kernel, maintenance=start_run(self.kernel.maintenance, self.mono)
        )
        self.model_maintenance_since = self.mono

    @precondition(
        lambda self: self.kernel.maintenance.phase == MaintenancePhase.RUNNING
    )
    @rule()
    def maintenance_finishes(self) -> None:
        """The valve exercise ends and reschedules."""
        self.kernel = replace(
            self.kernel,
            maintenance=finish_run(
                self.kernel.maintenance, self.wall + timedelta(days=7)
            ),
        )
        self.model_maintenance_since = None
        self._control_cycle()

    # -- the control cycle ---------------------------------------------------

    @rule()
    def control_cycle(self) -> None:
        """A control cycle runs without a new event (a queued request)."""
        self._control_cycle()

    def _control_cycle(self) -> None:
        """Run one control cycle and hold it against every model."""
        snapshot = self._snapshot()
        pre = self.kernel
        desired, post = decide(snapshot, pre)

        assert desired == self._expected(snapshot), (
            f"cascade disagrees with the models: got {desired}, "
            f"expected {self._expected(snapshot)}"
        )
        self._check_history_independence(snapshot, pre, desired)
        self._check_reported_setpoint_is_ignored(snapshot, pre, desired)
        self._check_reachability(post)
        self._check_hull(snapshot, desired)
        self._check_recorder_round_trip(snapshot, pre, desired)

        if post.lifecycle.phase == LifecyclePhase.RUNNING:
            assert not self.model_initialising
        self.kernel = post

    def _expected(self, snapshot: WorldSnapshot) -> DesiredState:
        """The cascade as the reference models predict it."""
        maintenance_blocks = (
            self.model_maintenance_since is not None
            and self.mono - self.model_maintenance_since < MAX_RUN_S
        )
        if self.model_initialising or maintenance_blocks:
            return DesiredState(call_for_heat=self.call_for_heat)

        boost = (
            self.model_preset == PRESET_BOOST
            and self.room_temperature is not None
            and self.heat_target_temperature is not None
            and self.room_temperature < self.heat_target_temperature
        )
        addressed = [e for e, trv in self.trvs.items() if trv.available or boost]

        def off(suppression: Suppression | None) -> dict[str, TrvDesired]:
            return {
                e: TrvDesired(
                    entity_id=e, hvac_mode=HvacMode.OFF, suppression=suppression
                )
                for e in addressed
            }

        if self.model_mode == HvacMode.OFF:
            return DesiredState(call_for_heat=False, trvs=off(None))
        if self.window.committed_open:
            return DesiredState(
                call_for_heat=self.call_for_heat, trvs=off(Suppression.WINDOW)
            )
        if self.door.committed_open:
            return DesiredState(
                call_for_heat=self.call_for_heat, trvs=off(Suppression.DOOR)
            )
        if not self.call_for_heat:
            return DesiredState(
                call_for_heat=False, trvs=off(Suppression.NO_CALL_FOR_HEAT)
            )
        return DesiredState(
            call_for_heat=True,
            trvs={
                e: TrvDesired(
                    entity_id=e,
                    hvac_mode=self.model_mode,
                    setpoint=self.heat_target_temperature,
                )
                for e in addressed
            },
        )

    def _check_history_independence(
        self, snapshot: WorldSnapshot, pre: KernelState, desired: DesiredState
    ) -> None:
        """The decision depends on committed regions only, not on bookkeeping.

        A window that opened and closed again, a ladder mid-window, or a
        TRV deep in its retry backoff must leave the same decision as a
        region that never saw any of it.
        """

        def committed(region: WindowState) -> WindowState:
            return WindowState(
                phase=WindowPhase.OPEN if region.effective_open else WindowPhase.CLOSED
            )

        normalized = replace(
            pre,
            window=committed(pre.window),
            door=committed(pre.door),
            control_mode=replace(pre.control_mode, pending=None),
            reachability={},
            last_control_monotonic=None,
        )
        assert decide(snapshot, normalized)[0] == desired

    def _check_reported_setpoint_is_ignored(
        self, snapshot: WorldSnapshot, pre: KernelState, desired: DesiredState
    ) -> None:
        """A knob turned on the TRV never leaks into the kernel's intent.

        Adopting a device-side change is the shell's job; the kernel must
        not echo the reported setpoint back.
        """
        blind = replace(
            snapshot,
            trvs={e: replace(trv, setpoint=None) for e, trv in snapshot.trvs.items()},
        )
        assert decide(blind, pre)[0] == desired

    def _check_reachability(self, post: KernelState) -> None:
        for entity_id, trv in self.trvs.items():
            region: ReachabilityState = post.reachability[entity_id]
            assert region.online == trv.available
            if trv.available:
                self.model_offline_since.pop(entity_id, None)
                continue
            since = self.model_offline_since.setdefault(entity_id, self.mono)
            assert region.offline_since == since
            assert region.retry_at is not None
            assert self.mono < region.retry_at <= self.mono + RETRY_MAX_S

    def _check_hull(self, snapshot: WorldSnapshot, desired: DesiredState) -> None:
        """Every calibrated intent leaves the hull finite and in bounds.

        Besides the cascade's own intents, a heating probe for every TRV
        goes through the hull, so the bounds are exercised whatever tier
        the cascade is on.
        """
        probe = DesiredState(
            call_for_heat=True,
            trvs={
                e: TrvDesired(
                    entity_id=e,
                    hvac_mode=HvacMode.HEAT,
                    setpoint=self.calibration.setpoint,
                    calibration_offset=self.calibration.calibration_offset,
                    valve_percent=self.calibration.valve,
                )
                for e in snapshot.trvs
            },
        )
        self._check_hulled(snapshot, probe)
        calibrated = DesiredState(
            call_for_heat=desired.call_for_heat,
            trvs={
                e: replace(
                    intent,
                    setpoint=self.calibration.setpoint,
                    calibration_offset=self.calibration.calibration_offset,
                    valve_percent=self.calibration.valve,
                )
                if intent.hvac_mode not in (None, HvacMode.OFF)
                else intent
                for e, intent in desired.trvs.items()
            },
        )
        self._check_hulled(snapshot, calibrated)

    def _check_hulled(self, snapshot: WorldSnapshot, calibrated: DesiredState) -> None:
        hulled = clamp(calibrated, snapshot)
        assert clamp(hulled, snapshot) == hulled, "the hull is not idempotent"
        assert hulled.trvs.keys() == calibrated.trvs.keys()

        for entity_id, intent in hulled.trvs.items():
            before = calibrated.trvs[entity_id]
            trv = self.trvs[entity_id]
            assert intent.hvac_mode == before.hvac_mode
            assert intent.suppression == before.suppression
            self._check_bounded(
                before.setpoint,
                intent.setpoint,
                trv.min_temp,
                trv.max_temp,
                FALLBACK_MIN_SETPOINT,
                FALLBACK_MAX_SETPOINT,
            )
            self._check_bounded(
                before.calibration_offset,
                intent.calibration_offset,
                trv.minimum_calibration,
                trv.calibration_max,
                FALLBACK_MIN_OFFSET,
                FALLBACK_MAX_OFFSET,
            )
            if before.valve_percent is None or not math.isfinite(before.valve_percent):
                assert intent.valve_percent is None
            else:
                assert intent.valve_percent is not None
                assert 0.0 <= intent.valve_percent <= 100.0
                if trv.valve_max_opening is not None and math.isfinite(
                    trv.valve_max_opening
                ):
                    assert intent.valve_percent <= trv.valve_max_opening

    @staticmethod
    def _check_bounded(
        raw: float | None,
        hulled: float | None,
        reported_low: float | None,
        reported_high: float | None,
        fallback_low: float,
        fallback_high: float,
    ) -> None:
        if raw is None or not math.isfinite(raw):
            assert hulled is None
            return
        assert hulled is not None and math.isfinite(hulled)
        low_ok = reported_low is not None and math.isfinite(reported_low)
        high_ok = reported_high is not None and math.isfinite(reported_high)
        if low_ok and high_ok:
            low, high = _span(reported_low, reported_high)
        else:
            low, high = _span(reported_low, reported_high, fallback_low, fallback_high)
        assert low <= hulled <= high
        if low <= raw <= high and (low_ok and high_ok):
            assert hulled == raw, "the hull moved a value that was in bounds"

    def _check_recorder_round_trip(
        self, snapshot: WorldSnapshot, pre: KernelState, desired: DesiredState
    ) -> None:
        """The diagnostics export replays to the decision it recorded."""
        self.recorder.record(snapshot, pre, desired)
        exported = json.loads(json.dumps(self.recorder.export()[-1]))
        matches, recomputed = replay(exported)
        assert matches, f"replay diverged: recorded {desired}, got {recomputed}"

    # -- invariants between steps -------------------------------------------

    @invariant()
    def ladder_converges(self) -> None:
        """Stable capabilities pull the ladder onto their rung.

        A degrade or recovery can commit through an intermediate rung.
        Each commit takes one full window plus the wait for the periodic
        tick that evaluates it, so two of those bound the whole move.
        """
        if not hasattr(self, "ladder"):
            return
        bound = 2 * (
            max(LADDER.down_debounce_seconds, LADDER.up_stability_seconds)
            + CONTROL_TICK_S
        )
        if self.mono - self.ladder.observed_since >= bound:
            assert self.kernel.control_mode.mode == self.ladder.observed, (
                f"ladder stuck on {self.kernel.control_mode.mode} although "
                f"{self.ladder.observed} has been observed since "
                f"t={self.ladder.observed_since} (now {self.mono})"
            )

    @invariant()
    def mode_region_matches_requests(self) -> None:
        """Mode and preset are exactly what the last valid requests set."""
        if not hasattr(self, "kernel"):
            return
        assert self.kernel.mode.hvac_mode == self.model_mode
        assert self.kernel.mode.preset == self.model_preset

    @invariant()
    def maintenance_cannot_block_forever(self) -> None:
        """A dead maintenance run stops pre-empting control after its cap."""
        if not hasattr(self, "kernel"):
            return
        region = self.kernel.maintenance
        if region.running_since is not None and region.is_blocking(self.mono):
            assert self.mono - region.running_since < MAX_RUN_S


_MANUAL_EXAMPLES = os.environ.get("BT_STATEFUL_EXAMPLES")

KernelMachine.TestCase.settings = settings(
    max_examples=int(_MANUAL_EXAMPLES) if _MANUAL_EXAMPLES else 200,
    derandomize=not _MANUAL_EXAMPLES,
    stateful_step_count=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
TestKernelMachine = KernelMachine.TestCase
