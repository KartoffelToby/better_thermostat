"""Auto-tuning of the PID gains over runs of control cycles.

Every test threads one ``PIDState`` through ``compute_pid`` with an injected
clock, five minutes per cycle as the recompute tick runs in production.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from custom_components.better_thermostat.utils.calibration.pid import (
    DEFAULT_PID_KD,
    DEFAULT_PID_KP,
    PIDParams,
    PIDState,
    compute_pid,
)

_CYCLE_S = 300.0
# A room that keeps moving: the sluggish rule stays out of these runs.
_MOVING_SLOPE = 0.01


def _run(
    readings: Iterable[float],
    *,
    target: float | Iterable[float] = 21.0,
    state: PIDState | None = None,
    params: PIDParams | None = None,
    slope: float = _MOVING_SLOPE,
) -> PIDState:
    """Feed one reading per cycle and return the state after the last one."""
    state = state if state is not None else PIDState()
    params = params if params is not None else PIDParams()
    readings = list(readings)
    targets = (
        [float(target)] * len(readings)
        if isinstance(target, (int, float))
        else list(target)
    )
    now = 1000.0
    for reading, cycle_target in zip(readings, targets, strict=True):
        now += _CYCLE_S
        _, _, state = compute_pid(
            params, cycle_target, reading, reading, slope, "k", state=state, now=now
        )
    return state


class TestOvershootDetection:
    """Only a crossing of the target beyond the threshold is an overshoot."""

    def test_an_approach_that_settles_below_the_target_keeps_kp_and_kd(self):
        """Entering the band from below without crossing tunes no damping."""
        state = _run([20.0, 20.3, 20.5, 20.7, 20.85, 20.95, 20.96, 20.97])

        assert state.pid_kp == DEFAULT_PID_KP
        assert state.pid_kd == DEFAULT_PID_KD

    def test_repeated_dips_below_the_target_do_not_ratchet_kd(self):
        """Forty dips out of the band and back stay on the same side."""
        state = _run([20.0] + [20.85, 20.95] * 40)

        assert state.pid_kp == DEFAULT_PID_KP
        assert state.pid_kd == DEFAULT_PID_KD

    def test_a_crossing_within_the_threshold_is_no_overshoot(self):
        """Ending up 0.15 K above the target stays under the 0.2 K threshold."""
        state = _run([20.0, 20.5, 21.15, 21.15])

        assert state.pid_kp == DEFAULT_PID_KP
        assert state.pid_kd == DEFAULT_PID_KD

    def test_a_crossing_beyond_the_threshold_tunes_once(self):
        """The room ending up 0.3 K above the target damps the gains once.

        Staying above the target is the same overshoot, not a new one.
        """
        params = PIDParams()
        state = _run([20.0, 20.5, 21.3, 21.3, 21.25, 21.3], params=params)

        assert state.pid_kp == pytest.approx(DEFAULT_PID_KP * params.kp_step_mul)
        assert state.pid_kd == pytest.approx(DEFAULT_PID_KD * params.kd_step_mul)
        assert state.last_delta_sign == -1

    def test_swinging_back_below_beyond_the_threshold_tunes_again(self):
        """Each swing to the other side beyond the threshold counts."""
        params = PIDParams()
        state = _run([20.0, 21.3, 20.7], params=params)

        assert state.pid_kd == pytest.approx(DEFAULT_PID_KD * params.kd_step_mul**2)
        assert state.last_delta_sign == 1

    def test_a_new_target_is_not_read_as_a_crossing(self):
        """Lowering the target past the room does not count as an overshoot.

        Both targets fall into the same 0.5 °C bucket, so one state follows
        them; the room was below the old target and is above the new one.
        """
        state = _run([20.96, 20.96], target=[21.2, 20.75])

        assert state.pid_kp == DEFAULT_PID_KP
        assert state.pid_kd == DEFAULT_PID_KD
        assert state.last_delta_sign == -1


class TestKdRelaxation:
    """Kd falls back toward its default while the room holds the target."""

    def test_kd_relaxes_toward_the_default_inside_the_band(self):
        """Every tune inside the band lowers a raised Kd by the relax factor."""
        params = PIDParams()
        state = _run(
            [21.0] * 5, state=PIDState(pid_kd=10000.0), params=params, slope=0.0
        )

        assert state.pid_kd == pytest.approx(10000.0 * params.kd_relax_mul**5)

    def test_kd_stops_at_the_default(self):
        """A long hold brings Kd back to the default and no lower."""
        state = _run([21.0] * 400, state=PIDState(pid_kd=10000.0), slope=0.0)

        assert state.pid_kd == DEFAULT_PID_KD

    def test_kd_below_the_default_is_left_alone(self):
        """A Kd set below the default is not raised."""
        state = _run([21.0] * 20, state=PIDState(pid_kd=500.0), slope=0.0)

        assert state.pid_kd == 500.0

    def test_kd_holds_outside_the_band(self):
        """Away from the target nothing relaxes Kd."""
        state = _run([20.5] * 20, state=PIDState(pid_kd=10000.0))

        assert state.pid_kd == 10000.0


class TestClosedLoop:
    """The tuned gains over days of heating a simulated room."""

    @staticmethod
    def _heat_for_days(days: int) -> tuple[PIDState, float]:
        """Heat a room with a night setback; return the state and the peak Kd.

        The radiator lags its valve by 20 minutes and the room its radiator
        by two hours, so the morning warm-up overshoots the target. The
        sensor reports in 0.1 K steps.
        """
        state = PIDState()
        room = radiator = 19.0
        now = 1000.0
        peak_kd = 0.0
        substep_seconds = 30.0
        for _ in range(int(days * 86400 / _CYCLE_S)):
            hour = (now / 3600.0) % 24
            target = 21.0 if 6 <= hour < 22 else 17.0
            reading = round(room * 10.0) / 10.0
            params = PIDParams(
                kp=state.pid_kp if state.pid_kp is not None else DEFAULT_PID_KP,
                ki=state.pid_ki if state.pid_ki is not None else 0.01,
                kd=state.pid_kd if state.pid_kd is not None else DEFAULT_PID_KD,
            )
            percent, _, state = compute_pid(
                params, target, reading, radiator, None, "k", state=state, now=now
            )
            for _ in range(int(_CYCLE_S / substep_seconds)):
                radiator += (
                    substep_seconds * ((20.0 + 0.4 * percent) - radiator) / 1200.0
                )
                room += substep_seconds * (
                    (radiator - room) / 7200.0 + (5.0 - room) / 36000.0
                )
                now += substep_seconds
            assert state.pid_kd is not None
            peak_kd = max(peak_kd, state.pid_kd)
        return state, peak_kd

    def test_kd_rises_on_overshoots_and_returns_to_the_default(self):
        """Four days of morning overshoots raise Kd and leave it at the default.

        The overshoots do register, and Kd does not climb toward its upper
        limit between them; Kp does not collapse to its lower limit.
        """
        params = PIDParams()
        state, peak_kd = self._heat_for_days(4)

        assert DEFAULT_PID_KD < peak_kd < 1.5 * DEFAULT_PID_KD
        assert state.pid_kd == pytest.approx(DEFAULT_PID_KD)
        assert state.pid_kp is not None
        assert state.pid_kp > params.kp_min


class TestGainsSetOutsideTheTuningRange:
    """Auto-tuning moves a gain set outside its range into it in one step."""

    def test_a_sluggish_room_brings_kp_800_down_to_the_upper_limit(self):
        """Kp 800 becomes the 500 auto-tuning keeps to."""
        params = PIDParams()
        # 0.11 K below the target keeps the valve under 95 % even at Kp 800.
        state = _run([20.89], state=PIDState(pid_kp=800.0), slope=0.0)

        assert state.pid_kp == params.kp_max
