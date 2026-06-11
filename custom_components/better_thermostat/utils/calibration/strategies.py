"""Calibration strategies behind the core Calibrator contract.

Each strategy wraps one balance computation (MPC, TPI, PID) plus the
knowledge of how to read a valve percentage out of its result. The
shared dispatch in ``calibration.py`` resolves the configured mode to a
strategy and applies identical valve-fraction mathematics afterwards,
so the per-mode behavior stays exactly what it was.

``observe`` and ``actuate`` are still one combined step here (the
balance computation both learns and emits); splitting them so the model
keeps converging in standby is the fail-soft ladder's work (M8).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math

from ...core.calibrator import CalibratorHealth, Capability
from ..const import CalibrationMode


@dataclass(frozen=True)
class ChannelAdjustment:
    """Channel-specific inputs for a mode's value adjustment.

    The two calibration channels (local offset, setpoint) share the
    mode logic but differ in direction and reference values; this
    carries the differences so one adjustment hook serves both.
    """

    hold_value: float
    legacy_fallback: Callable[[float], float]
    # Heating-promoting direction of the channel: -1.0 for the offset
    # channel (more negative opens the valve), +1.0 for the setpoint.
    boost_sign: float
    # Reference the boost distance is measured from: 0.0 for the offset
    # channel, the TRV-internal temperature for the setpoint.
    boost_neutral: float


@dataclass(frozen=True)
class ModeTraits:
    """Per-mode behavior of the calibration cascade.

    The cascade itself is identical for every mode; the traits carry
    everything mode-specific so the channel functions contain no mode
    branches. ``adjust`` receives the channel value, the current
    skip-post flag, and a :class:`ChannelAdjustment`, and returns both
    updated.
    """

    balance: BalanceStrategy | None = None
    needs_target: bool = True
    uses_tolerance_band: bool = True
    skip_post_adjustments: bool = False
    tolerance_delay: bool = True
    adjust: Callable | None = None


@dataclass(frozen=True)
class BalanceStrategy:
    """One calibration mode's balance computation and result accessor."""

    mode: CalibrationMode
    compute: Callable
    percent_of: Callable

    def run(self, bt, entity_id: str) -> tuple[float | None, bool]:
        """Run the balance computation.

        Returns ``(valve_percent, use_valve)``; the percentage is None
        when the computation produced no usable result.
        """
        result, use_valve = self.compute(bt, entity_id)
        if result is None:
            return None, bool(use_valve)
        percent = self.percent_of(result)
        if not isinstance(percent, (int, float)):
            return None, bool(use_valve)
        return float(percent), bool(use_valve)

    def capability(self, bt, entity_id: str) -> Capability:
        """Report the capability level for this TRV (annunciation only).

        A strategy is configured when selected, healthy when its inputs
        are present, and ready once a balance result exists.
        """
        trv = bt.real_trvs.get(entity_id)
        healthy = (
            trv is not None
            and bt.cur_temp is not None
            and bt.bt_target_temp is not None
        )
        ready = bool(healthy and trv is not None and trv.calibration_balance)
        return Capability(configured=True, healthy=bool(healthy), ready=ready)


class BalanceCalibrator:
    """Production adapter from a :class:`BalanceStrategy` to the core protocol.

    ``observe`` runs the strategy's balance computation (which both
    learns and emits — the standby split lives in the fail-soft ladder)
    and caches the result; ``actuate`` hands out the cached percentage.
    One instance belongs to one TRV of one entity.
    """

    def __init__(self, bt, entity_id: str, strategy: BalanceStrategy) -> None:
        self._bt = bt
        self._entity_id = entity_id
        self._strategy = strategy
        self._last_percent: float | None = None
        self._last_use_valve = False

    def observe(self, snapshot, now: float) -> None:
        """Run the balance computation and cache its result."""
        self._last_percent, self._last_use_valve = self._strategy.run(
            self._bt, self._entity_id
        )

    def is_ready(self) -> bool:
        """Whether the strategy reports a usable balance result."""
        return self.capability().ready

    def actuate(self, snapshot) -> float | None:
        """Return the cached setpoint-channel percentage, if any.

        A ``use_valve`` result is executed through the valve intent the
        computation already published, not through this channel.
        """
        if self._last_use_valve:
            return None
        return self._last_percent

    def capability(self) -> Capability:
        """Report the strategy's capability on the live entity."""
        return self._strategy.capability(self._bt, self._entity_id)

    def health(self) -> CalibratorHealth:
        """Report NON_FINITE when the cached result is not a finite number."""
        if self._last_percent is not None and not math.isfinite(self._last_percent):
            return CalibratorHealth.NON_FINITE
        return CalibratorHealth.HEALTHY


def _percent_of_mpc(result) -> float | None:
    return getattr(result, "valve_percent", None)


def _percent_of_tpi(result) -> float | None:
    return getattr(result, "duty_cycle_pct", None)


def _percent_of_pid(result) -> float | None:
    return result if isinstance(result, (int, float)) else None


def build_strategy_registry(
    compute_mpc_balance: Callable,
    compute_tpi_balance: Callable,
    compute_pid_balance: Callable,
) -> dict[CalibrationMode, BalanceStrategy]:
    """Build the mode-to-strategy registry from the balance callables."""
    return {
        CalibrationMode.MPC_CALIBRATION: BalanceStrategy(
            mode=CalibrationMode.MPC_CALIBRATION,
            compute=compute_mpc_balance,
            percent_of=_percent_of_mpc,
        ),
        CalibrationMode.TPI_CALIBRATION: BalanceStrategy(
            mode=CalibrationMode.TPI_CALIBRATION,
            compute=compute_tpi_balance,
            percent_of=_percent_of_tpi,
        ),
        CalibrationMode.PID_CALIBRATION: BalanceStrategy(
            mode=CalibrationMode.PID_CALIBRATION,
            compute=compute_pid_balance,
            percent_of=_percent_of_pid,
        ),
    }
