"""Shell-side thermal evidence tracker for the anomaly FSM region.

``AnomalyEvidenceTracker`` bridges the imperative shell (MPC v2 observer
diagnostics, ``HeatLossTracker``, and per-TRV raw local temperature history)
into pure ``ThermalEvidence`` and per-TRV slope values on ``WorldSnapshot``.

``observe_snapshot`` is strictly read-only so reconciler probes and repeated
``build_snapshot`` calls within a single cycle do not mutate rolling statistics
or advance TRV slope state. State advances exclusively inside ``commit_cycle``
when a control cycle commits.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
from typing import TYPE_CHECKING, Final, TypedDict

from ..core.desired import HeatingBlock
from ..core.fsm.anomaly import (
    DEFAULT_ANOMALY_PARAMS,
    AnomalyState,
    FaultPhase,
    FaultSlot,
    gating_codes,
    next_evaluation_monotonic,
    visible_codes,
)
from ..core.fsm.control_mode import ControlMode
from ..core.snapshot import ObserverReading, ThermalEvidence

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..core.snapshot import WorldSnapshot

DEFAULT_ARM_SAMPLES: Final[int] = 12
DEFAULT_RESIDUAL_TAU_SECONDS: Final[float] = 1800.0
DEFAULT_MIN_SIGMA_KELVIN_PER_MIN: Final[float] = 0.005
# A source the radiator does not explain is a disturbance that leaves the
# room's own baseline. The threshold is the residual floor the anomaly
# detectors use, so both read the same magnitude as a finding; the smaller
# one ends the finding again, so a source around the threshold does not
# flicker.
DEFAULT_SOURCE_ON_KELVIN_PER_MIN: Final[float] = 0.005
DEFAULT_SOURCE_OFF_KELVIN_PER_MIN: Final[float] = 0.002
DEFAULT_BASELINE_TAU_SECONDS: Final[float] = 43_200.0
DEFAULT_TRV_MIN_DT_SECONDS: Final[float] = 30.0
DEFAULT_TRV_TAU_SECONDS: Final[float] = 180.0
DEFAULT_TRV_MAX_GAP_SECONDS: Final[float] = 1800.0
DEFAULT_INNOVATION_HOLD_TAU_SECONDS: Final[float] = 300.0
ROOM_SLOPE_WINDOW_MIN_SECONDS: Final[float] = 150.0
ROOM_SLOPE_WINDOW_TARGET_SECONDS: Final[float] = 180.0
ROOM_SLOPE_WINDOW_MAX_SECONDS: Final[float] = 240.0
POST_CONTACT_FLUSH_SECONDS: Final[float] = 300.0


class FaultSlotExport(TypedDict):
    """JSON-serializable representation of a single ``FaultSlot``."""

    phase: str
    since: float | None
    active_since: float | None


class TrvFaultExport(TypedDict):
    """JSON-serializable representation of per-TRV fault slots."""

    no_heat_delivery: FaultSlotExport
    valve_stuck_open: FaultSlotExport


class AnomalyDiagnosticsExport(TypedDict):
    """JSON-serializable diagnostics payload for the anomaly subsystem."""

    enabled: bool
    visible_anomalies: list[str]
    trv_anomalies: dict[str, list[str]]
    gating_anomalies: list[str]
    next_evaluation_monotonic: float | None
    slots: dict[str, FaultSlotExport]
    trv_slots: dict[str, TrvFaultExport]
    residual_mean_kelvin_per_min: float
    residual_sigma_kelvin_per_min: float | None
    residual_samples: int
    disturbance_baseline_kelvin_per_min: float
    trv_raw_slopes_kelvin_per_min: dict[str, float | None]


def compute_expected_passive_slope(
    *,
    room_temperature: float | None,
    outdoor_temperature: float | None,
    heat_loss_rate_kelvin_per_hour_per_kelvin: float | None,
) -> float | None:
    """Compute the expected passive cooling rate in K/min from ``heat_loss_rate``.

    Parameters
    ----------
    room_temperature : float | None
        Current room temperature in degrees Celsius.
    outdoor_temperature : float | None
        Current outdoor temperature in degrees Celsius.
    heat_loss_rate_kelvin_per_hour_per_kelvin : float | None
        Learned envelope loss coefficient in ``(K/h) / K``.

    Returns
    -------
    float | None
        Expected passive cooling slope in ``K/min`` (non-positive), or ``None``
        when any input is unavailable or the learned coefficient is non-positive.
    """
    if (
        room_temperature is None
        or outdoor_temperature is None
        or heat_loss_rate_kelvin_per_hour_per_kelvin is None
        or heat_loss_rate_kelvin_per_hour_per_kelvin <= 0.0
    ):
        return None
    delta_kelvin = max(0.0, room_temperature - outdoor_temperature)
    return -heat_loss_rate_kelvin_per_hour_per_kelvin * delta_kelvin / 60.0


def compute_rc2_model_slope(
    *,
    room_temperature_hat: float | None,
    radiator_temperature_hat: float | None,
    outdoor_temperature: float | None,
    disturbance_baseline_kelvin_per_min: float,
    k_rad_room_per_min: float | None,
    k_loss_per_min: float | None,
) -> float | None:
    """Compute the 2R2C model-predicted room temperature slope in K/min.

    Parameters
    ----------
    room_temperature_hat : float | None
        Kalman-estimated room air temperature in degrees Celsius.
    radiator_temperature_hat : float | None
        Kalman-estimated radiator surface temperature in degrees Celsius.
    outdoor_temperature : float | None
        Outdoor temperature in degrees Celsius, or ``None`` when unavailable.
    disturbance_baseline_kelvin_per_min : float
        Slow baseline disturbance in ``K/min``.
    k_rad_room_per_min : float | None
        Radiator-to-room coupling rate ``1 / (R_rad * C_room)`` in ``1/min``.
    k_loss_per_min : float | None
        Envelope loss rate ``1 / (R_env * C_room)`` in ``1/min``.

    Returns
    -------
    float | None
        Predicted room slope in ``K/min``, or ``None`` when observer parameters
        are unavailable.
    """
    if (
        room_temperature_hat is None
        or radiator_temperature_hat is None
        or k_rad_room_per_min is None
        or k_loss_per_min is None
    ):
        return None
    loss_term = (
        k_loss_per_min * (room_temperature_hat - outdoor_temperature)
        if outdoor_temperature is not None
        else 0.0
    )
    return (
        k_rad_room_per_min * (radiator_temperature_hat - room_temperature_hat)
        - loss_term
        + disturbance_baseline_kelvin_per_min
    )


def compute_slope_residual(
    *,
    observed_slope_kelvin_per_min: float | None,
    expected_slope_kelvin_per_min: float | None,
) -> float | None:
    """Return ``observed_slope - expected_slope`` in K/min when both are known.

    Parameters
    ----------
    observed_slope_kelvin_per_min : float | None
        Measured room temperature slope in ``K/min``.
    expected_slope_kelvin_per_min : float | None
        Model-predicted room temperature slope in ``K/min``.

    Returns
    -------
    float | None
        Slope residual in ``K/min``, or ``None`` when either input is missing.
    """
    if observed_slope_kelvin_per_min is None or expected_slope_kelvin_per_min is None:
        return None
    return observed_slope_kelvin_per_min - expected_slope_kelvin_per_min


@dataclass
class _TrvSample:
    """Last committed raw temperature sample and EMA slope for one TRV."""

    monotonic: float
    raw_temperature: float
    ema_slope_kelvin_per_min: float | None = None


@dataclass
class _ObserverCache:
    """Latest MPC v2 observer state cached from ``_compute_mpc_v2_balance``."""

    monotonic: float
    room_temperature_hat: float
    radiator_temperature_hat: float
    disturbance_kelvin_per_min: float
    disturbance_at_bound: bool
    normalised_innovation: float
    room_time_constant_minutes: float
    k_rad_room_per_min: float
    k_loss_per_min: float


def _export_slot(slot: FaultSlot) -> FaultSlotExport:
    """Serialize a ``FaultSlot`` into a JSON-compatible dictionary."""
    return {
        "phase": slot.phase.value,
        "since": slot.since,
        "active_since": slot.active_since,
    }


def _source_active(
    current: bool, deviation: float, *, threshold: float, release: float, sign: int
) -> bool:
    """Return whether a source is active, with a band between the thresholds.

    Parameters
    ----------
    current : bool
        Whether the source was active before this reading.
    deviation : float
        The observer disturbance relative to the room's baseline, in K/min.
    threshold : float
        Deviation from which the source counts as active.
    release : float
        Deviation below which an active source is over.
    sign : int
        ``1`` for a heat gain and ``-1`` for a heat loss, so the deviation
        is read in the direction of the source it belongs to.

    Returns
    -------
    bool
        Whether the source is active now.
    """
    value = sign * deviation
    if current:
        return value >= release
    return value >= threshold


def _has_any_non_clear_slot(state: AnomalyState) -> bool:
    """Return whether any room or TRV slot is outside ``FaultPhase.CLEAR``."""
    if (
        state.rapid_loss.phase != FaultPhase.CLEAR
        or state.parasitic_loss.phase != FaultPhase.CLEAR
        or state.no_heat_delivery.phase != FaultPhase.CLEAR
    ):
        return True
    return any(
        trv.no_heat_delivery.phase != FaultPhase.CLEAR
        or trv.valve_stuck_open.phase != FaultPhase.CLEAR
        for trv in state.trvs.values()
    )


@dataclass
class AnomalyEvidenceTracker:
    """Stateful shell accumulator producing ``ThermalEvidence`` per snapshot."""

    arm_samples: int = DEFAULT_ARM_SAMPLES
    residual_tau_seconds: float = DEFAULT_RESIDUAL_TAU_SECONDS
    min_sigma_kelvin_per_min: float = DEFAULT_MIN_SIGMA_KELVIN_PER_MIN
    baseline_tau_seconds: float = DEFAULT_BASELINE_TAU_SECONDS
    trv_min_dt_seconds: float = DEFAULT_TRV_MIN_DT_SECONDS
    trv_tau_seconds: float = DEFAULT_TRV_TAU_SECONDS
    trv_max_gap_seconds: float = DEFAULT_TRV_MAX_GAP_SECONDS
    innovation_hold_tau_seconds: float = DEFAULT_INNOVATION_HOLD_TAU_SECONDS

    _observer: _ObserverCache | None = field(default=None, init=False, repr=False)
    _last_observer_room_temperature: float | None = field(
        default=None, init=False, repr=False
    )
    _disturbance_baseline_kelvin_per_min: float = field(
        default=0.0, init=False, repr=False
    )
    _heat_source_active: bool = field(default=False, init=False, repr=False)
    _cool_source_active: bool = field(default=False, init=False, repr=False)
    _residual_mean_kelvin_per_min: float = field(default=0.0, init=False, repr=False)
    _residual_variance: float = field(default=0.0, init=False, repr=False)
    _residual_samples: int = field(default=0, init=False, repr=False)
    _last_commit_monotonic: float | None = field(default=None, init=False, repr=False)
    _last_contact_or_block_monotonic: float | None = field(
        default=None, init=False, repr=False
    )
    _room_temperature_window: deque[tuple[float, float]] = field(
        default_factory=deque, init=False, repr=False
    )
    _trv_history: dict[str, _TrvSample] = field(
        default_factory=dict, init=False, repr=False
    )
    _last_anomaly_state: AnomalyState = field(
        default_factory=AnomalyState, init=False, repr=False
    )
    _last_anomaly_detection: bool = field(default=False, init=False, repr=False)
    _last_now_monotonic: float = field(default=0.0, init=False, repr=False)

    def reset(self) -> None:
        """Drop all accumulated evidence and return to the unarmed state."""
        self._observer = None
        self._last_observer_room_temperature = None
        self._disturbance_baseline_kelvin_per_min = 0.0
        self._heat_source_active = False
        self._cool_source_active = False
        self._residual_mean_kelvin_per_min = 0.0
        self._residual_variance = 0.0
        self._residual_samples = 0
        self._last_commit_monotonic = None
        self._last_contact_or_block_monotonic = None
        self._room_temperature_window.clear()
        self._trv_history.clear()
        self._last_anomaly_state = AnomalyState()
        self._last_anomaly_detection = False
        self._last_now_monotonic = 0.0

    @property
    def disturbance_baseline_kelvin_per_min(self) -> float:
        """Return the slow EMA baseline of the observer disturbance."""
        return self._disturbance_baseline_kelvin_per_min

    @property
    def heat_source_active(self) -> bool:
        """Whether a heat gain the radiator does not explain is active."""
        return self._heat_source_active

    @property
    def cool_source_active(self) -> bool:
        """Whether a heat loss the radiator does not explain is active."""
        return self._cool_source_active

    def fresh_observer_disturbance(self, now_monotonic: float) -> float | None:
        """Return the observer disturbance while the reading is fresh.

        Parameters
        ----------
        now_monotonic : float
            Current monotonic timestamp.

        Returns
        -------
        float | None
            The disturbance in ``K/min``, or ``None`` when there is no
            reading or the last one is too old to stand for the room now.
        """
        if self._observer is None:
            return None
        age = now_monotonic - self._observer.monotonic
        if not 0.0 <= age <= DEFAULT_ANOMALY_PARAMS.max_observer_age_seconds:
            return None
        return self._observer.disturbance_kelvin_per_min

    @property
    def residual_samples(self) -> int:
        """Return the number of normal samples accumulated in the residual EWMA."""
        return self._residual_samples

    @property
    def residual_sigma_kelvin_per_min(self) -> float | None:
        """Return the armed residual standard deviation in ``K/min``, or ``None``."""
        if self._residual_samples < self.arm_samples:
            return None
        return max(
            self.min_sigma_kelvin_per_min, math.sqrt(max(0.0, self._residual_variance))
        )

    @property
    def room_time_constant_minutes(self) -> float | None:
        """Return the latest observer room time constant in minutes, if known."""
        if self._observer is None:
            return None
        return self._observer.room_time_constant_minutes

    def record_mpc_v2_diagnostics(
        self,
        *,
        now_monotonic: float,
        room_temperature_hat: float,
        radiator_temperature_hat: float,
        disturbance_kelvin_per_min: float,
        disturbance_at_bound: bool,
        normalised_innovation: float,
        room_time_constant_minutes: float,
        k_rad_room_per_min: float,
        k_loss_per_min: float,
        learning_held: bool,
        room_temperature: float | None = None,
        contact_open: bool = False,
    ) -> None:
        """Record an MPC v2 observer step and update the slow disturbance baseline.

        Parameters
        ----------
        now_monotonic : float
            Monotonic timestamp of the MPC v2 step.
        room_temperature_hat : float
            Kalman-estimated room temperature in degrees Celsius.
        radiator_temperature_hat : float
            Kalman-estimated radiator surface temperature in degrees Celsius.
        disturbance_kelvin_per_min : float
            Clamped DOB disturbance estimate ``D_hat`` in ``K/min``.
        disturbance_at_bound : bool
            Whether ``D_hat`` is saturated at its floor or ceiling.
        normalised_innovation : float
            Prior room prediction innovation divided by innovation sigma.
        room_time_constant_minutes : float
            Effective room thermal time constant in minutes.
        k_rad_room_per_min : float
            Radiator-to-room coupling coefficient in ``1/min``.
        k_loss_per_min : float
            Envelope heat-loss rate coefficient in ``1/min``.
        learning_held : bool
            Whether learner updates are currently held.
        room_temperature : float | None, optional
            Raw room temperature reading at this step, used to hold innovation
            across quantized sensor plateaus.
        contact_open : bool, optional
            Whether a window/door contact sensor is currently open.
        """
        dt = (
            max(0.0, now_monotonic - self._observer.monotonic)
            if self._observer is not None
            else 60.0
        )

        if contact_open:
            self._last_contact_or_block_monotonic = now_monotonic
        in_post_contact_flush = (
            self._last_contact_or_block_monotonic is not None
            and 0.0
            <= now_monotonic - self._last_contact_or_block_monotonic
            <= POST_CONTACT_FLUSH_SECONDS
        )

        effective_innovation = normalised_innovation
        if contact_open or in_post_contact_flush:
            effective_innovation = 0.0
        elif self._observer is not None and room_temperature is not None:
            prev_room = self._last_observer_room_temperature
            prev_innov = self._observer.normalised_innovation
            if prev_room is not None and abs(room_temperature - prev_room) < 1e-6:
                if (
                    prev_innov < 0.0
                    and normalised_innovation
                    < DEFAULT_ANOMALY_PARAMS.parasitic_recovery_innovation_sigma
                    and abs(normalised_innovation) < abs(prev_innov)
                ):
                    decay = (
                        math.exp(-dt / self.innovation_hold_tau_seconds)
                        if dt > 0.0 and self.innovation_hold_tau_seconds > 0.0
                        else 1.0
                    )
                    effective_innovation = prev_innov * decay
            elif (
                prev_room is not None
                and room_temperature < prev_room
                and prev_innov < 0.0
                and normalised_innovation < 0.0
            ):
                decay = (
                    math.exp(-dt / self.innovation_hold_tau_seconds)
                    if dt > 0.0 and self.innovation_hold_tau_seconds > 0.0
                    else 1.0
                )
                effective_innovation = min(normalised_innovation, prev_innov * decay)

        if room_temperature is not None:
            self._last_observer_room_temperature = room_temperature

        # The source flags read the baseline as it was before this reading,
        # so a source is measured against what the room usually does.
        deviation = (
            disturbance_kelvin_per_min - self._disturbance_baseline_kelvin_per_min
        )
        self._heat_source_active = _source_active(
            self._heat_source_active,
            deviation,
            threshold=DEFAULT_SOURCE_ON_KELVIN_PER_MIN,
            release=DEFAULT_SOURCE_OFF_KELVIN_PER_MIN,
            sign=1,
        )
        self._cool_source_active = _source_active(
            self._cool_source_active,
            deviation,
            threshold=DEFAULT_SOURCE_ON_KELVIN_PER_MIN,
            release=DEFAULT_SOURCE_OFF_KELVIN_PER_MIN,
            sign=-1,
        )

        has_anomalies = _has_any_non_clear_slot(self._last_anomaly_state)
        if (
            not learning_held
            and not contact_open
            and not has_anomalies
            and self.baseline_tau_seconds > 0.0
        ):
            alpha = 1.0 - math.exp(
                -min(max(dt, 1.0), 3600.0) / self.baseline_tau_seconds
            )
            self._disturbance_baseline_kelvin_per_min += alpha * (
                disturbance_kelvin_per_min - self._disturbance_baseline_kelvin_per_min
            )

        self._observer = _ObserverCache(
            monotonic=now_monotonic,
            room_temperature_hat=room_temperature_hat,
            radiator_temperature_hat=radiator_temperature_hat,
            disturbance_kelvin_per_min=disturbance_kelvin_per_min,
            disturbance_at_bound=disturbance_at_bound,
            normalised_innovation=effective_innovation,
            room_time_constant_minutes=room_time_constant_minutes,
            k_rad_room_per_min=k_rad_room_per_min,
            k_loss_per_min=k_loss_per_min,
        )

    def _preview_trv_slope(
        self, entity_id: str, raw_temperature: float | None, now_monotonic: float
    ) -> float | None:
        """Compute the TRV raw temperature slope without mutating state."""
        if raw_temperature is None:
            return None
        sample = self._trv_history.get(entity_id)
        if sample is None:
            return None
        dt = now_monotonic - sample.monotonic
        if dt > self.trv_max_gap_seconds or dt < 0.0:
            return None
        if dt < self.trv_min_dt_seconds:
            return sample.ema_slope_kelvin_per_min
        inst_slope = (raw_temperature - sample.raw_temperature) / (dt / 60.0)
        if sample.ema_slope_kelvin_per_min is None or self.trv_tau_seconds <= 0.0:
            return inst_slope
        alpha = 1.0 - math.exp(-dt / self.trv_tau_seconds)
        return sample.ema_slope_kelvin_per_min + alpha * (
            inst_slope - sample.ema_slope_kelvin_per_min
        )

    def _windowed_room_slope(
        self,
        *,
        now_monotonic: float,
        room_temperature: float | None,
        fallback_slope_kelvin_per_min: float | None,
        contact_open: bool,
    ) -> tuple[float | None, bool]:
        """Return ``(observed_slope, from_window)`` using the committed 3-min buffer."""
        if contact_open:
            return 0.0, True

        in_post_contact_flush = (
            self._last_contact_or_block_monotonic is not None
            and 0.0
            <= now_monotonic - self._last_contact_or_block_monotonic
            <= POST_CONTACT_FLUSH_SECONDS
        )

        if room_temperature is not None and self._room_temperature_window:
            last_t, prev_room_temperature = self._room_temperature_window[-1]
            if (
                room_temperature > prev_room_temperature + 1e-6
                and now_monotonic >= last_t
            ):
                dt_last = max(60.0, now_monotonic - last_t)
                return (
                    (room_temperature - prev_room_temperature) / (dt_last / 60.0),
                    True,
                )

            if in_post_contact_flush:
                return 0.0, True

            best_ref: tuple[float, float] | None = None
            for ref_t, ref_temperature in self._room_temperature_window:
                dt = now_monotonic - ref_t
                if ROOM_SLOPE_WINDOW_MIN_SECONDS <= dt <= ROOM_SLOPE_WINDOW_MAX_SECONDS:
                    if best_ref is None or abs(
                        dt - ROOM_SLOPE_WINDOW_TARGET_SECONDS
                    ) < abs(
                        (now_monotonic - best_ref[0]) - ROOM_SLOPE_WINDOW_TARGET_SECONDS
                    ):
                        best_ref = (ref_t, ref_temperature)
            if best_ref is not None:
                dt = now_monotonic - best_ref[0]
                return (room_temperature - best_ref[1]) / (dt / 60.0), True

        if in_post_contact_flush:
            return 0.0, True

        return fallback_slope_kelvin_per_min, False

    def observe_snapshot(
        self,
        *,
        now_monotonic: float,
        room_temperature: float | None,
        room_temperature_filtered: float | None,
        outdoor_temperature: float | None,
        temperature_slope_kelvin_per_min: float | None,
        heat_loss_rate_kelvin_per_hour_per_kelvin: float | None,
        trv_raw_temperatures: Mapping[str, float | None],
        contact_open: bool = False,
    ) -> tuple[ThermalEvidence, dict[str, float | None]]:
        """Build ``ThermalEvidence`` and per-TRV raw slopes without mutating state.

        Parameters
        ----------
        now_monotonic : float
            Monotonic timestamp of the snapshot.
        room_temperature : float | None
            Raw room temperature in degrees Celsius.
        room_temperature_filtered : float | None
            Filtered room temperature in degrees Celsius.
        outdoor_temperature : float | None
            Outdoor temperature in degrees Celsius.
        temperature_slope_kelvin_per_min : float | None
            Room temperature EMA slope in ``K/min``.
        heat_loss_rate_kelvin_per_hour_per_kelvin : float | None
            Learned envelope heat-loss rate in ``(K/h) / K``.
        trv_raw_temperatures : Mapping[str, float | None]
            Per-TRV raw temperature readings keyed by ``entity_id``.
        contact_open : bool, optional
            Whether any contact sensor is currently open or debouncing.

        Returns
        -------
        tuple[ThermalEvidence, dict[str, float | None]]
            Pure ``ThermalEvidence`` and a mapping of ``entity_id`` to its
            previewed raw temperature slope in ``K/min``.
        """
        trv_slopes: dict[str, float | None] = {
            entity_id: self._preview_trv_slope(
                entity_id, raw_temperature, now_monotonic
            )
            for entity_id, raw_temperature in trv_raw_temperatures.items()
        }

        ref_temperature = (
            room_temperature_filtered
            if room_temperature_filtered is not None
            else room_temperature
        )
        passive_slope = compute_expected_passive_slope(
            room_temperature=ref_temperature,
            outdoor_temperature=outdoor_temperature,
            heat_loss_rate_kelvin_per_hour_per_kelvin=(
                heat_loss_rate_kelvin_per_hour_per_kelvin
            ),
        )

        observer_reading: ObserverReading | None = None
        rc2_slope: float | None = None
        if self._observer is not None:
            age_seconds = max(0.0, now_monotonic - self._observer.monotonic)
            observer_reading = ObserverReading(
                age_seconds=age_seconds,
                disturbance_kelvin_per_min=self._observer.disturbance_kelvin_per_min,
                disturbance_baseline_kelvin_per_min=(
                    self._disturbance_baseline_kelvin_per_min
                ),
                disturbance_at_bound=self._observer.disturbance_at_bound,
                normalised_innovation=self._observer.normalised_innovation,
                room_time_constant_minutes=(self._observer.room_time_constant_minutes),
            )
            if age_seconds <= DEFAULT_ANOMALY_PARAMS.max_observer_age_seconds:
                rc2_slope = compute_rc2_model_slope(
                    room_temperature_hat=self._observer.room_temperature_hat,
                    radiator_temperature_hat=self._observer.radiator_temperature_hat,
                    outdoor_temperature=outdoor_temperature,
                    disturbance_baseline_kelvin_per_min=(
                        self._disturbance_baseline_kelvin_per_min
                    ),
                    k_rad_room_per_min=self._observer.k_rad_room_per_min,
                    k_loss_per_min=self._observer.k_loss_per_min,
                )

        observed_slope, from_window = self._windowed_room_slope(
            now_monotonic=now_monotonic,
            room_temperature=room_temperature,
            fallback_slope_kelvin_per_min=temperature_slope_kelvin_per_min,
            contact_open=contact_open,
        )

        expected_for_residual = (
            passive_slope
            if passive_slope is not None
            else (
                min(0.0, rc2_slope)
                if rc2_slope is not None
                else (0.0 if from_window else None)
            )
        )
        raw_residual = compute_slope_residual(
            observed_slope_kelvin_per_min=observed_slope,
            expected_slope_kelvin_per_min=expected_for_residual,
        )

        reported_expected_passive = (
            expected_for_residual
            if expected_for_residual is not None and raw_residual is not None
            else passive_slope
        )

        evidence = ThermalEvidence(
            expected_passive_slope_kelvin_per_min=reported_expected_passive,
            slope_residual_kelvin_per_min=raw_residual,
            slope_residual_sigma_kelvin_per_min=self.residual_sigma_kelvin_per_min,
            observer=observer_reading,
        )
        return evidence, trv_slopes

    def commit_cycle(
        self,
        snapshot: WorldSnapshot,
        anomaly_state: AnomalyState,
        *,
        anomaly_detection: bool = False,
        control_mode: ControlMode = ControlMode.OPTIMAL,
        learning_held: bool = False,
        heating_blocked_by: HeatingBlock | None = None,
        contact_open: bool = False,
    ) -> None:
        """Advance rolling residual statistics, room buffer, and TRV slopes on commit.

        Parameters
        ----------
        snapshot : WorldSnapshot
            Committed cycle snapshot.
        anomaly_state : AnomalyState
            Post-``decide()`` anomaly region state.
        anomaly_detection : bool, optional
            Whether ``KernelState.anomaly_detection`` is enabled.
        control_mode : ControlMode, optional
            Current control mode of the thermostat.
        learning_held : bool, optional
            Whether learning is held on this cycle.
        heating_blocked_by : HeatingBlock | None, optional
            Active heating block reason, or ``None``.
        contact_open : bool, optional
            Whether any contact sensor is open or debouncing.
        """
        now_monotonic = snapshot.now_monotonic
        self._last_anomaly_state = anomaly_state
        self._last_anomaly_detection = anomaly_detection
        self._last_now_monotonic = now_monotonic

        for entity_id, trv in snapshot.trvs.items():
            if trv.raw_temperature is None:
                continue
            prev = self._trv_history.get(entity_id)
            if prev is None:
                self._trv_history[entity_id] = _TrvSample(
                    monotonic=now_monotonic,
                    raw_temperature=trv.raw_temperature,
                    ema_slope_kelvin_per_min=None,
                )
                continue
            dt_trv = now_monotonic - prev.monotonic
            if dt_trv > self.trv_max_gap_seconds or dt_trv < 0.0:
                self._trv_history[entity_id] = _TrvSample(
                    monotonic=now_monotonic,
                    raw_temperature=trv.raw_temperature,
                    ema_slope_kelvin_per_min=None,
                )
            elif dt_trv >= self.trv_min_dt_seconds:
                new_slope = self._preview_trv_slope(
                    entity_id, trv.raw_temperature, now_monotonic
                )
                self._trv_history[entity_id] = _TrvSample(
                    monotonic=now_monotonic,
                    raw_temperature=trv.raw_temperature,
                    ema_slope_kelvin_per_min=new_slope,
                )

        if (
            contact_open
            or heating_blocked_by is not None
            or control_mode != ControlMode.OPTIMAL
            or snapshot.room_temperature is None
        ):
            if contact_open or heating_blocked_by is not None:
                self._last_contact_or_block_monotonic = now_monotonic
            self._room_temperature_window.clear()
        else:
            if (
                not self._room_temperature_window
                or now_monotonic > self._room_temperature_window[-1][0]
            ):
                self._room_temperature_window.append(
                    (now_monotonic, snapshot.room_temperature)
                )
            cutoff = now_monotonic - ROOM_SLOPE_WINDOW_MAX_SECONDS
            while (
                len(self._room_temperature_window) > 2
                and self._room_temperature_window[0][0] < cutoff
            ):
                self._room_temperature_window.popleft()

        dt_commit = (
            max(0.0, now_monotonic - self._last_commit_monotonic)
            if self._last_commit_monotonic is not None
            else 60.0
        )
        self._last_commit_monotonic = now_monotonic

        has_anomalies = _has_any_non_clear_slot(anomaly_state)
        evidence = snapshot.thermal_evidence
        residual = (
            evidence.slope_residual_kelvin_per_min if evidence is not None else None
        )
        if (
            not learning_held
            and not contact_open
            and heating_blocked_by is None
            and control_mode == ControlMode.OPTIMAL
            and not has_anomalies
            and residual is not None
            and self.residual_tau_seconds > 0.0
        ):
            alpha = 1.0 - math.exp(
                -min(max(dt_commit, 1.0), 3600.0) / self.residual_tau_seconds
            )
            if self._residual_samples == 0:
                self._residual_mean_kelvin_per_min = residual
                self._residual_variance = 0.0
            else:
                delta = residual - self._residual_mean_kelvin_per_min
                self._residual_mean_kelvin_per_min += alpha * delta
                self._residual_variance = (1.0 - alpha) * (
                    self._residual_variance + alpha * delta * delta
                )
            self._residual_samples += 1

    def export(self) -> AnomalyDiagnosticsExport:
        """Export a JSON-safe snapshot of the anomaly state and evidence tracker."""
        state = self._last_anomaly_state
        now_monotonic = self._last_now_monotonic
        room_codes, trv_codes = (
            visible_codes(state) if self._last_anomaly_detection else ((), {})
        )
        gating = (
            gating_codes(state, now_monotonic)
            if self._last_anomaly_detection
            else frozenset()
        )
        due = (
            next_evaluation_monotonic(
                state, room_time_constant_minutes=self.room_time_constant_minutes
            )
            if self._last_anomaly_detection
            else None
        )
        return {
            "enabled": self._last_anomaly_detection,
            "visible_anomalies": [code.value for code in room_codes],
            "trv_anomalies": {
                entity_id: [code.value for code in codes]
                for entity_id, codes in trv_codes.items()
            },
            "gating_anomalies": sorted(code.value for code in gating),
            "next_evaluation_monotonic": due,
            "slots": {
                "rapid_loss": _export_slot(state.rapid_loss),
                "parasitic_loss": _export_slot(state.parasitic_loss),
                "no_heat_delivery": _export_slot(state.no_heat_delivery),
            },
            "trv_slots": {
                entity_id: {
                    "no_heat_delivery": _export_slot(trv_slot.no_heat_delivery),
                    "valve_stuck_open": _export_slot(trv_slot.valve_stuck_open),
                }
                for entity_id, trv_slot in state.trvs.items()
            },
            "residual_mean_kelvin_per_min": self._residual_mean_kelvin_per_min,
            "residual_sigma_kelvin_per_min": self.residual_sigma_kelvin_per_min,
            "residual_samples": self._residual_samples,
            "disturbance_baseline_kelvin_per_min": (
                self._disturbance_baseline_kelvin_per_min
            ),
            "trv_raw_slopes_kelvin_per_min": {
                entity_id: sample.ema_slope_kelvin_per_min
                for entity_id, sample in sorted(self._trv_history.items())
            },
        }


__all__ = [
    "AnomalyDiagnosticsExport",
    "AnomalyEvidenceTracker",
    "DEFAULT_ARM_SAMPLES",
    "DEFAULT_BASELINE_TAU_SECONDS",
    "DEFAULT_INNOVATION_HOLD_TAU_SECONDS",
    "DEFAULT_MIN_SIGMA_KELVIN_PER_MIN",
    "DEFAULT_RESIDUAL_TAU_SECONDS",
    "DEFAULT_TRV_MAX_GAP_SECONDS",
    "DEFAULT_TRV_MIN_DT_SECONDS",
    "DEFAULT_TRV_TAU_SECONDS",
    "FaultSlotExport",
    "TrvFaultExport",
    "compute_expected_passive_slope",
    "compute_rc2_model_slope",
    "compute_slope_residual",
]
