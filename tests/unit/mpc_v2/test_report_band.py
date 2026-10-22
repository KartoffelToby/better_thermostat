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


def test_the_threshold_is_the_median_step() -> None:
    """Smaller heartbeat steps and a single large jump leave the median alone."""
    band = ReportBand()
    for reading in (20.0, 20.5, 21.0, 21.2, 21.7, 19.2, 19.7):
        band.observe(reading)

    assert band.steps == pytest.approx([0.5, 0.5, 0.2, 0.5, 2.5, 0.5])
    assert band.threshold_K == pytest.approx(0.5)


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
    assert band.observe(21.3) == pytest.approx(0.5)
