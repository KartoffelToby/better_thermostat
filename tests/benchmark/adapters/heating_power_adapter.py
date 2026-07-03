"""Adapter for Better Thermostat's ``HEATING_POWER_CALIBRATION`` mode.

The production mode is labelled *"Time Based"* in the UI. It combines two
pieces:

* A ``heating_power`` estimate (°C/min) learned from completed heating
  cycles via EMA — see ``utils/thermal_learning.py``.
* A heuristic valve-position formula ``valve = 0.019 · (Δt/hp)^0.946``
  with minimum-opening floors — see ``utils/helpers.py``.

This adapter reimplements the cycle state machine in pure Python (no HA
``HVACAction`` dependency) so the benchmark can exercise it the same way
it exercises ``mpc`` / ``pid`` / ``tpi``. The valve-position formula is
re-implemented headlessly with its floor constants imported from the
production source; ``test_production_drift.py`` pins the full formula
against the production implementation. The EMA learner's adaptive alpha
(weight/env factors, clamps, rounding) calls the production helpers from
``utils/thermal_learning.py`` directly.
"""

from __future__ import annotations

from typing import Any

from custom_components.better_thermostat.utils.const import (
    MAX_HEATING_POWER,
    MIN_HEATING_POWER,
    VALVE_MIN_BASE,
    VALVE_MIN_OPENING_LARGE_DIFF,
    VALVE_MIN_PROPORTIONAL_SLOPE,
    VALVE_MIN_SMALL_DIFF_THRESHOLD,
    VALVE_MIN_THRESHOLD_TEMP_DIFF,
)
from custom_components.better_thermostat.utils.thermal_learning import (
    _ALPHA_MAX,
    _ALPHA_MIN,
    _BASE_ALPHA,
    _MIN_CYCLE_DURATION,
    clamp,
    compute_env_factor,
    compute_weight_factor,
    ema_smooth,
)

from .base import BenchmarkContext, BenchmarkOutput, ControllerFamily

# Benchmark-only plateau timeout: production finalizes a cycle when the
# room cools below its peak; the timeout bounds pathological plateaus.
_FINALIZE_TIMEOUT_MIN: float = 30.0

# Default observed target-temperature range, mirroring
# ``HeatingPowerTracker`` in utils/thermal_learning.py. Observed targets
# widen the range; the weight factor positions the current target in it.
_DEFAULT_MIN_TARGET: float = 18.0
_DEFAULT_MAX_TARGET: float = 21.0


