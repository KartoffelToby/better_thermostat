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

from ...core.calibrator import Capability
from ..const import CalibrationMode


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
