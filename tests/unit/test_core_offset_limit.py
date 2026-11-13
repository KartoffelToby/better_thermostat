"""Pure tests for telling an offset stuck at its range limit from a busy one."""

import math

import pytest

from custom_components.better_thermostat.core.offset_limit import (
    CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS,
    OffsetAtLimit,
    advance_offset_at_limit,
    compute_offset_correction,
    is_correction_past_limit,
    is_offset_stuck_at_limit,
    limit_held_by,
)
from custom_components.better_thermostat.core.safety import (
    FALLBACK_MAX_OFFSET,
    FALLBACK_MIN_OFFSET,
    resolve_offset_bounds,
)
from custom_components.better_thermostat.core.snapshot import TrvReported


class TestLimitHeldBy:
    """An offset sits at a limit when it is within one step of it."""

    @pytest.mark.parametrize("calibration_offset", [12.7, 12.5, 12.2])
    def test_an_offset_within_a_step_of_the_upper_limit_sits_at_it(
        self, calibration_offset
    ):
        assert limit_held_by(calibration_offset, -12.7, 12.7, 0.5) == 12.7

    @pytest.mark.parametrize("calibration_offset", [-7.0, -6.5])
    def test_an_offset_within_a_step_of_the_lower_limit_sits_at_it(
        self, calibration_offset
    ):
        assert limit_held_by(calibration_offset, -7.0, 7.0, 0.5) == -7.0

    @pytest.mark.parametrize("calibration_offset", [12.1, 6.0, 0.5, -6.0, -12.1])
    def test_an_offset_more_than_a_step_inside_the_range_sits_at_neither(
        self, calibration_offset
    ):
        assert limit_held_by(calibration_offset, -12.7, 12.7, 0.5) is None

    def test_a_reading_off_the_limit_by_read_back_rounding_sits_at_it(self):
        """A device that reports its offset rounded is at the limit it holds."""
        assert limit_held_by(12.695, -12.7, 12.7, 0.0) == 12.7
        assert limit_held_by(12.68, -12.7, 12.7, 0.0) is None

    def test_zero_never_sits_at_a_limit_of_a_narrow_range(self):
        """A range two steps wide does not read as a limit everywhere."""
        assert limit_held_by(0.0, -0.5, 0.5, 0.5) is None
        assert limit_held_by(0.5, -0.5, 0.5, 0.5) == 0.5
        assert limit_held_by(-0.5, -0.5, 0.5, 0.5) == -0.5

    def test_zero_never_sits_at_a_limit_close_to_it(self):
        """A range that ends one step past zero does not make zero its limit."""
        assert limit_held_by(0.0, -12.7, 0.5, 0.5) is None
        assert limit_held_by(0.0, -0.5, 12.7, 0.5) is None
        assert limit_held_by(0.5, -12.7, 0.5, 0.5) == 0.5
        assert limit_held_by(-0.5, -0.5, 12.7, 0.5) == -0.5

    def test_a_narrow_range_counts_an_eighth_of_its_width_as_the_limit(self):
        """On a range of ±2 in whole steps, an offset of 1 is half way, no limit."""
        assert limit_held_by(1.0, -2.0, 2.0, 1.0) is None
        assert limit_held_by(1.5, -2.0, 2.0, 1.0) == 2.0
        assert limit_held_by(-1.0, -2.0, 2.0, 1.0) is None

    @pytest.mark.parametrize("step", [0.0, -1.0, math.nan, math.inf])
    def test_a_step_that_is_no_positive_number_counts_as_none(self, step):
        assert limit_held_by(12.7, -12.7, 12.7, step) == 12.7
        assert limit_held_by(12.6, -12.7, 12.7, step) is None

    @pytest.mark.parametrize(
        ("calibration_offset", "lower", "upper"),
        [
            (math.nan, -12.7, 12.7),
            (math.inf, -12.7, 12.7),
            (12.7, -12.7, math.inf),
            (-12.7, math.nan, 12.7),
        ],
    )
    def test_a_value_that_is_no_finite_number_sits_at_no_limit(
        self, calibration_offset, lower, upper
    ):
        assert limit_held_by(calibration_offset, lower, upper, 0.5) is None


