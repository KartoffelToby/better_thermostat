"""A drop the cooler drives does not enter the learned heat loss.

Heat loss is the room's passive cooling. A measurement that sees the cooler
run learns nothing from the part it ran in.
"""

from datetime import UTC, datetime, timedelta

from homeassistant.components.climate.const import HVACAction
import pytest

from custom_components.better_thermostat.utils.thermal_learning import (
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
    temperature = 21.0
    for action, minutes, rate in segments:
        for _ in range(int(minutes)):
            tracker.update(temperature, action, _ts(minute))
            minute += 1.0
            temperature -= rate
        tracker.update(temperature, action, _ts(minute))
    tracker.update(temperature, HVACAction.HEATING, _ts(minute + 1.0))
    return minute + 2.0


class TestHeatLossIgnoresTheCooler:
    """A drop the cooler drives does not enter the heat loss rate."""

    def test_cooler_cycles_leave_the_rate_where_it_was(self):
        """Forty cycles of cooler-driven drops teach the tracker nothing."""
        tracker = HeatLossTracker()
        minute = 0.0
        for _ in range(40):
            minute = _loss_cycle(tracker, minute, [(HVACAction.COOLING, 30, 0.1)])

        assert tracker.heat_loss_rate == 0.01
        assert list(tracker.stats) == []

    @pytest.mark.parametrize(
        "segments",
        [
            pytest.param(
                [
                    (HVACAction.IDLE, 30, 0.02),
                    (HVACAction.COOLING, 30, 0.1),
                    (HVACAction.IDLE, 30, 0.02),
                ],
                id="idle-cooling-idle",
            ),
            pytest.param(
                [(HVACAction.COOLING, 30, 0.1), (HVACAction.IDLE, 30, 0.02)],
                id="cooling-idle",
            ),
        ],
    )
    def test_only_the_idle_stretch_after_the_cooler_is_measured(self, segments):
        """The idle stretch after the cooler stops is what the cycle measures."""
        tracker = HeatLossTracker()
        _loss_cycle(tracker, 0.0, segments)

        assert tracker.stats[-1]["rate"] == pytest.approx(0.02, abs=1e-9)
        assert tracker.heat_loss_rate == pytest.approx(
            ema_smooth(0.01, 0.02, _ALPHA), abs=1e-9
        )

    def test_an_idle_stretch_ended_by_the_cooler_is_discarded(self):
        """A stretch the cooler cuts short and heating follows teaches nothing."""
        tracker = HeatLossTracker()
        _loss_cycle(
            tracker, 0.0, [(HVACAction.IDLE, 30, 0.02), (HVACAction.COOLING, 30, 0.1)]
        )

        assert tracker.heat_loss_rate == 0.01


class TestHeatLossWaitsOutTheCoolerTail:
    """The room keeps falling fast for a few minutes after the cooler stops.

    Cold air still leaving the unit and the sensor catching up drive that
    fall, not the room's own loss, so the measurement waits the settle window
    out before it starts.
    """

    @pytest.mark.parametrize("tail_minutes", [2, 4, 8])
    def test_the_tail_after_the_cooler_stops_is_not_learned(self, tail_minutes):
        """A fast tail after the cooler leaves the measured rate at the true loss."""
        tracker = HeatLossTracker()
        _loss_cycle(
            tracker,
            0.0,
            [
                (HVACAction.COOLING, 30, 0.1),
                (HVACAction.IDLE, tail_minutes, 0.08),
                (HVACAction.IDLE, 40, 0.005),
            ],
        )

        assert tracker.stats[-1]["rate"] == pytest.approx(0.005, abs=1e-9)

    def test_a_stretch_shorter_than_the_settle_window_teaches_nothing(self):
        """Heating that resumes inside the settle window finalizes no cycle."""
        tracker = HeatLossTracker()
        _loss_cycle(
            tracker, 0.0, [(HVACAction.COOLING, 30, 0.1), (HVACAction.IDLE, 8, 0.02)]
        )

        assert list(tracker.stats) == []
        assert tracker.heat_loss_rate == 0.01

    @pytest.mark.parametrize("cycle_minutes", [5, 10, 12, 30])
    def test_the_settle_window_starts_when_the_cooler_is_switched_off(
        self, cycle_minutes
    ):
        """The measurement starts a full settle window after the cooler stops.

        The first reading without cooling is the cycle that switches the
        cooler off, so the fast tail follows that reading however long ago
        the previous cooling reading was.
        """
        tracker = HeatLossTracker()
        tracker.update(21.0, HVACAction.COOLING, _ts(0))
        off = float(cycle_minutes)
        tracker.update(21.0, HVACAction.IDLE, _ts(off))
        tracker.update(20.2, HVACAction.IDLE, _ts(off + 5))
        tracker.update(20.0, HVACAction.IDLE, _ts(off + 10))
        tracker.update(19.8, HVACAction.IDLE, _ts(off + 50))
        tracker.update(19.8, HVACAction.HEATING, _ts(off + 51))

        assert tracker.stats[-1]["rate"] == pytest.approx(0.005, abs=1e-9)
