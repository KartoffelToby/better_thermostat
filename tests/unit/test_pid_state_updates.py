"""Per-cycle state updates inside compute_pid.

Three writes happen alongside the control output: the slope EMA, the
integrator relief on a sign flip, and the error sign kept for the next cycle.
The first two are arithmetic on caller-owned state, so a non-numeric value
skips that single update and is recorded; the error sign is written on every
cycle.
"""

from __future__ import annotations

import pytest

from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    compute_pid,
    observe_standby,
)

_PID_LOGGER = "custom_components.better_thermostat.utils.calibration.pid"
_PARAMS = PIDParams(auto_tune=False, min_hold_time_s=0.0)


class _RefusesSlopeWrite(PIDState):
    """A state whose slope EMA cannot be written."""

    def __setattr__(self, name, value):
        if name == "ema_slope" and value is not None:
            raise ValueError("boom")
        super().__setattr__(name, value)


def _compute(params: PIDParams, state: PIDState, **overrides):
    """Run one cycle with a fixed clock and a rising-temperature default."""
    kwargs = {
        "inp_target_temperature": 21.0,
        "inp_room_temperature": 20.0,
        "inp_trv_temperature": 20.0,
        "inp_temperature_slope_K_per_min": 0.02,
        "key": "bt:climate.trv",
    }
    kwargs.update(overrides)
    return compute_pid(params=params, state=state, now=1000.0, **kwargs)


class TestSlopeEma:
    """The slope EMA blends the new reading into the stored one."""

    def test_first_reading_seeds_the_ema(self):
        """Without a stored value the reading is adopted as is."""
        _, _, state = _compute(_PARAMS, PIDState())
        assert state.ema_slope == pytest.approx(0.02)

    def test_further_readings_are_blended(self):
        """A stored value is blended 60/40 with the new reading."""
        _, _, state = _compute(_PARAMS, PIDState(ema_slope=0.07))
        assert state.ema_slope == pytest.approx(0.6 * 0.07 + 0.4 * 0.02)

    def test_unexpected_write_failure_propagates(self):
        """A failure that is not a type mismatch reaches the caller."""
        with pytest.raises(ValueError):
            _compute(_PARAMS, _RefusesSlopeWrite())


class TestIntegratorRelief:
    """A sign flip inside the steady-state band relieves the integrator."""

    @staticmethod
    def _flipped_state() -> PIDState:
        """State whose previous error had the opposite sign."""
        return PIDState(last_error_sign=1, pid_integral=40.0, pid_last_time=990.0)

    def _flip_cycle(self, params: PIDParams, state: PIDState, **overrides):
        """Run a cycle whose error is small and negative."""
        return _compute(
            params,
            state,
            inp_target_temperature=20.0,
            inp_room_temperature=20.05,
            **overrides,
        )

    def test_relief_applies_on_a_sign_flip(self):
        """The relief flag is reported and the integrator shrinks."""
        _, debug, state = self._flip_cycle(_PARAMS, self._flipped_state())
        assert debug["i_relief"] is True
        assert state.pid_integral < 40.0

    def test_no_relief_without_a_sign_flip(self):
        """A same-sign error leaves the integrator alone."""
        state = PIDState(last_error_sign=-1, pid_integral=40.0, pid_last_time=990.0)
        _, debug, _ = self._flip_cycle(_PARAMS, state)
        assert debug["i_relief"] is False


class TestErrorSign:
    """The error sign is recorded on every cycle."""

    @pytest.mark.parametrize(
        ("target", "current", "expected"),
        [(21.0, 20.0, 1), (20.0, 21.0, -1), (20.0, 20.0, 0)],
    )
    def test_sign_recorded(self, target, current, expected):
        """A positive, negative, and zero error each store their sign."""
        _, _, state = _compute(
            _PARAMS,
            PIDState(),
            inp_target_temperature=target,
            inp_room_temperature=current,
        )
        assert state.last_error_sign == expected


class TestStandbyObservation:
    """Standby follows the room only when there is a reading to follow."""

    def test_no_reading_leaves_the_measurement_chain(self):
        """Without a room or smoothed temperature the chain keeps its last point."""
        state = PIDState(pid_last_meas=20.4, pid_last_time=900.0)

        result = observe_standby(_PARAMS, state, None, now=1000.0)

        assert result is state
        assert (state.pid_last_meas, state.pid_last_time) == (20.4, 900.0)

    def test_a_reading_moves_the_measurement_chain(self):
        """A room temperature during standby advances the chain's time stamp."""
        state = PIDState(pid_last_meas=20.4, pid_last_time=900.0)

        observe_standby(_PARAMS, state, 20.8, now=1000.0)

        assert state.pid_last_time == 1000.0
        assert state.pid_last_meas != 20.4

    def test_the_smoothed_reading_is_followed_over_the_raw_one(self):
        """A filtered room temperature is the one the chain follows."""
        state = PIDState()

        observe_standby(
            _PARAMS, state, 20.0, now=1000.0, inp_room_temperature_filtered=21.0
        )

        assert state.pid_last_meas == 21.0

    def test_the_filtered_reading_alone_moves_the_chain(self):
        """Without a raw reading the filtered one still advances the chain."""
        state = PIDState()

        observe_standby(
            _PARAMS, state, None, now=1000.0, inp_room_temperature_filtered=21.0
        )

        assert (state.pid_last_meas, state.pid_last_time) == (21.0, 1000.0)

    def test_derivative_on_error_takes_the_reading_unsmoothed(self):
        """With the D channel on the error, standby stores the reading as is."""
        params = PIDParams(auto_tune=False, d_on_measurement=False)
        state = PIDState(pid_last_meas=20.0, pid_last_time=900.0)

        observe_standby(params, state, 21.0, now=1000.0)

        assert (state.pid_last_meas, state.pid_last_time) == (21.0, 1000.0)

    def test_the_smoothing_factor_weights_the_new_reading(self):
        """A valid smoothing factor sets the weight of the new reading."""
        params = PIDParams(auto_tune=False, d_smoothing_alpha=0.25)
        state = PIDState(pid_last_meas=20.0, pid_last_time=900.0)

        observe_standby(params, state, 21.0, now=1000.0)

        assert state.pid_last_meas == pytest.approx(20.25)
