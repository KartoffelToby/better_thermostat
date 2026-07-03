"""Exhaustive invariant sweeps over every FSM region.

The per-FSM test files pin individual transitions; these sweeps walk
the full input cross-product of each region with ``itertools.product``
and assert the structural invariants that must hold on every single
combination — the class of bug a hand-picked example test misses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import itertools

import pytest

from custom_components.better_thermostat.core.fsm import (
    control_mode as cm,
    lifecycle as lc,
    maintenance as mt,
    mode as md,
    reachability as rb,
    window as wd,
)
from custom_components.better_thermostat.core.snapshot import HvacMode

NOW = 10_000.0
NOW_DT = datetime(2026, 1, 2, 8, 30, tzinfo=UTC)


def _window_states():
    """All well-formed region states: only pending phases carry a timestamp."""
    for phase in wd.WindowPhase:
        if phase in (wd.WindowPhase.OPENING, wd.WindowPhase.CLOSING):
            pendings = (NOW - 5.0, NOW - 30.0)
        else:
            pendings = (None,)
        for pending in pendings:
            yield wd.WindowState(phase=phase, pending_since=pending)


class TestWindowSweep:
    """closed -> opening -> open -> closing with debounce, exhaustively."""

    PARAMS = (
        wd.WindowParams(),
        wd.WindowParams(open_delay_s=10.0, close_delay_s=10.0),
        wd.WindowParams(open_delay_s=60.0, close_delay_s=60.0),
    )

    @pytest.mark.parametrize(
        ("state", "sensor_open", "params"),
        list(itertools.product(_window_states(), (False, True), PARAMS)),
    )
    def test_invariants_hold_for_every_combination(self, state, sensor_open, params):
        """Every step keeps the region structurally sound."""
        result = wd.step(state, sensor_open, NOW, params)

        # Pending phases always carry their timestamp.
        if result.phase in (wd.WindowPhase.OPENING, wd.WindowPhase.CLOSING):
            assert result.pending_since is not None
        # Committed phases never carry one.
        if result.phase in (wd.WindowPhase.CLOSED, wd.WindowPhase.OPEN):
            assert result.pending_since is None

        # effective_open is a pure function of the phase.
        assert result.effective_open == (
            result.phase in (wd.WindowPhase.OPEN, wd.WindowPhase.CLOSING)
        )

        # A pending transition only commits after its full delay.
        if state.phase == wd.WindowPhase.OPENING and sensor_open:
            elapsed = NOW - state.pending_since if state.pending_since else None
            if elapsed is not None and elapsed < params.open_delay_s:
                assert result.phase == wd.WindowPhase.OPENING

        # Stepping again with the same observation changes nothing.
        assert wd.step(result, sensor_open, NOW, params) == result


class TestLadderSweep:
    """The fail-soft ladder degrades fast and recovers slowly, exhaustively."""

    PARAMS = cm.LadderParams(down_debounce_s=120.0, up_stability_s=300.0)
    RUNGS = tuple(cm.ControlMode)
    # Pending ages: fresh, just below both thresholds, past debounce,
    # past stability.
    AGES = (None, NOW - 1.0, NOW - 119.0, NOW - 120.0, NOW - 300.0)

    @pytest.mark.parametrize(
        ("mode", "down_since", "up_since", "room_ok", "trv_ok"),
        list(itertools.product(RUNGS, AGES, AGES, (False, True), (False, True))),
    )
    def test_invariants_hold_for_every_combination(
        self, mode, down_since, up_since, room_ok, trv_ok
    ):
        """Every step lands on the old rung or the capability target."""
        state = cm.ControlModeState(
            mode=mode,
            down_pending_since=down_since,
            up_pending_since=up_since,
            degraded_since=None if mode == cm.ControlMode.OPTIMAL else NOW - 500.0,
        )
        target = cm._target_rung(room_ok, trv_ok)
        result = cm.step_ladder(
            state,
            room_sensor_ok=room_ok,
            trv_temp_ok=trv_ok,
            now=NOW,
            params=self.PARAMS,
        )

        # The rung only ever moves to the capability target.
        assert result.mode in (mode, target)

        order = (
            cm.ControlMode.OPTIMAL,
            cm.ControlMode.SENSOR_FALLBACK,
            cm.ControlMode.HOLD,
        )
        if result.mode != mode:
            degrading = order.index(target) > order.index(mode)
            threshold = (
                self.PARAMS.down_debounce_s if degrading else self.PARAMS.up_stability_s
            )
            since = down_since if degrading else up_since
            # A commit requires the full debounce/stability window.
            assert since is not None and NOW - since >= threshold

        # Matching capability clears all pending timers.
        if target == mode:
            assert result.down_pending_since is None
            assert result.up_pending_since is None

        # After a commit, degraded_since exactly mirrors the new rung.
        if result.mode != mode:
            if result.mode == cm.ControlMode.OPTIMAL:
                assert result.degraded_since is None
            else:
                assert result.degraded_since is not None


class TestReachabilitySweep:
    """Per-TRV online/offline with exponential backoff, exhaustively."""

    STATES = (
        rb.ReachabilityState(),
        rb.ReachabilityState(
            online=False, offline_since=NOW - 10.0, retry_count=0, retry_at=NOW + 20.0
        ),
        rb.ReachabilityState(
            online=False, offline_since=NOW - 600.0, retry_count=3, retry_at=NOW - 1.0
        ),
        rb.ReachabilityState(
            online=False, offline_since=NOW - 9000.0, retry_count=9, retry_at=NOW - 1.0
        ),
    )

    @pytest.mark.parametrize(
        ("state", "reported"), list(itertools.product(STATES, (False, True)))
    )
    def test_invariants_hold_for_every_combination(self, state, reported):
        """Recovery is total, offline bookkeeping is monotone and capped."""
        result = rb.step(state, reported, NOW)

        if reported:
            # Any available report fully resets the region.
            assert result == rb.ReachabilityState()
            return

        assert result.online is False
        assert result.offline_since is not None
        # offline_since is sticky for the whole offline episode.
        if not state.online:
            assert result.offline_since == state.offline_since
        # The retry schedule only moves forward and the backoff is capped.
        assert result.retry_at is not None
        assert result.retry_at > NOW - 1.0
        if result.retry_count != state.retry_count:
            assert result.retry_at - NOW <= rb.RETRY_MAX_S

    def test_backoff_doubles_and_caps(self):
        """30 s doubling, hard-capped at 10 min."""
        delays = [rb._backoff(n) for n in range(8)]
        assert delays[:5] == [30.0, 60.0, 120.0, 240.0, 480.0]
        assert all(d == rb.RETRY_MAX_S for d in delays[5:])


class TestModeSweep:
    """The mode region never holds an invalid value, exhaustively."""

    INPUTS = (
        None,
        "",
        "heat",
        "cool",
        "off",
        "heat_cool",
        "auto",
        "dry",
        "fan_only",
        "HEAT",
        "banana",
        " heat ",
    )

    @pytest.mark.parametrize(
        ("current", "value"), list(itertools.product(tuple(HvacMode), INPUTS))
    )
    def test_hvac_mode_stays_valid(self, current, value):
        """Unknown inputs leave the state unchanged; known ones apply."""
        state = md.ModeState(hvac_mode=current, preset="eco")
        result = md.set_hvac_mode(state, value)
        assert isinstance(result.hvac_mode, HvacMode)
        if result.hvac_mode != current:
            assert value is not None and result.hvac_mode == value.strip().lower()
        # The preset axis is untouched by the mode axis.
        assert result.preset == "eco"

    @pytest.mark.parametrize(
        ("current", "preset"),
        list(
            itertools.product(
                (None, "eco", "boost"), (None, "", "none", "eco", "boost", "away")
            )
        ),
    )
    def test_preset_clears_or_applies(self, current, preset):
        """PRESET_NONE and empty values clear; the mode axis is untouched."""
        state = md.ModeState(hvac_mode=HvacMode.HEAT, preset=current)
        result = md.set_preset(state, preset)
        if preset in (None, "", md.PRESET_NONE):
            assert result.preset is None
        else:
            assert result.preset == preset
        assert result.hvac_mode == HvacMode.HEAT


class TestLifecycleSweep:
    """The lifecycle only ever moves forward, exhaustively."""

    ORDER = (
        lc.LifecyclePhase.INITIALISING,
        lc.LifecyclePhase.STARTING,
        lc.LifecyclePhase.RUNNING,
        lc.LifecyclePhase.STOPPING,
    )
    GRACES = (None, NOW_DT - timedelta(seconds=1), NOW_DT + timedelta(seconds=60))

    @pytest.mark.parametrize(("phase", "grace"), list(itertools.product(ORDER, GRACES)))
    def test_transitions_never_move_backwards(self, phase, grace):
        """startup_finished, tick, and stop are monotone in phase order."""
        state = lc.LifecycleState(phase=phase, grace_until=grace)
        for result in (
            lc.startup_finished(state),
            lc.tick(state, NOW_DT),
            lc.stop(state),
        ):
            assert self.ORDER.index(result.phase) >= self.ORDER.index(phase)

        # Grace only counts while a deadline is set and in the future.
        assert state.in_grace(NOW_DT) == (grace is not None and NOW_DT < grace)
        # startup_running is exactly the INITIALISING phase.
        assert state.startup_running == (phase == lc.LifecyclePhase.INITIALISING)

    def test_tick_promotes_only_after_grace(self):
        """STARTING holds while in grace and promotes when it lapses."""
        staying = lc.LifecycleState(
            phase=lc.LifecyclePhase.STARTING, grace_until=NOW_DT + timedelta(seconds=60)
        )
        assert lc.tick(staying, NOW_DT).phase == lc.LifecyclePhase.STARTING
        lapsed = lc.LifecycleState(
            phase=lc.LifecyclePhase.STARTING, grace_until=NOW_DT - timedelta(seconds=1)
        )
        assert lc.tick(lapsed, NOW_DT).phase == lc.LifecyclePhase.RUNNING


class TestMaintenanceSweep:
    """The maintenance region can never block control forever, exhaustively."""

    DUES = (None, NOW_DT - timedelta(minutes=1), NOW_DT + timedelta(days=1))

    @pytest.mark.parametrize(
        ("phase", "due", "window_open", "hvac_off", "has_trvs"),
        list(
            itertools.product(
                tuple(mt.MaintenancePhase),
                (None, NOW_DT - timedelta(minutes=1), NOW_DT + timedelta(days=1)),
                (False, True),
                (False, True),
                (False, True),
            )
        ),
    )
    def test_evaluate_tick_invariants(
        self, phase, due, window_open, hvac_off, has_trvs
    ):
        """DUE only arms from an unblocked, due, idle schedule."""
        state = mt.MaintenanceState(
            phase=phase,
            next_due=due,
            running_since=NOW - 5.0 if phase == mt.MaintenancePhase.RUNNING else None,
        )
        result = mt.evaluate_tick(
            state,
            NOW_DT,
            window_open=window_open,
            hvac_off=hvac_off,
            has_enabled_trvs=has_trvs,
        )

        # Only the IDLE phase reacts to the scheduler tick.
        if phase != mt.MaintenancePhase.IDLE:
            assert result == state
            return
        # A future schedule stays untouched.
        if due is not None and NOW_DT < due:
            assert result == state
            return
        # Blocked or TRV-less ticks postpone instead of arming.
        if window_open or hvac_off or not has_trvs:
            assert result.phase == mt.MaintenancePhase.IDLE
            assert result.next_due is not None and result.next_due > NOW_DT
        else:
            assert result.phase == mt.MaintenancePhase.DUE

    @pytest.mark.parametrize("phase", tuple(mt.MaintenancePhase))
    def test_finish_run_always_returns_to_idle(self, phase):
        """finish_run is unconditional — RUNNING can never be sticky."""
        state = mt.MaintenanceState(phase=phase, running_since=NOW)
        result = mt.finish_run(state, NOW_DT + timedelta(days=5))
        assert result.phase == mt.MaintenancePhase.IDLE
        assert result.running_since is None

    def test_overrunning_maintenance_stops_blocking(self):
        """A RUNNING phase past MAX_RUN_S is dead and no longer blocks."""
        state = mt.MaintenanceState(
            phase=mt.MaintenancePhase.RUNNING, running_since=NOW - mt.MAX_RUN_S
        )
        assert state.is_blocking(NOW) is False
        fresh = mt.MaintenanceState(
            phase=mt.MaintenancePhase.RUNNING, running_since=NOW - 10.0
        )
        assert fresh.is_blocking(NOW) is True
