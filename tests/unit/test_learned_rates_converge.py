"""The learned thermal rates converge on what the cycles measure.

The state the trackers carry keeps full precision, so no rounding band stops
the EMA short of the measured rate.
"""

from datetime import UTC, datetime, timedelta

from homeassistant.components.climate.const import HVACAction
import pytest

from custom_components.better_thermostat.utils.thermal_learning import (
    HeatingPowerTracker,
    HeatLossTracker,
    ema_smooth,
)

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_ALPHA = 0.1


def _ts(minutes: float) -> datetime:
    return _NOW + timedelta(minutes=minutes)


def _loss_cycle(
    tracker: HeatLossTracker,
    start: float,
    segments: list[tuple[HVACAction, float, float]],
) -> float:
    """Feed one sample per minute through ``segments`` and end on HEATING.

    Each segment is ``(action, minutes, rate)``: the room falls by ``rate``
    per minute while ``action`` holds. Returns the minute the cycle ended on.
    """
    minute = start
    temp = 21.0
    for action, minutes, rate in segments:
        for _ in range(int(minutes)):
            tracker.update(temp, action, _ts(minute))
            minute += 1.0
            temp -= rate
        tracker.update(temp, action, _ts(minute))
    tracker.update(temp, HVACAction.HEATING, _ts(minute + 1.0))
    return minute + 2.0


def _heating_cycle(tracker: HeatingPowerTracker, start: float, rise: float) -> float:
    """Heat for 100 minutes by ``rise`` K, then let the room fall off."""
    tracker.update(20.0, HVACAction.IDLE, _ts(start))
    tracker.update(20.0, HVACAction.HEATING, _ts(start + 1.0))
    tracker.update(20.0 + rise, HVACAction.IDLE, _ts(start + 101.0))
    tracker.update(20.0 + rise - 0.1, HVACAction.IDLE, _ts(start + 102.0))
    return start + 103.0


class TestLearnedRatesConverge:
    """The learned rates reach the measured rate instead of stalling short."""

    def test_heating_power_converges_on_the_measured_rate(self):
        """Repeated cycles at 0.01234 K/min bring the heating power there."""
        tracker = HeatingPowerTracker()
        expected = tracker.heating_power
        minute = 0.0
        for _ in range(300):
            minute = _heating_cycle(tracker, minute, 1.234)
            expected = ema_smooth(expected, 1.234 / 100.0, _ALPHA)

        assert tracker.heating_power == pytest.approx(expected, abs=1e-9)
        assert tracker.heating_power == pytest.approx(0.01234, abs=1e-9)

    def test_the_slowest_alpha_still_moves_the_heating_power(self):
        """At the smallest alpha a cycle 10 % above the learned power moves it."""
        tracker = HeatingPowerTracker()
        # A target at the bottom of the observed range and an outdoor
        # temperature above it give the smallest alpha the tracker uses.
        tracker.min_target, tracker.max_target = 18.0, 24.0
        before = tracker.heating_power
        context = {"heat_target_temperature": 18.0, "outdoor_temperature": 25.0}
        tracker.update(20.0, HVACAction.IDLE, _ts(0.0), **context)
        tracker.update(20.0, HVACAction.HEATING, _ts(1.0), **context)
        tracker.update(21.1, HVACAction.IDLE, _ts(101.0), **context)
        tracker.update(21.0, HVACAction.IDLE, _ts(102.0), **context)

        assert tracker.stats[-1]["alpha"] == 0.035
        assert tracker.heating_power == pytest.approx(
            ema_smooth(before, 0.011, 0.035), abs=1e-9
        )

    def test_heat_loss_converges_on_the_measured_rate(self):
        """Repeated idle drops at 0.012345 K/min bring the heat loss there."""
        tracker = HeatLossTracker()
        expected = tracker.heat_loss_rate
        minute = 0.0
        for _ in range(300):
            minute = _loss_cycle(tracker, minute, [(HVACAction.IDLE, 40, 0.012345)])
            expected = ema_smooth(expected, 0.012345, _ALPHA)

        assert tracker.heat_loss_rate == pytest.approx(expected, abs=1e-9)
        assert tracker.heat_loss_rate == pytest.approx(0.012345, abs=1e-9)
