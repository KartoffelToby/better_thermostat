"""Calibration strategies behind the core Calibrator contract.

Each strategy wraps one balance computation (MPC, TPI, PID) plus the
knowledge of how to read a valve percentage out of its result. The
shared dispatch in ``calibration.py`` resolves the configured mode to a
strategy and applies identical valve-fraction mathematics afterwards,
so the per-mode behavior stays exactly what it was.

``observe`` and ``actuate`` are one combined step here by design: the
controllers gate themselves in standby (PID and TPI skip, MPC drops the
in-flight learning interval and keeps the model) while the entity-level
estimates keep converging — see tests/unit/test_standby_contract.py for
the pinned contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import math
from typing import TYPE_CHECKING

from ...core.calibrator import CalibratorHealth, Capability, detect_oscillation
from ...core.snapshot import WorldSnapshot
from ..const import CalibrationMode
from .mpc import MpcOutput
from .tpi import TpiOutput

if TYPE_CHECKING:
    from ...climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

# A balance computation yields either a controller-specific output object
# (MPC/TPI) or a raw PID percentage, or ``None`` when no result was produced.
BalanceResult = MpcOutput | TpiOutput | float
# Each strategy's ``compute`` returns ``(result, use_valve)``.
ComputeBalance = Callable[
    ["BetterThermostat", str], tuple["BalanceResult | None", bool]
]
# Each strategy's ``percent_of`` reads a valve percentage out of a result.
PercentOf = Callable[["BalanceResult"], float | None]


# Grades the controllers' sanitize steps own (and may clear again).
SELF_HEAL_GRADES = (
    CalibratorHealth.NON_FINITE,
    CalibratorHealth.RUNAWAY_GAINS,
    CalibratorHealth.WINDUP_SUSPECT,
)


def annunciate_health(
    bt: BetterThermostat,
    entity_id: str,
    health: CalibratorHealth,
    *,
    recovers: tuple[CalibratorHealth, ...] = SELF_HEAL_GRADES,
) -> None:
    """Record a calibrator health grade on the TRV, logging transitions.

    Annunciation only: the self-healing lives in the controllers'
    sanitize steps, and the oscillation detector never backs gains off
    by itself. A HEALTHY verdict clears only the grades its reporter
    owns (``recovers``), so the sanitize path and the oscillation
    watcher do not flap each other's annunciations.
    """
    trv = bt.real_trvs.get(entity_id)
    if trv is None or trv.calibrator_health == health:
        return
    if health == CalibratorHealth.HEALTHY and trv.calibrator_health not in recovers:
        return
    if health == CalibratorHealth.HEALTHY:
        _LOGGER.info(
            "better_thermostat %s: calibrator for %s recovered (was %s)",
            bt.device_name,
            entity_id,
            trv.calibrator_health,
        )
    else:
        _LOGGER.warning(
            "better_thermostat %s: calibrator for %s reports %s",
            bt.device_name,
            entity_id,
            health,
        )
    trv.calibrator_health = health


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
    adjust: Callable[..., tuple[float, bool]] | None = None


@dataclass(frozen=True)
class BalanceStrategy:
    """One calibration mode's balance computation and result accessor."""

    mode: CalibrationMode
    compute: ComputeBalance
    percent_of: PercentOf

    def run(self, bt: BetterThermostat, entity_id: str) -> tuple[float | None, bool]:
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
        self._watch_oscillation(bt, entity_id, float(percent))
        return float(percent), bool(use_valve)

    def _watch_oscillation(
        self, bt: BetterThermostat, entity_id: str, percent: float
    ) -> None:
        """Feed the oscillation detector and annunciate its verdict."""
        trv = bt.real_trvs.get(entity_id)
        if trv is None:
            return
        trv.balance_percent_history.append(percent)
        oscillating = detect_oscillation(trv.balance_percent_history)
        annunciate_health(
            bt,
            entity_id,
            CalibratorHealth.OSCILLATING if oscillating else CalibratorHealth.HEALTHY,
            recovers=(CalibratorHealth.OSCILLATING,),
        )

    def capability(self, bt: BetterThermostat, entity_id: str) -> Capability:
        """Report the capability level for this TRV (annunciation only).

        A strategy is configured when selected, healthy when its inputs
        are present, and ready once a balance result exists.
        """
        trv = bt.real_trvs.get(entity_id)
        healthy = (
            trv is not None
            and bt.cur_temp is not None
            and bt.bt_target_temp is not None
            and trv.calibrator_health == CalibratorHealth.HEALTHY
        )
        ready = bool(healthy and trv is not None and trv.calibration_balance)
        return Capability(configured=True, healthy=bool(healthy), ready=ready)


