"""Thermal learning: heating-power and heat-loss state machines.

Typed, pure-Python tracker dataclasses that are free of Home Assistant imports
at runtime.  All HA-specific side-effects are communicated back to the caller
via frozen *Result* dataclasses.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.components.climate.const import HVACAction

from .const import MAX_HEAT_LOSS, MAX_HEATING_POWER, MIN_HEAT_LOSS, MIN_HEATING_POWER

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------
_TIMEOUT_MIN: int = 30  # plateau safety timeout (minutes)
_MIN_CYCLE_DURATION: float = 1.0  # minimum cycle minutes to accept
_BASE_ALPHA: float = 0.10  # EMA base smoothing factor
_ALPHA_MIN: float = 0.02
_ALPHA_MAX: float = 0.25

# Telemetry deque sizes
_STATS_MAXLEN: int = 10
_CYCLES_MAXLEN: int = 50


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def ema_smooth(old: float, new: float, alpha: float) -> float:
    """Exponential moving average: ``old * (1 - alpha) + new * alpha``."""
    return old * (1.0 - alpha) + new * alpha


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* into ``[lo, hi]``."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def compute_weight_factor(
    target_temp: float | None,
    min_target: float,
    max_target: float,
) -> float:
    """Relative-position weight within the observed target-temp range.

    Returns a factor in ``[0.5, 1.5]``.
    """
    temp_range = max(max_target - min_target, 0.1)
    if target_temp is None:
        relative_pos = 0.5
    else:
        relative_pos = (target_temp - min_target) / temp_range
    return clamp(0.5 + relative_pos, 0.5, 1.5)


def compute_env_factor(
    outdoor_temp: float | None,
    target_temp: float | None,
) -> float:
    """Environmental factor based on outdoor-to-setpoint gradient.

    Returns a factor in ``[0.7, 1.3]``.  Without outdoor data returns 1.0.
    """
    if outdoor_temp is None or target_temp is None:
        return 1.0
    delta_env = max(target_temp - outdoor_temp, 0.1)
    return clamp(delta_env / 20.0, 0.7, 1.3)


# ---------------------------------------------------------------------------
# Result dataclasses (frozen – no mutation after creation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CycleResult:
    """Result of a finalized heating or cooling cycle."""

    power_changed: bool = False
    loss_changed: bool = False


@dataclass(frozen=True, slots=True)
class HeatingPowerUpdate:
    """Return value of :meth:`HeatingPowerTracker.update`."""

    action_changed: bool = False
    current_action: object = None  # HVACAction at runtime
    cycle_result: CycleResult | None = None


@dataclass(frozen=True, slots=True)
class HeatLossUpdate:
    """Return value of :meth:`HeatLossTracker.update`."""

    cycle_result: CycleResult | None = None


# ---------------------------------------------------------------------------
# HeatingPowerTracker
# ---------------------------------------------------------------------------

@dataclass
class HeatingPowerTracker:
    """State machine that learns effective heating power (°C / min).

    Call :meth:`update` on every temperature event.  The caller is responsible
    for acting on the returned :class:`HeatingPowerUpdate`.
    """

    heating_power: float = 0.01
    normalized_power: float | None = None
    start_temp: float | None = None
    start_ts: datetime | None = None
    end_temp: float | None = None  # peak temperature after heating stops
    end_ts: datetime | None = None
    _prev_action: object = None  # HVACAction – kept as object to avoid runtime import
    min_target: float = 18.0
    max_target: float = 21.0
    stats: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=_STATS_MAXLEN)
    )
    cycles: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=_CYCLES_MAXLEN)
    )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        cur_temp: float,
        current_action: HVACAction,
        now: datetime,
        *,
        target_temp: float | None = None,
        outdoor_temp: float | None = None,
    ) -> HeatingPowerUpdate:
        """Process one temperature reading and return what changed."""
        from homeassistant.components.climate.const import HVACAction as _HA

        action_changed = current_action != self._prev_action

        # --- Transition: heating starts ---
        if current_action == _HA.HEATING and self._prev_action != _HA.HEATING:
            self.start_temp = cur_temp
            self.start_ts = now
            self.end_temp = None
            self.end_ts = None

        # --- Transition: heating stops (candidate end) ---
        elif (
            current_action != _HA.HEATING
            and self._prev_action == _HA.HEATING
            and self.start_temp is not None
            and self.end_temp is None
        ):
            self.end_temp = cur_temp
            self.end_ts = now

        # --- Peak tracking: temp still rising after heating stopped ---
        elif (
            current_action != _HA.HEATING
            and self.start_temp is not None
            and self.end_temp is not None
            and cur_temp > self.end_temp
        ):
            self.end_temp = cur_temp
            self.end_ts = now

        # --- Finalization criteria ---
        cycle_result = self._maybe_finalize(
            cur_temp, now, target_temp=target_temp, outdoor_temp=outdoor_temp
        )

        # --- Dynamic target range ---
        if target_temp is not None:
            self.min_target = min(self.min_target, target_temp)
            self.max_target = max(self.max_target, target_temp)

        self._prev_action = current_action

        return HeatingPowerUpdate(
            action_changed=action_changed,
            current_action=current_action,
            cycle_result=cycle_result,
        )

    def reset_power(self, value: float = 0.01) -> None:
        """Reset heating power to the given default."""
        self.heating_power = value

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _maybe_finalize(
        self,
        cur_temp: float,
        now: datetime,
        *,
        target_temp: float | None,
        outdoor_temp: float | None,
    ) -> CycleResult | None:
        """Check finalization criteria and compute a new EMA value if met."""
        finalize = False

        if (
            self.start_temp is not None
            and self.end_temp is not None
            and cur_temp < self.end_temp
        ):
            finalize = True
        elif self.end_ts is not None and (now - self.end_ts) > timedelta(
            minutes=_TIMEOUT_MIN
        ):
            finalize = True

        if not finalize:
            return None

        power_changed = False

        if self.end_temp is not None and self.start_temp is not None:
            temp_diff = self.end_temp - self.start_temp
        else:
            temp_diff = 0.0

        if self.end_ts is not None and self.start_ts is not None:
            duration_min = (self.end_ts - self.start_ts).total_seconds() / 60.0
        else:
            duration_min = 0.0

        if duration_min >= _MIN_CYCLE_DURATION and temp_diff > 0:
            weight_factor = compute_weight_factor(
                target_temp, self.min_target, self.max_target
            )
            env_factor = compute_env_factor(outdoor_temp, target_temp)

            normalized_power: float | None = None
            if outdoor_temp is not None and target_temp is not None:
                delta_env = max(target_temp - outdoor_temp, 0.1)
                normalized_power = round((temp_diff / duration_min) / delta_env, 5)

            heating_rate = round(temp_diff / duration_min, 4)

            alpha = clamp(_BASE_ALPHA * weight_factor * env_factor, _ALPHA_MIN, _ALPHA_MAX)

            old_power = self.heating_power
            unbounded = ema_smooth(old_power, heating_rate, alpha)
            new_power = clamp(unbounded, MIN_HEATING_POWER, MAX_HEATING_POWER)

            if new_power != unbounded:
                bound_name = (
                    "MIN_HEATING_POWER"
                    if new_power <= MIN_HEATING_POWER
                    else "MAX_HEATING_POWER"
                )
                _LOGGER.debug(
                    "better_thermostat: heating_power clamped from %.4f to %.4f at %s "
                    "(min=%.4f, max=%.4f)",
                    unbounded,
                    new_power,
                    bound_name,
                    MIN_HEATING_POWER,
                    MAX_HEATING_POWER,
                )

            self.heating_power = round(new_power, 4)
            self.normalized_power = normalized_power
            power_changed = self.heating_power != old_power

            # Short stats history
            self.stats.append(
                {
                    "dT": round(temp_diff, 2),
                    "min": round(duration_min, 1),
                    "rate": heating_rate,
                    "alpha": round(alpha, 3),
                    "envf": round(env_factor, 3),
                    "hp": self.heating_power,
                    "norm": normalized_power,
                }
            )

            # Full cycle telemetry
            self.cycles.append(
                {
                    "start": self.start_ts.isoformat() if self.start_ts else None,
                    "end": self.end_ts.isoformat() if self.end_ts else None,
                    "temp_start": (
                        round(self.start_temp, 2) if self.start_temp is not None else None
                    ),
                    "temp_peak": (
                        round(self.end_temp, 2) if self.end_temp is not None else None
                    ),
                    "delta_t": round(temp_diff, 3),
                    "minutes": round(duration_min, 2),
                    "rate_c_min": heating_rate,
                    "target": target_temp,
                    "outdoor": outdoor_temp,
                    "norm_power": normalized_power,
                }
            )

            _LOGGER.debug(
                "better_thermostat: heating cycle evaluated: ΔT=%.3f°C, t=%.2fmin, "
                "rate=%.4f°C/min, hp(old/new)=%.4f/%.4f, alpha=%.3f, env_factor=%.3f, norm=%s",
                temp_diff,
                duration_min,
                heating_rate,
                old_power,
                self.heating_power,
                alpha,
                env_factor,
                normalized_power,
            )

        # Reset for next cycle (even if discarded)
        self.start_temp = None
        self.end_temp = None
        self.start_ts = None
        self.end_ts = None

        return CycleResult(power_changed=power_changed)


# ---------------------------------------------------------------------------
# HeatLossTracker
# ---------------------------------------------------------------------------

@dataclass
class HeatLossTracker:
    """State machine that learns heat-loss rate (°C / min) during idle periods.

    Call :meth:`update` on every temperature event.  The caller is responsible
    for acting on the returned :class:`HeatLossUpdate`.
    """

    heat_loss_rate: float = 0.01
    start_temp: float | None = None
    start_ts: datetime | None = None
    end_temp: float | None = None  # lowest observed temperature
    end_ts: datetime | None = None
    _prev_action: object = None  # HVACAction
    stats: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=_STATS_MAXLEN)
    )
    cycles: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=_CYCLES_MAXLEN)
    )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        cur_temp: float,
        current_action: HVACAction,
        now: datetime,
        *,
        window_open: bool = False,
    ) -> HeatLossUpdate:
        """Process one temperature reading and return what changed."""
        from homeassistant.components.climate.const import HVACAction as _HA

        # Window open → reset tracking
        if window_open:
            self.start_temp = None
            self.start_ts = None
            self.end_temp = None
            self.end_ts = None
            self._prev_action = current_action
            return HeatLossUpdate()

        # Track idle cooling
        if current_action != _HA.HEATING:
            if self.start_temp is None:
                self.start_temp = cur_temp
                self.start_ts = now
                self.end_temp = cur_temp
                self.end_ts = now
            elif self.end_temp is None or cur_temp < self.end_temp:
                self.end_temp = cur_temp
                self.end_ts = now

        # Finalize when heating restarts
        cycle_result: CycleResult | None = None
        if current_action == _HA.HEATING and self.start_temp is not None:
            cycle_result = self._finalize()

        self._prev_action = current_action
        return HeatLossUpdate(cycle_result=cycle_result)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _finalize(self) -> CycleResult:
        """Evaluate the completed idle-cooling cycle."""
        loss_changed = False

        if self.end_temp is not None and self.start_ts is not None:
            temp_drop = self.start_temp - self.end_temp if self.start_temp is not None else 0.0
            if self.end_ts is not None and self.start_ts is not None:
                duration_min = (self.end_ts - self.start_ts).total_seconds() / 60.0
            else:
                duration_min = 0.0

            if duration_min >= _MIN_CYCLE_DURATION and temp_drop > 0:
                loss_rate = round(temp_drop / duration_min, 5)

                # Adaptive smoothing (alpha is always base for heat loss)
                alpha = clamp(_BASE_ALPHA, _ALPHA_MIN, _ALPHA_MAX)
                old_loss = self.heat_loss_rate
                unbounded = ema_smooth(old_loss, loss_rate, alpha)
                new_loss = clamp(unbounded, MIN_HEAT_LOSS, MAX_HEAT_LOSS)

                if new_loss != unbounded:
                    bound_name = (
                        "MIN_HEAT_LOSS"
                        if new_loss <= MIN_HEAT_LOSS
                        else "MAX_HEAT_LOSS"
                    )
                    _LOGGER.debug(
                        "better_thermostat: heat_loss clamped from %.4f to %.4f at %s "
                        "(min=%.4f, max=%.4f)",
                        unbounded,
                        new_loss,
                        bound_name,
                        MIN_HEAT_LOSS,
                        MAX_HEAT_LOSS,
                    )

                self.heat_loss_rate = round(new_loss, 5)
                loss_changed = self.heat_loss_rate != old_loss

                self.stats.append(
                    {
                        "dT": round(temp_drop, 2),
                        "min": round(duration_min, 1),
                        "rate": loss_rate,
                        "alpha": round(alpha, 3),
                        "loss": self.heat_loss_rate,
                    }
                )

                self.cycles.append(
                    {
                        "start": self.start_ts.isoformat() if self.start_ts else None,
                        "end": self.end_ts.isoformat() if self.end_ts else None,
                        "temp_start": (
                            round(self.start_temp, 2) if self.start_temp is not None else None
                        ),
                        "temp_min": (
                            round(self.end_temp, 2) if self.end_temp is not None else None
                        ),
                        "rate": loss_rate,
                    }
                )

        # Reset after finalize
        self.start_temp = None
        self.start_ts = None
        self.end_temp = None
        self.end_ts = None

        return CycleResult(loss_changed=loss_changed)