class HeatingPowerAdapter:
    """Benchmark wrapper for the ``HEATING_POWER_CALIBRATION`` ("Time Based") mode.

    The default ``initial_heating_power = 0.02 °C/min`` represents a
    mid-range learned value — what a well-fitted production install would
    converge on after a few days. Override to ``0.01`` for the
    "fresh-install" cold-start experience.
    """

    name: str = "heating_power"
    family: ControllerFamily = "valve"

    def __init__(self, initial_heating_power: float = 0.02) -> None:
        self._initial_heating_power = max(
            MIN_HEATING_POWER, min(MAX_HEATING_POWER, initial_heating_power)
        )
        self.heating_power: float = self._initial_heating_power
        self._cycle_start_temp: float | None = None
        self._cycle_start_t: float | None = None
        self._cycle_peak_temp: float | None = None
        self._cycle_peak_t: float | None = None
        self._was_heating: bool = False
        self._min_target: float = _DEFAULT_MIN_TARGET
        self._max_target: float = _DEFAULT_MAX_TARGET

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Drop all learned state; optionally seed ``heating_power`` from ``prior``."""
        seed = self._initial_heating_power
        if prior is not None:
            seeded = prior.get("heating_power")
            if isinstance(seeded, (int, float)):
                seed = max(MIN_HEATING_POWER, min(MAX_HEATING_POWER, float(seeded)))
        self.heating_power = seed
        self._cycle_start_temp = None
        self._cycle_start_t = None
        self._cycle_peak_temp = None
        self._cycle_peak_t = None
        self._was_heating = False
        self._min_target = _DEFAULT_MIN_TARGET
        self._max_target = _DEFAULT_MAX_TARGET

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute valve percent + update the heating-power learner."""
        valve_pct = self._compute_valve_pct(ctx)
        self._update_learner(ctx, valve_pct > 0.0)
        return BenchmarkOutput(
            valve_percent=valve_pct,
            diagnostics={
                "heating_power": self.heating_power,
                "cycle_active": self._cycle_start_t is not None,
            },
        )

    def export_state(self) -> dict[str, Any]:
        """Return the learned heating-power and current cycle state."""
        return {
            "heating_power": self.heating_power,
            "cycle_start_temp": self._cycle_start_temp,
            "cycle_peak_temp": self._cycle_peak_temp,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_valve_pct(self, ctx: BenchmarkContext) -> float:
        """Heating-power → valve-position formula, mirroring ``utils/helpers.py``."""
        temp_diff = ctx.target_temp_C - ctx.current_temp_C
        if temp_diff <= 0.0:
            return 0.0
        hp = max(MIN_HEATING_POWER, min(MAX_HEATING_POWER, self.heating_power))
        valve_pos = 0.019 * (temp_diff / hp) ** 0.946
        if temp_diff > VALVE_MIN_THRESHOLD_TEMP_DIFF:
            valve_pos = max(VALVE_MIN_OPENING_LARGE_DIFF, valve_pos)
        elif temp_diff >= VALVE_MIN_SMALL_DIFF_THRESHOLD:
            min_v = (
                VALVE_MIN_BASE
                + (temp_diff - VALVE_MIN_SMALL_DIFF_THRESHOLD)
                * VALVE_MIN_PROPORTIONAL_SLOPE
            )
            valve_pos = max(min_v, valve_pos)
        return max(0.0, min(1.0, valve_pos)) * 100.0

    def _update_learner(self, ctx: BenchmarkContext, is_heating: bool) -> None:
        """Track heating cycles and EMA-update ``heating_power`` on finalize.

        Cycle = valve-active phase. Peak = warmest point after valve closes
        (radiator residual heat keeps room rising briefly). Finalize once
        the room cools back below peak (or after a 30-min plateau timeout),
        then EMA the observed K/min into ``heating_power``.
        """
        if is_heating and not self._was_heating:
            # Demand can resume before the room cools below the tracked
            # peak; finalize the finished cycle instead of dropping it.
            if (
                self._cycle_start_temp is not None
                and self._cycle_start_t is not None
                and self._cycle_peak_temp is not None
                and self._cycle_peak_t is not None
            ):
                self._finalize_cycle(ctx)
            self._cycle_start_temp = ctx.current_temp_C
            self._cycle_start_t = ctx.t
            self._cycle_peak_temp = None
            self._cycle_peak_t = None
        elif not is_heating and self._was_heating:
            self._cycle_peak_temp = ctx.current_temp_C
            self._cycle_peak_t = ctx.t
        elif (
            not is_heating
            and self._cycle_peak_temp is not None
            and ctx.current_temp_C > self._cycle_peak_temp
        ):
            self._cycle_peak_temp = ctx.current_temp_C
            self._cycle_peak_t = ctx.t

        if (
            self._cycle_start_temp is not None
            and self._cycle_start_t is not None
            and self._cycle_peak_temp is not None
            and self._cycle_peak_t is not None
        ):
            elapsed_since_peak_min = (ctx.t - self._cycle_peak_t) / 60.0
            if (
                ctx.current_temp_C < self._cycle_peak_temp
                or elapsed_since_peak_min >= _FINALIZE_TIMEOUT_MIN
            ):
                self._finalize_cycle(ctx)

        # Widen the observed target range after the finalize check, in the
        # same order as ``HeatingPowerTracker.update``.
        self._min_target = min(self._min_target, ctx.target_temp_C)
        self._max_target = max(self._max_target, ctx.target_temp_C)

        self._was_heating = is_heating

    def _finalize_cycle(self, ctx: BenchmarkContext) -> None:
        """Update the EMA from the just-closed cycle and reset cycle state.

        The adaptive smoothing factor is the production formula:
        ``alpha = clamp(base * weight * env, min, max)`` with the weight
        and environment factors computed by ``utils/thermal_learning.py``.
        """
        assert self._cycle_start_temp is not None
        assert self._cycle_start_t is not None
        assert self._cycle_peak_temp is not None
        assert self._cycle_peak_t is not None
        temp_diff_K = self._cycle_peak_temp - self._cycle_start_temp
        duration_min = (self._cycle_peak_t - self._cycle_start_t) / 60.0
        if duration_min >= _MIN_CYCLE_DURATION and temp_diff_K > 0.0:
            heating_rate = round(temp_diff_K / duration_min, 4)
            weight_factor = compute_weight_factor(
                ctx.target_temp_C, self._min_target, self._max_target
            )
            env_factor = compute_env_factor(ctx.outdoor_temp_C, ctx.target_temp_C)
            alpha = clamp(
                _BASE_ALPHA * weight_factor * env_factor, _ALPHA_MIN, _ALPHA_MAX
            )
            updated = clamp(
                ema_smooth(self.heating_power, heating_rate, alpha),
                MIN_HEATING_POWER,
                MAX_HEATING_POWER,
            )
            self.heating_power = round(updated, 4)
        self._cycle_start_temp = None
        self._cycle_start_t = None
        self._cycle_peak_temp = None
        self._cycle_peak_t = None