class TestCorrection:
    """The correction is what the gap between room and reading asks for."""

    def test_a_head_that_offsets_its_reading_is_corrected_by_the_gap(self):
        """The reading carries the offset, so the gap adds to the offset held."""
        assert compute_offset_correction(
            25.0, 19.0, 5.0, shifts_setpoint=False
        ) == pytest.approx(11.0)
        assert compute_offset_correction(
            20.0, 25.0, -5.0, shifts_setpoint=False
        ) == pytest.approx(-10.0)

    def test_a_head_that_shifts_its_setpoint_is_corrected_by_the_negated_gap(self):
        """The reading carries no offset, so the offset held does not count."""
        assert compute_offset_correction(
            25.0, 19.0, 5.0, shifts_setpoint=True
        ) == pytest.approx(-6.0)
        assert compute_offset_correction(
            25.0, 19.0, -5.0, shifts_setpoint=True
        ) == pytest.approx(-6.0)

    @pytest.mark.parametrize(
        ("correction", "limit", "past"),
        [
            pytest.param(
                5.6, 5.0, True, id="above the upper limit by more than a step"
            ),
            pytest.param(5.5, 5.0, False, id="above the upper limit by one step"),
            pytest.param(4.0, 5.0, False, id="inside the range below the upper limit"),
            pytest.param(
                -5.6, -5.0, True, id="below the lower limit by more than a step"
            ),
            pytest.param(-5.5, -5.0, False, id="below the lower limit by one step"),
            pytest.param(5.6, -5.0, False, id="past the other limit"),
        ],
    )
    def test_a_correction_more_than_a_step_past_the_limit_is_past_it(
        self, correction, limit, past
    ):
        assert is_correction_past_limit(correction, limit, 0.5) is past

    def test_a_limit_of_zero_has_nothing_past_it(self):
        assert is_correction_past_limit(5.0, 0.0, 0.5) is False
        assert is_correction_past_limit(-5.0, 0.0, 0.5) is False

    @pytest.mark.parametrize("step", [0.0, -1.0, math.nan])
    def test_a_step_that_is_no_positive_number_counts_as_none(self, step):
        assert is_correction_past_limit(5.1, 5.0, step) is True
        assert is_correction_past_limit(5.005, 5.0, step) is False


class TestRun:
    """The run counts unbroken cycles at one limit."""

    def test_the_first_cycle_at_a_limit_starts_a_run(self):
        assert advance_offset_at_limit(None, 12.7, 100.0) == OffsetAtLimit(12.7, 100.0)

    def test_a_cycle_at_the_same_limit_keeps_the_start(self):
        run = OffsetAtLimit(12.7, 100.0)
        assert advance_offset_at_limit(run, 12.7, 400.0) is run

    def test_a_cycle_at_the_other_limit_starts_a_new_run(self):
        run = OffsetAtLimit(12.7, 100.0)
        assert advance_offset_at_limit(run, -12.7, 400.0) == OffsetAtLimit(-12.7, 400.0)

    def test_a_cycle_at_neither_limit_ends_the_run(self):
        assert advance_offset_at_limit(OffsetAtLimit(12.7, 100.0), None, 400.0) is None


class TestStuck:
    """An offset is stuck once its run has lasted the whole window."""

    def test_the_window_is_an_hour(self):
        assert CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS == 3600.0

    def test_no_run_is_never_stuck(self):
        assert is_offset_stuck_at_limit(None, 1e9) is False

    def test_a_run_shorter_than_the_window_is_not_stuck(self):
        run = OffsetAtLimit(12.7, 100.0)
        now = 100.0 + CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS - 1.0
        assert is_offset_stuck_at_limit(run, now) is False

    def test_a_run_as_long_as_the_window_is_stuck(self):
        run = OffsetAtLimit(12.7, 100.0)
        now = 100.0 + CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS
        assert is_offset_stuck_at_limit(run, now) is True

    def test_the_window_can_be_given(self):
        run = OffsetAtLimit(12.7, 0.0)
        assert is_offset_stuck_at_limit(run, 60.0, window_seconds=60.0) is True
        assert is_offset_stuck_at_limit(run, 59.0, window_seconds=60.0) is False


class TestOffsetBounds:
    """The limits are the ones the safety hull clamps the offset to."""

    def test_the_reported_range_is_the_range(self):
        reported = TrvReported(
            entity_id="climate.trv",
            min_local_calibration=-5.0,
            max_local_calibration=5.0,
        )
        assert resolve_offset_bounds(reported) == (-5.0, 5.0)

    def test_a_missing_bound_is_the_fallback(self):
        reported = TrvReported(entity_id="climate.trv", max_local_calibration=5.0)
        assert resolve_offset_bounds(reported) == (FALLBACK_MIN_OFFSET, 5.0)
        assert resolve_offset_bounds(None) == (FALLBACK_MIN_OFFSET, FALLBACK_MAX_OFFSET)
