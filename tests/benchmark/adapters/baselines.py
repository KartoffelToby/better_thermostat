"""Baseline controllers for benchmark context.

These are not production controllers. They exist so that the production
controllers can be compared against trivial reference behaviour:

* :class:`BangBangAdapter` — fixed hysteresis band, 0 % or 100 % output.
* :class:`LinearPAdapter` — simple proportional controller, no integral
  action.
* :class:`IdealOracleAdapter` — knows the plant's true steady-state
  inverse and commands the valve setting needed to hold the setpoint
  asymptotically. Used as an upper bound for what's physically possible
  given the simulator.

Comparing real controllers to these establishes a sanity floor and ceiling
for each scenario's metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import BenchmarkContext, BenchmarkOutput, ControllerFamily


@dataclass
class BangBangParams:
    """Hysteresis band for the bang-bang controller."""

    band_K: float = 0.2  # +/- around setpoint
    on_pct: float = 100.0
    off_pct: float = 0.0


class BangBangAdapter:
    """Naive on/off controller with a single hysteresis band."""

    name: str = "bangbang"
    family: ControllerFamily = "valve"

    def __init__(self, params: BangBangParams | None = None) -> None:
        self._params = params if params is not None else BangBangParams()
        self._state_on: bool = False

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Reset to the OFF state."""
        _ = prior
        self._state_on = False

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Toggle between on/off based on the hysteresis band."""
        sp = ctx.target_temp_C
        cur = ctx.current_temp_C
        p = self._params
        if cur < sp - p.band_K:
            self._state_on = True
        elif cur > sp + p.band_K:
            self._state_on = False
        # Inside the band — keep previous state (the hysteresis).
        return BenchmarkOutput(
            valve_percent=p.on_pct if self._state_on else p.off_pct,
            diagnostics={"state_on": self._state_on},
        )

    def export_state(self) -> dict[str, Any]:
        """Return a snapshot of the on/off state."""
        return {"state_on": self._state_on}


@dataclass
class LinearPParams:
    """Gain and saturation for the proportional controller."""

    kp: float = 50.0  # percent per K of error
    clamp_min_pct: float = 0.0
    clamp_max_pct: float = 100.0


class LinearPAdapter:
    """Single-gain proportional controller (no integral, no derivative)."""

    name: str = "linear_p"
    family: ControllerFamily = "valve"

    def __init__(self, params: LinearPParams | None = None) -> None:
        self._params = params if params is not None else LinearPParams()

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Reset has nothing to clear for a pure-P controller."""
        _ = prior

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Output ``kp * (setpoint - measured)`` clamped to the saturation band."""
        p = self._params
        error_K = ctx.target_temp_C - ctx.current_temp_C
        raw = p.kp * error_K
        clamped = max(p.clamp_min_pct, min(p.clamp_max_pct, raw))
        return BenchmarkOutput(
            valve_percent=clamped,
            diagnostics={"error_K": round(error_K, 3), "raw_pct": round(raw, 2)},
        )

    def export_state(self) -> dict[str, Any]:
        """Return an empty state (no learning)."""
        return {}


class IdealOracleAdapter:
    """Oracle controller with steady-state plant inversion and aggressive feedback.

    Knows the plant's parameters (``gain_heater``, ``T_water_C`` etc.) and
    computes the steady-state valve percent that would hold the current
    setpoint asymptotically. Adds a strong proportional feedback term
    that compensates for transients and small modelling errors.

    The oracle is *not* a real controller — it has direct access to the
    plant parameters and would not exist outside simulation. It serves
    two purposes in the benchmark:

    * **Upper bound** for tracking quality — what a feedback controller
      with perfect knowledge can achieve.
    * **Stabilisation driver** in :func:`runner.run_scenario` — warms the
      plant to equilibrium before the test controller takes over.
    """

    name: str = "ideal_oracle"
    family: ControllerFamily = "valve"

    def __init__(
        self,
        plant_params: Any | None = None,
        feedback_gain_per_K: float = 50.0,
        feedback_clamp_pct: float = 50.0,
    ) -> None:
        # Defer the plant import to runtime to keep adapters/ free of cycles.
        if plant_params is None:
            from tests.benchmark.plant import PROFILE_STANDARD

            plant_params = PROFILE_STANDARD
        self._plant = plant_params
        self._feedback_gain = feedback_gain_per_K
        self._feedback_clamp = feedback_clamp_pct

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """No state to reset."""
        _ = prior

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Return steady-state valve percent plus aggressive feedback correction."""
        p = self._plant
        sp = ctx.target_temp_C
        T_out = ctx.outdoor_temp_C

        # Steady-state inversion of the two-state plant:
        #   T_rad_ss = 2 * sp - T_out   (from room balance)
        #   gain * u * (T_water - T_rad_ss) = T_rad_ss - sp   (from rad balance)
        #   => u_ss = (T_rad_ss - sp) / (gain * (T_water - T_rad_ss))
        T_rad_ss = 2.0 * sp - T_out
        denom = p.gain_heater * (p.T_water_C - T_rad_ss)
        if denom <= 0.0:
            u_ff_pct = 100.0  # cannot reach setpoint with this water temp
        else:
            u_ff_pct = max(0.0, min(100.0, 100.0 * (T_rad_ss - sp) / denom))

        # Aggressive P-feedback so the oracle reacts to transients quickly.
        # Feed back on the plant truth: the oracle is the perfect-knowledge
        # upper bound, so sensor lag/noise must not depress its ceiling.
        error_K = sp - ctx.raw_room_temp_C
        u_fb_pct = max(
            -self._feedback_clamp,
            min(self._feedback_clamp, error_K * self._feedback_gain),
        )

        valve = max(0.0, min(100.0, u_ff_pct + u_fb_pct))
        return BenchmarkOutput(
            valve_percent=valve,
            diagnostics={
                "u_ff_pct": round(u_ff_pct, 2),
                "u_fb_pct": round(u_fb_pct, 2),
                "error_K": round(error_K, 3),
            },
        )

    def export_state(self) -> dict[str, Any]:
        """Return an empty state (stateless)."""
        return {}
