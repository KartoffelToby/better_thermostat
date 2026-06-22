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

1. A *desired TRV setpoint* (high when BT wants heat, equal to room when
   not),
2. Quantised to ``setpoint_step_K``,
3. Optionally held by a hysteresis band before it changes,
4. Driven through a small TRV-internal P-loop using the TRV's own
   reported temperature.

The resulting ``valve_percent`` is what actually drives the plant's
actuator. From the benchmark's point of view the wrapper looks like any
other adapter; from BT's point of view the underlying controller is
unchanged. The differential between BT's "intent" and the TRV's actual
action surfaces the failure modes characteristic of Tado, Bosch,
Sonoff (offset-mode) and SEA80x-family offset-based TRVs.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    # TRV setpoint. ``"heuristic"`` (default) issues
    # ``target + headroom·u/100``: a setpoint anchored at the user
    # target, far above T_room. Tracks well on quantised TRVs because
    # small u-changes still land on a setpoint above the room.
    # ``"inversion"`` solves the TRV's own P-loop backwards
    # (``T_set = T_room + u/p_gain``) — physically cleaner under sensor
    # bias but degrades on quantised setpoints. The cleaner long-term
    # fix is a cascade-control plant model that exposes the TRV's
    # internal regulator as a first-class layer rather than a bolt-on.
    setpoint_mapping: str = "heuristic"

    def __post_init__(self) -> None:
        """Reject invalid mapping and non-physical numeric fields.

        Raises
        ------
        ValueError
            If ``setpoint_mapping`` is neither ``"heuristic"`` nor
            ``"inversion"``, if ``setpoint_step_K`` or ``internal_p_gain``
            is not positive, or if a hysteresis/headroom/latency field is
            negative.
        """
        if self.setpoint_mapping not in ("heuristic", "inversion"):
            raise ValueError(
                "IndirectTrvParams setpoint_mapping must be 'heuristic' or "
                f"'inversion', got {self.setpoint_mapping!r}"
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
# rather than calibrated truths.

TADO_PARAMS = IndirectTrvParams(
    setpoint_step_K=0.5,
    internal_hysteresis_K=0.3,
    internal_p_gain=30.0,
    max_calibration_headroom_K=5.0,
    command_latency_steps=0,
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

Larger hysteresis band, gentler P-gain, command latency of ~2 MPC
steps — reflecting reports of slow follow-through on direct commands.
"""

TUYA_PARAMS = IndirectTrvParams(
    setpoint_step_K=1.0,  # 1 K resolution kills fine tracking
    internal_hysteresis_K=0.5,
    internal_p_gain=15.0,  # softer regulation
    max_calibration_headroom_K=4.0,
    command_latency_steps=1,
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

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Reset wrapped controller and the TRV layer.

        ``prior`` takes the shape produced by :meth:`export_state`: the
        inner controller's snapshot under ``"inner"``, the TRV-layer cache
        at the top level. Missing keys fall back to a cleared state.
        """
        inner_prior = prior.get("inner") if prior is not None else None
        self.inner.reset(inner_prior if isinstance(inner_prior, dict) else None)
        last = prior.get("last_quantised_setpoint_C") if prior is not None else None
        self._last_quantised_setpoint_C = last if isinstance(last, float) else None
        pending = prior.get("pending_setpoints") if prior is not None else None
        self._pending_setpoints = list(pending) if isinstance(pending, list) else []

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Translate the inner controller's valve_percent into TRV-controlled u."""
        inner_out = self.inner.step(ctx)
        if inner_out.valve_percent is None:
            raise ValueError(
                f"{self.name}: inner adapter {self.inner.name} produced no "
                "valve_percent; IndirectTrvAdapter only wraps valve-family controllers"
            )
        bt_valve_pct = inner_out.valve_percent

        # Map BT's "heat intent" (0-100 % valve) onto a TRV setpoint.
        #
        # "inversion": invert the TRV's own P-loop exactly. The TRV
        # computes ``u_trv = p_gain · (T_set − T_room)``; to request a
        # ``u_trv == bt_valve_pct`` we set ``T_set = T_room +
        # bt_valve_pct / p_gain``. Physically well-founded but degrades
        # under heavy setpoint quantisation.
        #
        # "heuristic" (default): scale a fixed headroom band against the
        # user target, ignoring T_room entirely. Less principled but
        # tracks better on quantised TRVs (0.5 K / 1 K setpoint steps).
        if self.params.setpoint_mapping == "heuristic":
            headroom_K = self.params.max_calibration_headroom_K
            desired_setpoint_C = ctx.target_temp_C + headroom_K * (bt_valve_pct / 100.0)
        else:
            p_gain = max(self.params.internal_p_gain, 1e-6)
            desired_setpoint_C = ctx.current_temp_C + bt_valve_pct / p_gain

        # Quantise to TRV's setpoint resolution.
        step = max(self.params.setpoint_step_K, 1e-6)
        quantised = round(desired_setpoint_C / step) * step

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
                applied_setpoint = self._pending_setpoints.pop(0)
            applied_setpoint = self._pending_setpoints[0]

        # TRV-internal P-loop against the room temperature the TRV itself
        # reports. We use ``current_temp_C`` (sensor reading) — real TRVs
        # measure the room near their body, not the radiator surface they
        # sit on, so the room temperature is the right proxy here.
        error_K = applied_setpoint - ctx.current_temp_C
        u_pct = max(0.0, min(100.0, self.params.internal_p_gain * error_K))

        return BenchmarkOutput(
            valve_percent=u_pct,
            diagnostics={
                **inner_out.diagnostics,
                "indirect_setpoint_C": applied_setpoint,
                "indirect_quantised_diff_K": applied_setpoint - desired_setpoint_C,
            },
        )

    def export_state(self) -> dict[str, Any]:
        """Expose inner state plus TRV-layer cache."""
        return {
            "inner": self.inner.export_state(),
            "last_quantised_setpoint_C": self._last_quantised_setpoint_C,
            "pending_setpoints": list(self._pending_setpoints),
        }
