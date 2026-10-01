"""IndirectTrvAdapter — wrap any controller in an offset-based TRV layer.

Tado, Bosch BTH-RA, Sonoff TRVZB offset-mode and similar TRVs do **not**
accept a raw valve-percent command. They run their own closed loop and
only expose a setpoint (typically quantised to 0.5 K). Home Assistant
integrations like Better Thermostat fool such hardware into tracking an
external sensor by pushing a *calibrated* setpoint, but the TRV's own
P-regulator and hysteresis sit between BT's command and the physical
valve.

This wrapper sits on top of any benchmark adapter and converts the
controller's ``valve_percent`` decision into:

1. A *desired TRV setpoint* (high when BT wants heat, below the TRV's
   own reading when not),
2. Quantised to ``setpoint_step_K``,
3. Optionally held by a hysteresis band before it changes,
4. Driven through a small TRV-internal P-loop using the TRV's own
   reported temperature.

The default ``"production"`` mapping follows the setpoint channel of
``calculate_calibration_setpoint`` in ``calibration.py`` for the
controller modes: ``T_trv + (T_max − T_trv)·u``, a setpoint pushed below
the TRV's reading when ``u`` is zero, and rounding up while heating and
down while idle. The TRV in that mapping reads
``T_room + trv_sensor_rad_fraction·(T_rad − T_room)``: it sits on the
radiator, so it reads warm, but far below the radiator itself. The
inner controller sees that reading as its TRV temperature, as BT does.

The resulting ``valve_percent`` is what actually drives the plant's
actuator. From the benchmark's point of view the wrapper looks like any
other adapter; from BT's point of view the underlying controller is
unchanged. The differential between BT's "intent" and the TRV's actual
action surfaces the failure modes characteristic of Tado, Bosch,
Sonoff (offset-mode) and SEA80x-family offset-based TRVs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any

from .base import BenchmarkContext, BenchmarkOutput, ControllerAdapter, ControllerFamily


@dataclass(frozen=True)
class IndirectTrvParams:
    """Hardware-side characteristics of an offset-based TRV."""

    setpoint_step_K: float = 0.5
    internal_hysteresis_K: float = 0.3
    internal_p_gain: float = 30.0  # percent per Kelvin of internal error
    # Max upward bias BT can push the TRV setpoint above the room target —
    # the calibration headroom. 5 K is a realistic Better Thermostat
    # calibration range; above that the TRV's own thermometer protects.
    max_calibration_headroom_K: float = 5.0
    command_latency_steps: int = 0  # # of steps between BT command and TRV reaction
    # Mapping from the inner controller's ``u`` (valve_percent) to the
    # TRV setpoint. ``"production"`` (default) is the mapping Better
    # Thermostat ships, see the module docstring. ``"heuristic"`` issues
    # ``target + headroom·u/100``: a setpoint anchored at the user
    # target, far above T_room. ``"inversion"`` solves the TRV's own
    # P-loop backwards (``T_set = T_room + u/p_gain``). Both close the
    # TRV loop on the room sensor reading instead of on the TRV's own.
    setpoint_mapping: str = "production"
    # The TRV's setpoint range in °C; ``"production"`` scales ``u`` up to
    # the maximum and pushes the setpoint no lower than the minimum.
    min_setpoint: float = 5.0
    max_setpoint: float = 30.0
    # Share of the radiator's excess temperature the TRV's own sensor
    # picks up. 0 reads the room, 1 reads the radiator. Used by
    # ``"production"`` only.
    trv_sensor_rad_fraction: float = 0.1
    # Whether BT learns the opening the TRV chose. Better Thermostat reads
    # it from a ``valve_position`` attribute on the TRV's climate entity.
    # With it the inner controller gets the plant's valve as the previous
    # valve; without it, its own previous command.
    reports_valve_position: bool = False

    def __post_init__(self) -> None:
        """Reject invalid mapping and non-physical numeric fields.

        Raises
        ------
        ValueError
            If ``setpoint_mapping`` is not one of ``"production"``,
            ``"heuristic"`` or ``"inversion"``, if ``setpoint_step_K`` or
            ``internal_p_gain`` is not positive, if a
            hysteresis/headroom/latency field is negative, if the setpoint
            range is empty, or if ``trv_sensor_rad_fraction`` lies outside
            [0, 1].
        """
        if self.setpoint_mapping not in ("production", "heuristic", "inversion"):
            raise ValueError(
                "IndirectTrvParams setpoint_mapping must be 'production', "
                f"'heuristic' or 'inversion', got {self.setpoint_mapping!r}"
            )
        if self.min_setpoint >= self.max_setpoint:
            raise ValueError(
                "IndirectTrvParams min_setpoint must be below max_setpoint, "
                f"got {self.min_setpoint} and {self.max_setpoint}"
            )
        if not 0.0 <= self.trv_sensor_rad_fraction <= 1.0:
            raise ValueError(
                "IndirectTrvParams trv_sensor_rad_fraction must lie in [0, 1], "
                f"got {self.trv_sensor_rad_fraction}"
            )
        if self.setpoint_step_K <= 0.0 or self.internal_p_gain <= 0.0:
            raise ValueError(
                "IndirectTrvParams setpoint_step_K and internal_p_gain must be "
                f"> 0, got setpoint_step_K={self.setpoint_step_K}, "
                f"internal_p_gain={self.internal_p_gain}"
            )
        if (
            self.internal_hysteresis_K < 0.0
            or self.max_calibration_headroom_K < 0.0
            or self.command_latency_steps < 0
        ):
            raise ValueError(
                "IndirectTrvParams internal_hysteresis_K, "
                "max_calibration_headroom_K and command_latency_steps must be "
                ">= 0"
            )


# Vendor quirk presets. Values are heuristic — sourced from user-side
# observations of each TRV family's offset-mode behaviour, not from
# manufacturer documentation. Treat them as plausible operating points
# rather than calibrated truths. The setpoint maxima are typical upper
# ends of each family's range, equally unmeasured. All four leave
# ``reports_valve_position`` off.

TADO_PARAMS = IndirectTrvParams(
    setpoint_step_K=0.5,
    internal_hysteresis_K=0.3,
    internal_p_gain=30.0,
    max_calibration_headroom_K=5.0,
    command_latency_steps=0,
    max_setpoint=25.0,
)
"""Tado X / Tado Smart Radiator Thermostat.

