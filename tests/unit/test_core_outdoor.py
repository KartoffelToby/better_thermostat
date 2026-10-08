"""Pure tests for the damped outdoor temperature and the summer-mode switch."""

import math

import pytest

from custom_components.better_thermostat.core.outdoor import (
    OUTDOOR_DAMPING_TIME_CONSTANT,
    SUMMER_MODE_HYSTERESIS_KELVIN,
    add_reading,
    damped_value_at,
    heat_threshold,
    start_damping,
)

HOUR = 3600.0
DAY = 24 * HOUR
TAU = OUTDOOR_DAMPING_TIME_CONSTANT.total_seconds()

# A sun-exposed sensor on a clear October day (issue #2645): 6 to 9 °C at
# night, 35 °C at noon, back to 12 °C by midnight. (hour, °C)
SUNNY_DAY = [
    (0, 9.5),
    (4, 7.5),
    (8, 6.0),
    (8.5, 7.0),
    (8.8, 12.0),
    (9.3, 22.0),
    (9.8, 25.5),
    (10.3, 25.5),
    (10.4, 24.0),
    (11, 24.0),
    (11.6, 28.0),
    (12.2, 35.0),
    (12.7, 31.0),
    (12.9, 29.5),
    (13.3, 33.0),
    (13.7, 28.0),
    (14.5, 25.0),
    (15, 26.0),
    (16, 24.5),
    (18, 19.0),
    (20, 15.0),
    (22, 13.0),
    (24, 12.3),
]


def _sunny_temperature(seconds: float) -> float:
    """Return the SUNNY_DAY curve at ``seconds``, repeating every day."""
    hour = (seconds % DAY) / HOUR
    for (h0, t0), (h1, t1) in zip(SUNNY_DAY, SUNNY_DAY[1:], strict=False):
        if h0 <= hour <= h1:
            return t0 + (t1 - t0) * (hour - h0) / (h1 - h0)
    raise AssertionError(hour)


def _report_on_change(days: float, step: float) -> list[tuple[float, float]]:
    """Sample the sunny curve every minute and report each ``step`` change.

    Returns (time, reading) pairs the way a Zigbee sensor sends them: many
    while the temperature moves, few while it holds.
    """
    reports: list[tuple[float, float]] = []
    last = None
    for minute in range(int(days * 24 * 60)):
        at = minute * 60.0
        reading = round(_sunny_temperature(at) / step) * step
        if reading != last:
            reports.append((at, reading))
            last = reading
    return reports


def _damp(reports):
    state = None
    for at, reading in reports:
        state = add_reading(state, reading, at)
    return state


class TestFilter:
    """The first-order filter over held readings."""

    def test_the_first_reading_starts_the_filter(self):
        state = add_reading(None, 7.5, 100.0)
        assert state == start_damping(7.5, 100.0)
        assert damped_value_at(state, 100.0) == 7.5

    def test_a_step_moves_the_value_by_63_percent_in_one_time_constant(self):
        state = add_reading(start_damping(0.0, 0.0), 10.0, 1.0)
        assert damped_value_at(state, 1.0 + TAU) == pytest.approx(
            10.0 * (1 - math.exp(-1))
        )

    def test_a_reading_counts_from_when_it_arrives(self):
        """The new reading has no weight at the moment it arrives."""
        state = add_reading(start_damping(5.0, 0.0), 30.0, DAY)
        assert damped_value_at(state, DAY) == pytest.approx(5.0)

    def test_the_held_reading_pulls_the_value_until_the_next_one(self):
        state = start_damping(5.0, 0.0)
        later = add_reading(state, 6.0, 6 * HOUR)
        assert later.value == pytest.approx(5.0)
        assert damped_value_at(later, 12 * HOUR) == pytest.approx(
            5.0 + (6.0 - 5.0) * -math.expm1(-6 * HOUR / TAU)
        )

    def test_a_time_before_the_last_reading_returns_its_value(self):
        state = add_reading(start_damping(5.0, 0.0), 9.0, HOUR)
        assert damped_value_at(state, 0.0) == state.value

    def test_the_same_state_passed_twice_counts_once(self):
        state = add_reading(start_damping(5.0, 0.0), 9.0, HOUR)
        assert add_reading(state, 9.0, HOUR) is state

    def test_an_older_reading_is_ignored(self):
        state = add_reading(start_damping(5.0, 0.0), 9.0, HOUR)
        assert add_reading(state, 30.0, HOUR / 2) is state

    @pytest.mark.parametrize("reading", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_reading_is_ignored(self, reading):
        state = start_damping(5.0, 0.0)
        assert add_reading(state, reading, HOUR) is state
        assert add_reading(None, reading, HOUR) is None


class TestReportingRate:
    """Issue #2645: the damped value must not depend on how often a sensor reports."""

    def test_reports_on_every_tenth_and_every_half_degree_agree(self):
        fine = _damp(_report_on_change(3, 0.1))
        coarse = _damp(_report_on_change(3, 0.5))
        at = 3 * DAY - 60.0
        assert damped_value_at(fine, at) == pytest.approx(
            damped_value_at(coarse, at), abs=0.2
        )

    def test_a_burst_of_warm_readings_weighs_by_duration_not_count(self):
        """A hundred readings in one warm minute weigh as one warm minute."""
        state = start_damping(8.0, 0.0)
        for i in range(100):
            state = add_reading(state, 30.0, DAY + i * 0.6)
        state = add_reading(state, 8.0, DAY + 60.0)
        assert damped_value_at(state, 2 * DAY) == pytest.approx(8.0, abs=0.02)

    def test_the_sunny_afternoon_does_not_switch_the_room_off(self):
        """The reporter lowered the threshold to 18 °C; the room has to heat.

        The count-weighted daily mean put the curve at about 22 °C and kept
        the TRVs off. Held for as long as each reading was current, the
        damped temperature stays below 18 °C at every hour of the third day.
        """
        state = None
        reports = iter(_report_on_change(3, 0.1))
        call_for_heat = True
        pending = next(reports)
        for hour in range(2 * 24, 3 * 24):
            at = hour * HOUR
            while pending is not None and pending[0] <= at:
                state = add_reading(state, pending[1], pending[0])
                pending = next(reports, None)
            damped = damped_value_at(state, at)
            call_for_heat = damped < heat_threshold(18.0, call_for_heat)
            assert call_for_heat, (hour, damped)

    def test_midnight_does_not_move_the_value(self):
        """No calendar-day buckets: the value is continuous across midnight."""
        state = _damp([r for r in _report_on_change(3, 0.1) if r[0] < 2 * DAY])
        before = damped_value_at(state, 2 * DAY - 1.0)
        after = damped_value_at(state, 2 * DAY + 1.0)
        assert after == pytest.approx(before, abs=0.001)


class TestHeatThreshold:
    """The hysteresis band between switching off and resuming."""

    def test_a_heating_room_stops_at_the_off_temperature(self):
        assert heat_threshold(18.0, True) == 18.0

    def test_a_room_in_summer_mode_resumes_below_the_band(self):
        assert heat_threshold(18.0, False) == 18.0 - SUMMER_MODE_HYSTERESIS_KELVIN

    def test_a_temperature_hovering_at_the_threshold_switches_once(self):
        call_for_heat = True
        flips = 0
        for damped in [17.9, 18.0, 17.9, 18.1, 17.5, 18.0, 17.2]:
            new = damped < heat_threshold(18.0, call_for_heat)
            flips += new != call_for_heat
            call_for_heat = new
        assert flips == 1
        assert call_for_heat is False
        assert (16.9 < heat_threshold(18.0, call_for_heat)) is True
