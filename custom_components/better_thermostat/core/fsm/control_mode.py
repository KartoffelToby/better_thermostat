"""Control-mode region: OPTIMAL -> SENSOR_FALLBACK -> HOLD.

Today this region is pure annunciation: it lifts the watcher's
``degraded_mode`` boolean into an explicit state and records what is
degraded since when. It does not influence the control law — giving it
effect (the fail-soft ladder, including the HOLD rung) is M8's product
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ControlMode(StrEnum):
    """Discrete rungs of the (future) degradation ladder."""

    OPTIMAL = "optimal"
    SENSOR_FALLBACK = "sensor_fallback"
    HOLD = "hold"


@dataclass(frozen=True)
class ControlModeState:
    """State of the control-mode region (annunciation only)."""

    mode: ControlMode = ControlMode.OPTIMAL
    unavailable_sensors: tuple[str, ...] = ()
    degraded_since: float | None = None

    @property
    def degraded(self) -> bool:
        """Mirror of the legacy ``degraded_mode`` boolean."""
        return self.mode != ControlMode.OPTIMAL


def step(
    state: ControlModeState, unavailable_sensors: list[str], now: float
) -> ControlModeState:
    """Advance the region from the watcher's sensor availability check.

    Mirrors the legacy logic one to one: any unavailable optional sensor
    means SENSOR_FALLBACK, none means OPTIMAL. ``degraded_since`` keeps
    the timestamp of the first transition into degradation.
    """
    if not unavailable_sensors:
        return ControlModeState()
    return ControlModeState(
        mode=ControlMode.SENSOR_FALLBACK,
        unavailable_sensors=tuple(unavailable_sensors),
        degraded_since=state.degraded_since if state.degraded else now,
    )