0.5 K setpoint resolution, mild internal hysteresis, mid-range P-gain.
"""

BOSCH_PARAMS = IndirectTrvParams(
    setpoint_step_K=0.5,
    internal_hysteresis_K=0.5,  # tighter dead-zone before motor moves
    internal_p_gain=20.0,  # gentler internal regulation
    max_calibration_headroom_K=4.0,  # narrower override authority
    command_latency_steps=2,  # Bosch BTH-RA is slow to react
)
"""Bosch BTH-RA / Smart Radiator Thermostat II.

Larger hysteresis band, gentler P-gain, command latency of ~2 simulator
steps — reflecting reports of slow follow-through on direct commands.
"""

TUYA_PARAMS = IndirectTrvParams(
    setpoint_step_K=1.0,  # 1 K resolution kills fine tracking
    internal_hysteresis_K=0.5,
    internal_p_gain=15.0,  # softer regulation
    max_calibration_headroom_K=4.0,
    command_latency_steps=1,
    max_setpoint=35.0,
)
"""Tuya TS0601-derivative TRVs.

1 K setpoint quantisation is the dominant pathology — covers a long
tail of cheap re-branded Tuya TRVs.
"""

SONOFF_TRVZB_PARAMS = IndirectTrvParams(
    setpoint_step_K=0.5,
    internal_hysteresis_K=0.5,
    internal_p_gain=25.0,
    max_calibration_headroom_K=5.0,
    command_latency_steps=0,
    max_setpoint=35.0,
)
"""Sonoff TRVZB in offset-mode (post-FW 1.3 — pre-1.3 quirks live in the
direct-valve actuator profile with a 15-22 % deadband instead).
"""


class IndirectTrvAdapter:
    """Wrap an inner adapter behind a TRV-internal P-loop with quantisation."""

    family: ControllerFamily = "valve"

    def __init__(self, inner: ControllerAdapter, params: IndirectTrvParams) -> None:
        self.inner = inner
        self.params = params
        self.name = f"{inner.name}+indirect_trv"
        self._last_quantised_setpoint_C: float | None = None
        self._pending_setpoints: list[float] = []
        self._last_inner_valve_pct = 0.0

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Reset wrapped controller and the TRV layer.

        ``prior`` takes the shape produced by :meth:`export_state`: the
        inner controller's snapshot under ``"inner"``, the TRV-layer cache
        at the top level. Missing keys fall back to a cleared state.
        """
        inner_prior = prior.get("inner") if prior is not None else None
        self.inner.reset(inner_prior if isinstance(inner_prior, dict) else None)
        last = prior.get("last_quantised_setpoint_C") if prior is not None else None
        self._last_quantised_setpoint_C = (
            float(last) if isinstance(last, (int, float)) else None
        )
        pending = prior.get("pending_setpoints") if prior is not None else None
        self._pending_setpoints = list(pending) if isinstance(pending, list) else []
        last_inner = prior.get("last_inner_valve_pct") if prior is not None else None
        self._last_inner_valve_pct = (
            float(last_inner) if isinstance(last_inner, (int, float)) else 0.0
        )

    def _trv_reading(self, ctx: BenchmarkContext) -> float:
        """Return the temperature the TRV's own sensor reports."""
        room = ctx.raw_room_temp_C
        if ctx.trv_temp_C is None:
            return room
        return room + self.params.trv_sensor_rad_fraction * (ctx.trv_temp_C - room)

    def _production_setpoint(
        self, ctx: BenchmarkContext, trv_reading: float, bt_valve_pct: float
    ) -> float:
        """Map ``u`` onto a TRV setpoint the way ``calibration.py`` does."""
        p = self.params
        fraction = max(0.0, min(1.0, bt_valve_pct / 100.0))
        setpoint = trv_reading + (p.max_setpoint - trv_reading) * fraction
        if fraction == 0.0 and setpoint >= trv_reading:
            # ``_compute_zero_open_offset``: push the setpoint below the
            # TRV's reading, further the more the room overshoots.
            overshoot = max(0.0, ctx.current_temp_C - ctx.target_temp_C)
            max_offset = max(1.0, trv_reading - p.min_setpoint)
            offset = max(
                p.setpoint_step_K, max_offset * (1.0 - math.exp(-0.5 * overshoot))
            )
            setpoint = trv_reading - offset
        # Round up while BT asks for heat, down while it does not, so the
        # step grid never turns a closing command into an opening one.
        steps = setpoint / p.setpoint_step_K
        steps = math.ceil(steps - 1e-9) if fraction > 0.0 else math.floor(steps + 1e-9)
        setpoint = steps * p.setpoint_step_K
        return max(p.min_setpoint, min(p.max_setpoint, setpoint))

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Translate the inner controller's valve_percent into TRV-controlled u."""
        production = self.params.setpoint_mapping == "production"
        trv_reading = self._trv_reading(ctx)
        inner_ctx = ctx
        if production:
            inner_ctx = replace(
                ctx,
                trv_temp_C=trv_reading,
                last_valve_percent=(
                    ctx.last_valve_percent
                    if self.params.reports_valve_position
                    else self._last_inner_valve_pct
                ),
            )
        inner_out = self.inner.step(inner_ctx)
        if inner_out.valve_percent is None:
            raise ValueError(
                f"{self.name}: inner adapter {self.inner.name} produced no "
                "valve_percent; IndirectTrvAdapter only wraps valve-family controllers"
            )
        bt_valve_pct = inner_out.valve_percent
        self._last_inner_valve_pct = bt_valve_pct

        # Map BT's "heat intent" (0-100 % valve) onto a TRV setpoint.
        #
        # "production" (default): the mapping in ``calibration.py``, see
        # ``_production_setpoint``.
        #
        # "inversion": invert the TRV's own P-loop exactly. The TRV
        # computes ``u_trv = p_gain · (T_set − T_room)``; to request a
        # ``u_trv == bt_valve_pct`` we set ``T_set = T_room +
        # bt_valve_pct / p_gain``. Physically well-founded but degrades
        # under heavy setpoint quantisation.
        #
        # "heuristic": scale a fixed headroom band against the
        # user target, ignoring T_room entirely. Less principled but
        # tracks better on quantised TRVs (0.5 K / 1 K setpoint steps).
        if production:
            desired_setpoint = self._production_setpoint(ctx, trv_reading, bt_valve_pct)
        elif self.params.setpoint_mapping == "heuristic":
            headroom_K = self.params.max_calibration_headroom_K
            desired_setpoint = ctx.target_temp_C + headroom_K * (bt_valve_pct / 100.0)
        else:
            p_gain = max(self.params.internal_p_gain, 1e-6)
            desired_setpoint = ctx.current_temp_C + bt_valve_pct / p_gain

        # Quantise to TRV's setpoint resolution.
        step = max(self.params.setpoint_step_K, 1e-6)
        quantised = round(desired_setpoint / step) * step

        # Hysteresis band on the *quantised* setpoint — TRV ignores micro-
        # changes inside the band.
        if self._last_quantised_setpoint_C is None:
            applied_setpoint = quantised
        elif (
            abs(quantised - self._last_quantised_setpoint_C)
            < self.params.internal_hysteresis_K
        ):
            applied_setpoint = self._last_quantised_setpoint_C
        else:
            applied_setpoint = quantised
        self._last_quantised_setpoint_C = applied_setpoint

        # Optional FIFO latency for the command.
        if self.params.command_latency_steps > 0:
            self._pending_setpoints.append(applied_setpoint)
            while len(self._pending_setpoints) > self.params.command_latency_steps + 1:
                self._pending_setpoints.pop(0)
            applied_setpoint = self._pending_setpoints[0]

        # TRV-internal P-loop against the temperature the TRV reports: its
        # own radiator-warmed reading under ``"production"``, the room
        # sensor reading under the other two mappings.
        error_K = applied_setpoint - (trv_reading if production else ctx.current_temp_C)
        u_pct = max(0.0, min(100.0, self.params.internal_p_gain * error_K))

        return BenchmarkOutput(
            valve_percent=u_pct,
            diagnostics={
                **inner_out.diagnostics,
                "indirect_setpoint_C": applied_setpoint,
                "indirect_quantised_diff_K": applied_setpoint - desired_setpoint,
            },
        )

    def export_state(self) -> dict[str, Any]:
        """Expose inner state plus TRV-layer cache."""
        return {
            "inner": self.inner.export_state(),
            "last_quantised_setpoint_C": self._last_quantised_setpoint_C,
            "pending_setpoints": list(self._pending_setpoints),
            "last_inner_valve_pct": self._last_inner_valve_pct,
        }
