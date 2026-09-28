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
    temp = 21.0
    for action, minutes, rate in segments:
        for _ in range(int(minutes)):
            tracker.update(temp, action, _ts(minute))
            minute += 1.0
            temp -= rate
        tracker.update(temp, action, _ts(minute))
    tracker.update(temp, HVACAction.HEATING, _ts(minute + 1.0))
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
