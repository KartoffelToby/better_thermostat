"""Unit tests for the reporting-threshold estimate of the room sensor."""

from __future__ import annotations

import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.report_band import (
    MAX_REPORT_THRESHOLD_K,
    MIN_REPORT_STEPS,
    MIN_REPORT_THRESHOLD_K,
    REPORT_STEP_WINDOW,
    ReportBand,
)


def test_the_first_reading_is_fresh() -> None:
    """Without a previous reading nothing can repeat."""
    band = ReportBand()

    assert band.observe(21.0) is None
    assert band.last_reading == 21.0


def test_a_repeat_returns_the_threshold_and_a_change_returns_none() -> None:
    """The same value again is a repeat; a different one is a fresh report."""
    band = ReportBand()
    for reading in (21.0, 21.5, 22.0, 21.5):
        band.observe(reading)

    assert band.observe(21.5) == pytest.approx(0.5)
    assert band.observe(21.0) is None


def test_too_few_steps_leave_the_threshold_at_the_lower_bound() -> None:
    """A lone large step, such as a setpoint jump, does not set the threshold.

    Until ``MIN_REPORT_STEPS`` steps are seen, a repeat counts almost as a
    fresh reading.
    """
    band = ReportBand()
    readings = [21.0 - 4.0 * k for k in range(MIN_REPORT_STEPS)]
    for reading in readings:
        band.observe(reading)
    assert band.observe(readings[-1]) == MIN_REPORT_THRESHOLD_K

    band.observe(readings[-1] - 4.0)

    assert band.observe(readings[-1] - 4.0) == MAX_REPORT_THRESHOLD_K


def test_triggered_steps_set_the_threshold_among_many_heartbeat_steps() -> None:
    """A room that holds its temperature reports mostly by heartbeat.

    Two triggered 0.5 K steps among eight smaller heartbeat steps still give
    the sensor's threshold; a 2.5 K jump stays out of the estimate.
    """
    band = ReportBand()
    for reading in (
        21.0,
        21.1,
        21.3,
        21.2,
        21.0,
        20.5,
        20.7,
        20.8,
        20.6,
        21.1,
        21.0,
        18.5,
    ):
        band.observe(reading)

    assert band.steps == pytest.approx(
        [0.1, 0.2, 0.1, 0.2, 0.5, 0.2, 0.1, 0.2, 0.5, 0.1, 2.5]
    )
    assert band.threshold_K == pytest.approx(0.5)


def test_a_single_odd_step_does_not_set_the_threshold() -> None:
    """Over a full window of steps, one well above the others leaves it alone."""
    band = ReportBand()
    reading = 20.0
    band.observe(reading)
    for _ in range(REPORT_STEP_WINDOW - 1):
        reading += 0.1
        band.observe(reading)
    band.observe(reading + 0.9)

    assert len(band.steps) == REPORT_STEP_WINDOW
    assert max(band.steps) == pytest.approx(0.9)
    assert band.threshold_K == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        pytest.param([0.1, 0.1, 0.1], 0.1, id="interpolates-between-equal-steps"),
        pytest.param([0.1, 0.2, 0.3], 0.28, id="interpolates-between-the-top-two"),
        pytest.param([0.3, 0.1, 0.2, 0.1, 0.1], 0.26, id="order-does-not-matter"),
    ],
)
def test_the_threshold_is_the_upper_quantile_of_the_steps(
    steps: list[float], expected: float
) -> None:
    """The estimate interpolates linearly between the sorted steps."""
    band = ReportBand()

    band.restore(None, steps)

    assert band.threshold_K == pytest.approx(expected)


def test_steps_all_above_the_ceiling_give_the_ceiling() -> None:
    """Only room events seen: the threshold is at least the upper bound."""
    band = ReportBand()

    band.restore(None, [2.0, 3.0, 4.0])

    assert band.threshold_K == MAX_REPORT_THRESHOLD_K


@pytest.mark.parametrize(
    ("steps", "factor"),
    [
        pytest.param([], 1.0, id="no-steps"),
        pytest.param([0.1] * 5, 1.0, id="fine-sensor"),
        pytest.param([0.5] * 5, 25.0, id="half-kelvin"),
        pytest.param([1.0] * 5, 100.0, id="one-kelvin"),
    ],
)
def test_move_suppression_grows_with_the_square_of_the_threshold(
    steps: list[float], factor: float
) -> None:
    """The finest sensor plans with the plain weight; a coarser one with ``r²``."""
    band = ReportBand()

    band.restore(None, steps)

    assert band.move_suppression_factor == pytest.approx(factor)


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        pytest.param(0.01, MIN_REPORT_THRESHOLD_K, id="finer-than-the-floor"),
        pytest.param(3.0, MAX_REPORT_THRESHOLD_K, id="coarser-than-the-ceiling"),
    ],
)
def test_the_threshold_stays_inside_its_bounds(step: float, expected: float) -> None:
    """Steps beyond either bound give the bound, not the step."""
    band = ReportBand()
    for k in range(5):
        band.observe(20.0 + step * k)

    assert band.threshold_K == expected


def test_only_the_recent_steps_count() -> None:
    """A sensor reconfigured to a finer threshold is learned again."""
    band = ReportBand()
    reading = 20.0
    for _ in range(REPORT_STEP_WINDOW):
        reading += 0.5
        band.observe(reading)
    for _ in range(REPORT_STEP_WINDOW):
        reading += 0.2
        band.observe(reading)

    assert len(band.steps) == REPORT_STEP_WINDOW
    assert band.threshold_K == pytest.approx(0.2)


def test_restore_adopts_the_reading_and_drops_steps_without_information() -> None:
    """A persisted history comes back without zero or negative steps."""
    band = ReportBand()

    band.restore(21.3, [0.5, 0.0, -0.2, 0.4, 0.6])

    assert band.last_reading == 21.3
    assert band.steps == [0.5, 0.4, 0.6]
    assert band.observe(21.3) == pytest.approx(0.58)
