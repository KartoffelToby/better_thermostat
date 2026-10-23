"""Controller adapter protocol and shared dataclasses.

Every calibration controller — existing or future — is wrapped by an adapter
that conforms to :class:`ControllerAdapter`. The adapter's job is to translate
between the benchmark's universal :class:`BenchmarkContext` / :class:`BenchmarkOutput`
shape and the controller's native interface, with no behavior changes inside
the controller itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, ClassVar, Literal, Protocol


@dataclass(frozen=True)
class BenchmarkContext:
    """Read-only state visible to the controller at one simulation step."""

    t: float  # seconds since scenario start
    dt: float  # time since last step (seconds)
    target_temperature: float
    # The room temperature Better Thermostat has committed from the sensor
    # reports (see ``input_stage``); what production hands its controllers.
    room_temperature: float
    raw_room_temperature: float  # plant-internal truth (for adapters that cheat-peek)
    trv_temperature: float | None  # the TRV's own sensor reading, if supported
    outdoor_temperature: float
    window_open: bool = False
    solar_intensity: float = 0.0  # 0.0 - 1.0
    last_valve_percent: float = 0.0
    # The bias plus drift inside ``room_temperature``, sampled and filtered
    # like the reading; the noise-free part of the sensor's error that no
    # controller can observe.
    sensor_calibration_error_K: float = 0.0
    # Production's EMA of the committed room temperature and the slope its
    # minute tick derives from it; ``None`` before the first tick.
    room_temperature_filtered: float | None = None
    temperature_slope_K_per_min: float | None = None
    # The TRV's internal measurement before it rounds its report; the TRV's
    # own regulation runs on it, Better Thermostat never sees it.
    trv_internal_temperature: float | None = None
    # Whether Better Thermostat runs a control cycle at this step: an event
    # requests one (first step, a committed room reading, a changed TRV
    # report, a setpoint or window change), and the 300 s recompute tick
    # adds one for the modes that register it.
    control_event: bool = True
    control_tick: bool = True
    # The heating water reaching the radiator, as a flow sensor reports it,
    # and the value an installer configures for it; ``None`` where the
    # caller does not model the heat source.
    flow_temperature: float | None = None
    nominal_flow_temperature: float | None = None
    # ``False`` while the heat source delivers no heat, as while a heat
    # pump heats hot water.
    heat_delivered: bool = True


@dataclass(frozen=True)
class BenchmarkOutput:
    """What the controller produced at one step.

    Exactly one of ``valve_percent`` / ``setpoint_offset_K`` / ``duty_cycle_percent``
    should be set, matching the controller's :attr:`family`. The one exception:
    duty-family controllers additionally mirror their duty cycle into
    ``valve_percent``, because the plant is actuated through ``valve_percent``
    regardless of family.
    """

    valve_percent: float | None = None
    setpoint_offset_K: float | None = None
    duty_cycle_percent: float | None = None
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
            for name in ("valve_percent", "setpoint_offset_K", "duty_cycle_percent")
            if getattr(self, name) is not None
        }
        if not populated:
            raise ValueError(
                "BenchmarkOutput requires one of valve_percent, "
                "setpoint_offset_K, duty_cycle_pct"
            )
        if len(populated) > 1 and populated != {"valve_percent", "duty_cycle_percent"}:
            raise ValueError(
                "BenchmarkOutput allows only one output family "
                f"(or duty_cycle_pct mirrored into valve_percent), got: {sorted(populated)}"
            )
        valve, duty = self.valve_percent, self.duty_cycle_percent
        if (
            populated == {"valve_percent", "duty_cycle_percent"}
            and valve is not None
            and duty is not None
            and not math.isclose(valve, duty, abs_tol=1e-9)
        ):
            raise ValueError(
                "BenchmarkOutput duty_cycle_pct must be mirrored into valve_percent "
                f"(got valve_percent={valve}, duty_cycle_pct={duty})"
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


class ControlCycleGate:
    """Hold a production controller's output between control cycles.

    Better Thermostat computes a controller only when a control cycle runs;
    between cycles the TRV keeps the last command. A subclass implements
    :meth:`_compute`, and its ``reset`` clears ``_held``.
    """

    # Whether the mode registers the 300 s recompute tick.
    runs_on_tick: ClassVar[bool] = True
    _held: BenchmarkOutput | None = None

    def _compute(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        raise NotImplementedError

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute on a control cycle, otherwise repeat the last output."""
        cycle = ctx.control_event or (self.runs_on_tick and ctx.control_tick)
        if self._held is None or cycle:
            self._held = self._compute(ctx)
        return self._held