class BalanceCalibrator:
    """Production adapter from a :class:`BalanceStrategy` to the core protocol.

    ``observe`` runs the strategy's balance computation (which both
    learns and emits; the controllers handle standby internally) and
    caches the result; ``actuate`` hands out the cached percentage once
    ``is_ready`` allows it. One instance belongs to one TRV of one
    entity; the dispatch in ``calibration.py`` keeps it on the Trv and
    rebuilds it when the configured mode changes.
    """

    def __init__(
        self, bt: BetterThermostat, entity_id: str, strategy: BalanceStrategy
    ) -> None:
        self._bt = bt
        self._entity_id = entity_id
        self.strategy = strategy
        self._last_percent: float | None = None
        self._last_use_valve = False

    def observe(self, snapshot: WorldSnapshot | None, now: float) -> None:
        """Run the balance computation and cache its result.

        The snapshot is accepted for protocol compatibility but unused:
        the strategy reads its inputs straight off the live entity.
        """
        self._last_percent, self._last_use_valve = self.strategy.run(
            self._bt, self._entity_id
        )

    def is_ready(self) -> bool:
        """Whether a usable (finite) balance result exists to actuate on.

        Deliberately narrower than :meth:`capability`: an OSCILLATING
        annunciation does not drop control to passthrough (automatic
        backoff on a false positive is worse than the oscillation —
        see the detector's note in ``core/calibrator.py``); only a
        missing or non-finite result blocks actuation.
        """
        return self._last_percent is not None and math.isfinite(self._last_percent)

    def actuate(self, snapshot: WorldSnapshot) -> float | None:
        """Return the cached setpoint-channel percentage, if any.

        A ``use_valve`` result is executed through the valve intent the
        computation already published, not through this channel.
        """
        if not self.is_ready() or self._last_use_valve:
            return None
        return self._last_percent

    def cached(self) -> tuple[float | None, bool]:
        """Return ``(percent, use_valve)`` once ready, else ``(None, False)``.

        Channel split for the dispatch: the setpoint/offset channel
        translates the percentage, the valve channel executes the intent
        the computation already published on the Trv.
        """
        if not self.is_ready():
            return None, False
        return self._last_percent, self._last_use_valve

    def capability(self) -> Capability:
        """Report the strategy's capability on the live entity."""
        return self.strategy.capability(self._bt, self._entity_id)

    def health(self) -> CalibratorHealth:
        """Report NON_FINITE when the cached result is not a finite number."""
        if self._last_percent is not None and not math.isfinite(self._last_percent):
            return CalibratorHealth.NON_FINITE
        return CalibratorHealth.HEALTHY


def _percent_of_mpc(result: BalanceResult) -> float | None:
    return getattr(result, "valve_percent", None)


def _percent_of_tpi(result: BalanceResult) -> float | None:
    return getattr(result, "duty_cycle_pct", None)


def _percent_of_pid(result: BalanceResult) -> float | None:
    return result if isinstance(result, (int, float)) else None


def build_strategy_registry(
    compute_mpc_balance: ComputeBalance,
    compute_tpi_balance: ComputeBalance,
    compute_pid_balance: ComputeBalance,
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
