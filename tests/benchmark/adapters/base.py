"""Controller adapter protocol and shared dataclasses.

Every calibration controller — existing or future — is wrapped by an adapter
that conforms to :class:`ControllerAdapter`. The adapter's job is to translate
between the benchmark's universal :class:`BenchmarkContext` / :class:`BenchmarkOutput`
shape and the controller's native interface, with no behavior changes inside
the controller itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class BenchmarkContext:
    """Read-only state visible to the controller at one simulation step."""

    t: float  # seconds since scenario start
    dt: float  # time since last step (seconds)
    target_temp_C: float
    current_temp_C: float  # measurement coming out of the sensor model
    raw_room_temp_C: float  # plant-internal truth (for adapters that cheat-peek)
    trv_temp_C: float | None  # radiator surface, if supported
    outdoor_temp_C: float
    window_open: bool = False
    solar_intensity: float = 0.0  # 0.0 - 1.0
    last_valve_percent: float = 0.0


@dataclass(frozen=True)
class BenchmarkOutput:
    """What the controller produced at one step.

    Exactly one of ``valve_percent`` / ``setpoint_offset_K`` / ``duty_cycle_pct``
    should be set, matching the controller's :attr:`family`. The one exception:
    duty-family controllers additionally mirror their duty cycle into
    ``valve_percent``, because the plant is actuated through ``valve_percent``
    regardless of family.
    """

    valve_percent: float | None = None
    setpoint_offset_K: float | None = None
    duty_cycle_pct: float | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the one-output contract at the adapter boundary.

        Raises
        ------
        ValueError
            If no output field is populated, or if a combination other
            than the documented duty+valve pairing is populated.
        """
        populated = {
            name
            for name in ("valve_percent", "setpoint_offset_K", "duty_cycle_pct")
            if getattr(self, name) is not None
        }
        if not populated:
            raise ValueError(
                "BenchmarkOutput requires one of valve_percent, "
                "setpoint_offset_K, duty_cycle_pct"
            )
        if len(populated) > 1 and populated != {"valve_percent", "duty_cycle_pct"}:
            raise ValueError(
                "BenchmarkOutput allows only one output family "
                f"(or duty_cycle_pct mirrored into valve_percent), got: {sorted(populated)}"
            )


ControllerFamily = Literal["valve", "offset", "duty"]


class ControllerAdapter(Protocol):
    """Structural type every controller adapter must satisfy."""

    name: str
    family: ControllerFamily

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Drop all learned state; optionally seed from ``prior``."""
        ...

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Produce one control action for ``ctx``."""
        ...

    def export_state(self) -> dict[str, Any]:
        """Return a serializable snapshot of current controller state."""
        ...
