"""TRV actuator model with four valve-flow profiles.

EQUAL_PERCENTAGE is the most realistic of the four for typical TRV
hardware: a given fractional change in commanded valve position
produces the same fractional change in delivered flow, which gives
the controller a roughly constant loop gain across the operating
range. See DESIGN.md §8 (actuator modelling; Karlsson 1980).

flow = (pct/100)^alpha       (alpha ≈ 3 for typical residential TRVs)

EXPONENTIAL is a fixed quadratic curve, ``flow = (pct/100)^2`` — the
alpha=2 special case of the power law, kept as a mild-curvature step
between LINEAR and EQUAL_PERCENTAGE. It ignores
``equal_percentage_exponent``.

LINEAR is the simplest reference profile and the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math


class ActuatorProfile(StrEnum):
    """Valve flow profile family."""

    LINEAR = "linear"
    THRESHOLD = "threshold"
    EXPONENTIAL = "exponential"
    EQUAL_PERCENTAGE = "equal_percentage"


@dataclass
class ActuatorParams:
    """Parameters describing actuator behaviour."""

    profile: ActuatorProfile = ActuatorProfile.LINEAR
    dead_zone_pct: float = 0.0
    hysteresis_pct: float = 0.0
    quantize_pct: float = 0.0  # 0 = continuous, e.g. 1.0 = integer percent
    # Exponent for equal-percentage profile; ignored by other profiles.
    # Typical values for residential TRVs: 2.5 - 4.0.
    equal_percentage_exponent: float = 3.0
    # Generic deadband: commands strictly below this percent produce zero
    # flow regardless of the profile. Distinct from ``dead_zone_pct``,
    # which only applies inside the THRESHOLD profile.
    deadband_pct: float = 0.0

    def __post_init__(self) -> None:
        """Reject non-physical actuator parameters.

        Raises
        ------
        ValueError
            If a parameter is not finite, a percent-domain field is
            outside [0, 100], or the equal-percentage exponent is < 1.
        """
        for name in (
            "dead_zone_pct",
            "hysteresis_pct",
            "quantize_pct",
            "equal_percentage_exponent",
            "deadband_pct",
        ):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"ActuatorParams {name} must be finite, got {value}")
        for name in ("dead_zone_pct", "hysteresis_pct", "quantize_pct", "deadband_pct"):
            value = getattr(self, name)
            if not (0.0 <= value <= 100.0):
                raise ValueError(
                    f"ActuatorParams {name} must be in [0, 100], got {value}"
                )
        if self.equal_percentage_exponent < 1.0:
            raise ValueError(
                "ActuatorParams equal_percentage_exponent must be >= 1, got "
                f"{self.equal_percentage_exponent}"
            )


class Actuator:
    """Maps a commanded valve percent to an effective flow in [0, 1]."""

    def __init__(self, params: ActuatorParams) -> None:
        self.params = params
        self._last_applied_pct: float = 0.0

    def apply(self, cmd_pct: float) -> float:
        """Translate a commanded percent into an effective flow in [0, 1]."""
        p = self.params
        pct = max(0.0, min(100.0, cmd_pct))

        if p.hysteresis_pct > 0.0:
            if abs(pct - self._last_applied_pct) < p.hysteresis_pct:
                pct = self._last_applied_pct

        if p.quantize_pct > 0.0:
            pct = round(pct / p.quantize_pct) * p.quantize_pct
            pct = max(0.0, min(100.0, pct))

        if p.deadband_pct > 0.0 and pct < p.deadband_pct:
            self._last_applied_pct = pct
            return 0.0

        if p.profile == ActuatorProfile.THRESHOLD:
            if pct < p.dead_zone_pct:
                flow = 0.0
            else:
                span = 100.0 - p.dead_zone_pct
                flow = (pct - p.dead_zone_pct) / span if span > 0.0 else 1.0
        elif p.profile == ActuatorProfile.EXPONENTIAL:
            # Fixed quadratic curve (power law with alpha = 2).
            flow = (pct / 100.0) ** 2
        elif p.profile == ActuatorProfile.EQUAL_PERCENTAGE:
            # Realistic TRV characteristic: equal-percentage curve
            # (modified power-law approximation, exponent >= 1 enforced
            # at construction).
            flow = (pct / 100.0) ** p.equal_percentage_exponent
        else:  # LINEAR
            flow = pct / 100.0

        self._last_applied_pct = pct
        return max(0.0, min(1.0, flow))
