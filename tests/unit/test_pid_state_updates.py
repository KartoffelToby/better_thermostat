"""Per-cycle state updates inside compute_pid.

Three writes happen alongside the control output: the slope EMA, the
integrator relief on a sign flip, and the error sign kept for the next cycle.
The first two are arithmetic on caller-owned state, so a non-numeric value
skips that single update and is recorded; the error sign is written on every
cycle.
"""

from __future__ import annotations

import logging

import pytest

from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    compute_pid,
)

_PID_LOGGER = "custom_components.better_thermostat.utils.calibration.pid"
_PARAMS = PIDParams(auto_tune=False, min_hold_time_s=0.0)


class _RefusesComparison:
    """A band that cannot be compared to a temperature difference."""

    def __ge__(self, other):
        return NotImplemented


class _FailsComparison:
    """A band whose comparison fails for a reason other than its type."""

    def __ge__(self, other):
        raise ValueError("boom")


class _RefusesSlopeWrite(PIDState):
    """A state whose slope EMA cannot be written."""

    def __setattr__(self, name, value):
        if name == "ema_slope" and value is not None:
            raise ValueError("boom")
        super().__setattr__(name, value)


def _compute(params: PIDParams, state: PIDState, **overrides):
    """Run one cycle with a fixed clock and a rising-temperature default."""
    kwargs = {
        "inp_target_temp_C": 21.0,
        "inp_current_temp_C": 20.0,
        "inp_trv_temp_C": 20.0,
        "inp_temp_slope_K_per_min": 0.02,
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

    def test_non_numeric_stored_value_is_traced(self, caplog):
        """A stored value that is not a number skips the blend and is recorded."""
        state = PIDState()
        state.ema_slope = "warm"  # type: ignore[assignment]
        with caplog.at_level(logging.DEBUG, logger=_PID_LOGGER):
            percent, _, out = _compute(_PARAMS, state)
        assert percent >= 0.0
        assert out.ema_slope == "warm"
        assert "slope EMA update skipped for bt:climate.trv" in caplog.text

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
            params, state, inp_target_temp_C=20.0, inp_current_temp_C=20.05, **overrides
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

    def test_non_numeric_band_is_traced(self, caplog):
        """A band that is not a number skips the relief and is recorded."""
        params = PIDParams(auto_tune=False, min_hold_time_s=0.0)
        params.steady_state_band_K = _RefusesComparison()  # type: ignore[assignment]
        with caplog.at_level(logging.DEBUG, logger=_PID_LOGGER):
            _, debug, _ = self._flip_cycle(params, self._flipped_state())
        assert debug["i_relief"] is False
        assert "integrator relief skipped for bt:climate.trv" in caplog.text

    def test_unexpected_band_failure_propagates(self):
        """A failure that is not a type mismatch reaches the caller."""
        params = PIDParams(auto_tune=False, min_hold_time_s=0.0)
        params.steady_state_band_K = _FailsComparison()  # type: ignore[assignment]
        with pytest.raises(ValueError):
            self._flip_cycle(params, self._flipped_state())


class TestErrorSign:
    """The error sign is recorded on every cycle."""

    @pytest.mark.parametrize(
        ("target", "current", "expected"),
        [(21.0, 20.0, 1), (20.0, 21.0, -1), (20.0, 20.0, 0)],
    )
    def test_sign_recorded(self, target, current, expected):
        """A positive, negative, and zero error each store their sign."""
        _, _, state = _compute(
            _PARAMS, PIDState(), inp_target_temp_C=target, inp_current_temp_C=current
        )
        assert state.last_error_sign == expected
