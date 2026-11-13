"""Anomaly region: sensorless thermal and actuator fault gating.

Tracks four fault classes from continuous shell-computed residuals, MPC v2
observer diagnostics, and unshifted TRV temperatures:

* ``UNEXPECTED_RAPID_LOSS`` — unmonitored open window (room-level, 60-minute
  liveness bound).
* ``SUSTAINED_PARASITIC_LOSS`` — unmonitored open interior door or steady
  extra heat loss (room-level, 24-hour liveness bound).
* ``NO_HEAT_DELIVERY`` — heating supply outage when every addressed TRV stays
  cold, or single-TRV failure when a sibling warms up.
* ``VALVE_STUCK_OPEN`` — mechanically stuck-open or leaking valve while
  heating is off or commanded shut (per TRV).

Each detector steps through ``CLEAR -> SUSPECT -> ACTIVE -> RECOVERING ->
CLEAR``, with liveness-bounded detectors transitioning ``ACTIVE -> EXPIRED``
once their maximum gating runtime has elapsed. ``EXPIRED`` remains visible for
diagnostics while ceasing to hold learning or suppress heating.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..desired import AnomalyCode, HeatingBlock
from ..snapshot import HvacMode, ObserverReading, TrvReported, WorldSnapshot
from .control_mode import ControlMode


class FaultPhase(StrEnum):
    """Discrete lifecycle phases of a single fault detector."""

    CLEAR = "clear"
    SUSPECT = "suspect"
    ACTIVE = "active"
    RECOVERING = "recovering"
    EXPIRED = "expired"


@dataclass(frozen=True)
class FaultSlot:
    """Timer state of one fault detector.

    ``since`` marks the start of a pending dwell: onset in ``SUSPECT`` and
    recovery in ``RECOVERING`` or ``EXPIRED``. ``active_since`` stamps when the
    current episode entered ``ACTIVE`` and stays fixed across ``RECOVERING`` so
    oscillating evidence cannot stretch the liveness bound.
    """

    phase: FaultPhase = FaultPhase.CLEAR
    since: float | None = None
    active_since: float | None = None

    @property
    def is_visible(self) -> bool:
        """Whether this detector currently annunciates its fault code."""
        return self.phase in (
            FaultPhase.ACTIVE,
            FaultPhase.RECOVERING,
            FaultPhase.EXPIRED,
        )

    def is_gating(
        self, now_monotonic: float, liveness_seconds: float | None = None
    ) -> bool:
        """Whether this detector may hold learning or suppress heating."""
        if self.phase not in (FaultPhase.ACTIVE, FaultPhase.RECOVERING):
            return False
        if liveness_seconds is None:
            return True
        return (
            self.active_since is not None
            and (now_monotonic - self.active_since) < liveness_seconds
        )


@dataclass(frozen=True)
class TrvFaultState:
    """Per-TRV fault slots for single-valve heat failure and stuck-open leak."""

    no_heat_delivery: FaultSlot = field(default_factory=FaultSlot)
    valve_stuck_open: FaultSlot = field(default_factory=FaultSlot)

    @property
    def is_clear(self) -> bool:
        """Whether every fault slot of this TRV is in ``CLEAR``."""
        return (
            self.no_heat_delivery.phase == FaultPhase.CLEAR
            and self.valve_stuck_open.phase == FaultPhase.CLEAR
        )


@dataclass(frozen=True)
class AnomalyState:
    """Aggregate state of the anomaly region."""

    rapid_loss: FaultSlot = field(default_factory=FaultSlot)
    parasitic_loss: FaultSlot = field(default_factory=FaultSlot)
    no_heat_delivery: FaultSlot = field(default_factory=FaultSlot)
    trvs: Mapping[str, TrvFaultState] = field(default_factory=dict)
    last_rapid_loss_monotonic: float | None = None

    def __post_init__(self) -> None:
        """Freeze the per-TRV mapping to preserve immutability at runtime."""
        object.__setattr__(self, "trvs", MappingProxyType(dict(self.trvs)))


@dataclass(frozen=True)
class AnomalyParams:
    """Thresholds, dwell windows, and liveness bounds of the anomaly region."""

    max_observer_age_seconds: float = 660.0
    rapid_loss_innovation_sigma: float = 3.0
    rapid_loss_residual_sigma: float = 3.0
    rapid_loss_min_drop_kelvin_per_min: float = 0.05
    rapid_loss_dwell_seconds: float = 180.0
    rapid_loss_recovery_seconds: float = 300.0
    rapid_loss_liveness_seconds: float = 3600.0
    parasitic_loss_kelvin_per_min: float = 0.007
    parasitic_recovery_innovation_sigma: float = 0.3
    parasitic_dwell_seconds: float = 900.0
    parasitic_recovery_seconds: float = 900.0
    parasitic_liveness_seconds: float = 86_400.0
    parasitic_after_rapid_cooldown_seconds: float = 900.0
    no_heat_min_deficit_kelvin: float = 0.5
    no_heat_min_valve_percent: float = 50.0
    no_heat_min_setpoint_lead_kelvin: float = 2.0
    no_heat_max_room_slope_kelvin_per_min: float = 0.0
    no_heat_warming_trv_slope_kelvin_per_min: float = 0.02
    valve_command_delay_seconds: float = 90.0
    default_room_time_constant_minutes: float = 90.0
    min_no_heat_dwell_seconds: float = 900.0
    max_no_heat_dwell_seconds: float = 4800.0
    no_heat_recovery_seconds: float = 600.0
    stuck_open_trv_excess_kelvin: float = 3.0
    stuck_open_min_residual_kelvin_per_min: float = 0.0
    stuck_open_max_solar_intensity: float = 0.6
    stuck_open_dwell_seconds: float = 1800.0
    stuck_open_recovery_seconds: float = 900.0


DEFAULT_ANOMALY_PARAMS: Final = AnomalyParams()


def step_slot(
    slot: FaultSlot,
    evidence: bool,
    now_monotonic: float,
    *,
    dwell_seconds: float,
    recovery_seconds: float,
    liveness_seconds: float | None = None,
    force_clear: bool = False,
) -> FaultSlot:
    """Advance one fault slot by a single boolean evidence observation.

    Parameters
    ----------
    slot : FaultSlot
        Current state of the fault slot.
    evidence : bool
        Whether the fault signature holds on this snapshot.
    now_monotonic : float
        Monotonic timestamp of the snapshot.
    dwell_seconds : float
        Continuous evidence duration required to commit ``SUSPECT -> ACTIVE``.
    recovery_seconds : float
        Continuous absence duration required to clear ``ACTIVE`` or ``EXPIRED``.
    liveness_seconds : float | None, optional
        Maximum duration from ``active_since`` before ``ACTIVE``/``RECOVERING``
        demotes to non-gating ``EXPIRED``; ``None`` when unbounded.
    force_clear : bool, optional
        Immediate reset when an external precondition explains or voids the
        signature (such as an open contact sensor).

    Returns
    -------
    FaultSlot
        Updated fault slot.
    """
    if force_clear:
        return FaultSlot()

    phase = slot.phase

    if phase == FaultPhase.CLEAR:
        if not evidence:
            return slot
        if dwell_seconds <= 0.0:
            return FaultSlot(phase=FaultPhase.ACTIVE, active_since=now_monotonic)
        return FaultSlot(phase=FaultPhase.SUSPECT, since=now_monotonic)

    if phase == FaultPhase.SUSPECT:
        if not evidence or slot.since is None:
            return FaultSlot()
        if now_monotonic - slot.since >= dwell_seconds:
            return FaultSlot(phase=FaultPhase.ACTIVE, active_since=now_monotonic)
        return slot

    if phase == FaultPhase.ACTIVE:
        if slot.active_since is None:
            return FaultSlot()
        if (
            liveness_seconds is not None
            and now_monotonic - slot.active_since >= liveness_seconds
        ):
            if not evidence:
                return FaultSlot()
            return FaultSlot(phase=FaultPhase.EXPIRED, active_since=slot.active_since)
        if evidence:
            return slot
        if recovery_seconds <= 0.0:
            return FaultSlot()
        return FaultSlot(
            phase=FaultPhase.RECOVERING,
            since=now_monotonic,
            active_since=slot.active_since,
        )

    if phase == FaultPhase.RECOVERING:
        if slot.active_since is None or slot.since is None:
            return FaultSlot()
        if (
            liveness_seconds is not None
            and now_monotonic - slot.active_since >= liveness_seconds
        ):
            if not evidence:
                return FaultSlot()
            return FaultSlot(phase=FaultPhase.EXPIRED, active_since=slot.active_since)
        if evidence:
            return FaultSlot(phase=FaultPhase.ACTIVE, active_since=slot.active_since)
        if now_monotonic - slot.since >= recovery_seconds:
            return FaultSlot()
        return slot

    # EXPIRED
    if slot.active_since is None:
        return FaultSlot()
    if evidence:
        return FaultSlot(phase=FaultPhase.EXPIRED, active_since=slot.active_since)
    if recovery_seconds <= 0.0:
        return FaultSlot()
    if slot.since is None:
        return FaultSlot(
            phase=FaultPhase.EXPIRED,
            since=now_monotonic,
            active_since=slot.active_since,
        )
    if now_monotonic - slot.since >= recovery_seconds:
        return FaultSlot()
    return slot


def _fresh_observer(
    snapshot: WorldSnapshot, params: AnomalyParams
) -> ObserverReading | None:
    """Return the snapshot's observer reading when within the freshness window."""
    evidence = snapshot.thermal_evidence
    if evidence is None or evidence.observer is None:
        return None
    observer = evidence.observer
    if 0.0 <= observer.age_seconds <= params.max_observer_age_seconds:
        return observer
    return None


def _room_reference_temperature(snapshot: WorldSnapshot) -> float | None:
    """Return the filtered room temperature, falling back to the raw reading."""
    if snapshot.room_temperature_filtered is not None:
        return snapshot.room_temperature_filtered
    return snapshot.room_temperature


def no_heat_dwell_seconds(
    snapshot: WorldSnapshot, params: AnomalyParams = DEFAULT_ANOMALY_PARAMS
) -> float:
    """Return the ``NO_HEAT_DELIVERY`` onset dwell scaled to the room's inertia.

    Parameters
    ----------
    snapshot : WorldSnapshot
        Snapshot carrying an optional observer time constant.
    params : AnomalyParams, optional
        Anomaly parameters providing bounds and fallback time constant.

    Returns
    -------
    float
        Dwell time in seconds, clamped to ``[min_no_heat_dwell_seconds,
        max_no_heat_dwell_seconds]``.
    """
    observer = _fresh_observer(snapshot, params)
    tau_minutes = (
        observer.room_time_constant_minutes
        if observer is not None and observer.room_time_constant_minutes > 0.0
        else params.default_room_time_constant_minutes
    )
    raw_dwell = params.valve_command_delay_seconds + 0.5 * tau_minutes * 60.0
    return max(
        params.min_no_heat_dwell_seconds,
        min(params.max_no_heat_dwell_seconds, raw_dwell),
    )


def _rapid_loss_evidence(
    snapshot: WorldSnapshot,
    control_mode: ControlMode,
    observer: ObserverReading | None,
    params: AnomalyParams,
) -> bool:
    """Evaluate whether the unmonitored rapid-loss signature holds."""
    if control_mode != ControlMode.OPTIMAL:
        return False
    room_temperature = _room_reference_temperature(snapshot)
    if (
        room_temperature is not None
        and snapshot.heat_target_temperature is not None
        and room_temperature > snapshot.heat_target_temperature + 0.5
    ):
        return False
    evidence = snapshot.thermal_evidence
    if (
        evidence is not None
        and evidence.slope_residual_kelvin_per_min is not None
        and evidence.slope_residual_kelvin_per_min >= 0.0
    ):
        return False
    effective_slope = snapshot.temperature_slope
    if (
        evidence is not None
        and evidence.slope_residual_kelvin_per_min is not None
        and evidence.expected_passive_slope_kelvin_per_min is not None
    ):
        effective_slope = evidence.slope_residual_kelvin_per_min + min(
            0.0, evidence.expected_passive_slope_kelvin_per_min
        )
    if (
        effective_slope is None
        or effective_slope > -params.rapid_loss_min_drop_kelvin_per_min
    ):
        return False
    if (
        observer is not None
        and observer.normalised_innovation <= -params.rapid_loss_innovation_sigma
    ):
        return True
    if evidence is None:
        return False
    residual = evidence.slope_residual_kelvin_per_min
    sigma = evidence.slope_residual_sigma_kelvin_per_min
    return (
        residual is not None
        and sigma is not None
        and sigma > 0.0
        and residual <= -params.rapid_loss_residual_sigma * sigma
    )


def _parasitic_loss_evidence(
    slot: FaultSlot,
    snapshot: WorldSnapshot,
    control_mode: ControlMode,
    observer: ObserverReading | None,
    last_rapid_loss_monotonic: float | None,
    params: AnomalyParams,
) -> bool:
    """Evaluate whether the sustained parasitic-loss signature holds."""
    if control_mode != ControlMode.OPTIMAL or observer is None:
        return False
    if (
        last_rapid_loss_monotonic is not None
        and snapshot.now_monotonic - last_rapid_loss_monotonic
        < params.parasitic_after_rapid_cooldown_seconds
    ):
        return False
    disturbance_shift = (
        observer.disturbance_kelvin_per_min
        - observer.disturbance_baseline_kelvin_per_min
    )
    negative_disturbance = (
        disturbance_shift <= -params.parasitic_loss_kelvin_per_min
        or (observer.disturbance_at_bound and observer.disturbance_kelvin_per_min < 0.0)
    )
    if not negative_disturbance:
        return False
    if slot.is_visible:
        if observer.normalised_innovation >= params.parasitic_recovery_innovation_sigma:
            return False
        evidence = snapshot.thermal_evidence
        if (
            evidence is not None
            and evidence.slope_residual_kelvin_per_min is not None
            and evidence.slope_residual_kelvin_per_min
            > -0.5 * params.parasitic_loss_kelvin_per_min
        ):
            return False
    elif snapshot.temperature_slope is not None and snapshot.temperature_slope > 0.005:
        return False
    return True


def _trv_has_high_demand(trv: TrvReported, params: AnomalyParams) -> bool:
    """Return whether a TRV is strongly demanding heat."""
    if (
        trv.valve_percent is not None
        and trv.valve_percent >= params.no_heat_min_valve_percent
    ):
        return True
    trv_temperature = (
        trv.raw_temperature
        if trv.raw_temperature is not None
        else trv.current_temperature
    )
    return (
        trv.setpoint is not None
        and trv_temperature is not None
        and (trv.setpoint - trv_temperature) >= params.no_heat_min_setpoint_lead_kelvin
    )


def _trv_is_warming(trv: TrvReported, params: AnomalyParams) -> bool:
    """Return whether a TRV's internal sensor is actively warming."""
    return (
        trv.raw_temperature_slope_kelvin_per_min is not None
        and trv.raw_temperature_slope_kelvin_per_min
        > params.no_heat_warming_trv_slope_kelvin_per_min
    )


def _trv_is_hot_above_room(
    trv: TrvReported, room_temperature: float, params: AnomalyParams
) -> bool:
    """Return whether a TRV's radiator body is already hot above the room."""
    return (
        trv.raw_temperature is not None
        and (trv.raw_temperature - room_temperature)
        > params.stuck_open_trv_excess_kelvin
    )


def _no_heat_evidence(
    snapshot: WorldSnapshot,
    heating_blocked_by: HeatingBlock | None,
    addressed_trvs: Sequence[str],
    params: AnomalyParams,
) -> tuple[bool, dict[str, bool]]:
    """Evaluate room-level and per-TRV ``NO_HEAT_DELIVERY`` evidence."""
    room_temperature = _room_reference_temperature(snapshot)
    if (
        heating_blocked_by is not None
        or snapshot.hvac_mode == HvacMode.COOL
        or room_temperature is None
        or (snapshot.heat_target_temperature - room_temperature)
        < params.no_heat_min_deficit_kelvin
        or not addressed_trvs
    ):
        return False, {}

    demanding = [
        entity_id
        for entity_id in addressed_trvs
        if _trv_has_high_demand(snapshot.trvs[entity_id], params)
    ]
    if not demanding:
        return False, {}

    delivering = {
        entity_id
        for entity_id in addressed_trvs
        if _trv_is_warming(snapshot.trvs[entity_id], params)
        or _trv_is_hot_above_room(snapshot.trvs[entity_id], room_temperature, params)
    }

    if delivering:
        per_trv = {
            entity_id: True
            for entity_id in demanding
            if entity_id not in delivering
            and snapshot.trvs[entity_id].raw_temperature is not None
        }
        return False, per_trv

    room_stalled = (
        snapshot.temperature_slope is not None
        and snapshot.temperature_slope <= params.no_heat_max_room_slope_kelvin_per_min
    )
    return room_stalled, {}


def _valve_stuck_open_evidence(
    trv: TrvReported,
    snapshot: WorldSnapshot,
    heating_blocked_by: HeatingBlock | None,
    room_temperature: float | None,
    params: AnomalyParams,
) -> bool:
    """Evaluate per-TRV ``VALVE_STUCK_OPEN`` evidence."""
    if heating_blocked_by in (
        HeatingBlock.DEVICES_RELEASED,
        HeatingBlock.STARTUP,
        HeatingBlock.VALVE_MAINTENANCE,
    ):
        return False
    if not trv.available or trv.released:
        return False
    if (
        snapshot.is_day
        and snapshot.solar_intensity > params.stuck_open_max_solar_intensity
    ):
        return False
    heating_expected_shut = (
        heating_blocked_by in (HeatingBlock.HVAC_OFF, HeatingBlock.NO_CALL_FOR_HEAT)
        or trv.hvac_mode == HvacMode.OFF
        or (trv.valve_percent is not None and trv.valve_percent <= 0.0)
    )
    if not heating_expected_shut:
        return False
    if room_temperature is None or not _trv_is_hot_above_room(
        trv, room_temperature, params
    ):
        return False
    evidence = snapshot.thermal_evidence
    if evidence is not None and evidence.slope_residual_kelvin_per_min is not None:
        # A residual on a slope nobody measured says nothing about a room
        # that keeps warming.
        return (
            evidence.slope_measured
            and evidence.slope_residual_kelvin_per_min
            >= params.stuck_open_min_residual_kelvin_per_min
        )
    return (
        snapshot.temperature_slope is not None
        and snapshot.temperature_slope >= params.stuck_open_min_residual_kelvin_per_min
    )


def step(
    state: AnomalyState,
    snapshot: WorldSnapshot,
    *,
    control_mode: ControlMode,
    heating_blocked_by: HeatingBlock | None,
    contact_open: bool,
    addressed_trvs: Sequence[str],
    params: AnomalyParams = DEFAULT_ANOMALY_PARAMS,
) -> AnomalyState:
    """Advance the anomaly region from one world snapshot.

    Parameters
    ----------
    state : AnomalyState
        Previous anomaly region state.
    snapshot : WorldSnapshot
        Current immutable observation of the room and its TRVs.
    control_mode : ControlMode
        Active rung of the degradation ladder.
    heating_blocked_by : HeatingBlock | None
        Upstream cascade block before any anomaly-driven suppression, or
        ``None`` when the heating rung is reached.
    contact_open : bool
        Whether a physical window or door contact region is effectively open.
    addressed_trvs : Sequence[str]
        Entity IDs of TRVs addressed on this cycle.
    params : AnomalyParams, optional
        Detection thresholds, dwell windows, and liveness bounds.

    Returns
    -------
    AnomalyState
        Successor anomaly state.
    """
    now = snapshot.now_monotonic
    released_or_starting = heating_blocked_by in (
        HeatingBlock.DEVICES_RELEASED,
        HeatingBlock.STARTUP,
    )
    observer = _fresh_observer(snapshot, params)

    rapid_evidence = _rapid_loss_evidence(snapshot, control_mode, observer, params)
    rapid_slot = step_slot(
        state.rapid_loss,
        rapid_evidence,
        now,
        dwell_seconds=params.rapid_loss_dwell_seconds,
        recovery_seconds=params.rapid_loss_recovery_seconds,
        liveness_seconds=params.rapid_loss_liveness_seconds,
        force_clear=contact_open or released_or_starting,
    )
    last_rapid_loss = (
        now
        if (rapid_slot.is_visible or contact_open)
        else state.last_rapid_loss_monotonic
    )

    parasitic_evidence = _parasitic_loss_evidence(
        state.parasitic_loss, snapshot, control_mode, observer, last_rapid_loss, params
    )
    parasitic_slot = step_slot(
        state.parasitic_loss,
        parasitic_evidence,
        now,
        dwell_seconds=params.parasitic_dwell_seconds,
        recovery_seconds=params.parasitic_recovery_seconds,
        liveness_seconds=params.parasitic_liveness_seconds,
        force_clear=contact_open or released_or_starting,
    )

    room_no_heat_ev, per_trv_no_heat_ev = _no_heat_evidence(
        snapshot, heating_blocked_by, addressed_trvs, params
    )
    dynamic_no_heat_dwell = no_heat_dwell_seconds(snapshot, params)
    no_heat_slot = step_slot(
        state.no_heat_delivery,
        room_no_heat_ev,
        now,
        dwell_seconds=dynamic_no_heat_dwell,
        recovery_seconds=params.no_heat_recovery_seconds,
        force_clear=released_or_starting,
    )

    room_temperature = _room_reference_temperature(snapshot)
    next_trvs: dict[str, TrvFaultState] = {}
    for entity_id, trv in snapshot.trvs.items():
        prior = state.trvs.get(entity_id, TrvFaultState())
        trv_no_heat = step_slot(
            prior.no_heat_delivery,
            per_trv_no_heat_ev.get(entity_id, False),
            now,
            dwell_seconds=dynamic_no_heat_dwell,
            recovery_seconds=params.no_heat_recovery_seconds,
            force_clear=released_or_starting or trv.released,
        )
        trv_stuck = step_slot(
            prior.valve_stuck_open,
            _valve_stuck_open_evidence(
                trv, snapshot, heating_blocked_by, room_temperature, params
            ),
            now,
            dwell_seconds=params.stuck_open_dwell_seconds,
            recovery_seconds=params.stuck_open_recovery_seconds,
            force_clear=released_or_starting or trv.released,
        )
        updated_trv = TrvFaultState(
            no_heat_delivery=trv_no_heat, valve_stuck_open=trv_stuck
        )
        if not updated_trv.is_clear:
            next_trvs[entity_id] = updated_trv

    return AnomalyState(
        rapid_loss=rapid_slot,
        parasitic_loss=parasitic_slot,
        no_heat_delivery=no_heat_slot,
        trvs=next_trvs,
        last_rapid_loss_monotonic=last_rapid_loss,
    )


def visible_codes(
    state: AnomalyState,
) -> tuple[tuple[AnomalyCode, ...], dict[str, tuple[AnomalyCode, ...]]]:
    """Return the room-wide union and per-TRV visible anomaly codes.

    Parameters
    ----------
    state : AnomalyState
        Anomaly region state to inspect.

    Returns
    -------
    tuple[tuple[AnomalyCode, ...], dict[str, tuple[AnomalyCode, ...]]]
        ``(anomalies, trv_anomalies)`` sorted for deterministic equality and
        JSON export. ``EXPIRED`` slots remain visible here for diagnostics.
    """
    codes: set[AnomalyCode] = set()
    if state.rapid_loss.is_visible:
        codes.add(AnomalyCode.UNEXPECTED_RAPID_LOSS)
    if state.parasitic_loss.is_visible:
        codes.add(AnomalyCode.SUSTAINED_PARASITIC_LOSS)
    if state.no_heat_delivery.is_visible:
        codes.add(AnomalyCode.NO_HEAT_DELIVERY)

    trv_codes: dict[str, tuple[AnomalyCode, ...]] = {}
    for entity_id in sorted(state.trvs):
        entry = state.trvs[entity_id]
        per_trv: list[AnomalyCode] = []
        if entry.no_heat_delivery.is_visible:
            per_trv.append(AnomalyCode.NO_HEAT_DELIVERY)
            codes.add(AnomalyCode.NO_HEAT_DELIVERY)
        if entry.valve_stuck_open.is_visible:
            per_trv.append(AnomalyCode.VALVE_STUCK_OPEN)
            codes.add(AnomalyCode.VALVE_STUCK_OPEN)
        if per_trv:
            trv_codes[entity_id] = tuple(per_trv)

    return tuple(sorted(codes)), trv_codes


def gating_codes(
    state: AnomalyState,
    now_monotonic: float,
    params: AnomalyParams = DEFAULT_ANOMALY_PARAMS,
) -> frozenset[AnomalyCode]:
    """Return the anomaly codes currently permitted to gate learning or heating.

    Excludes ``EXPIRED`` slots and any liveness-bounded slot whose runtime has
    reached its cap at ``now_monotonic``.

    Parameters
    ----------
    state : AnomalyState
        Anomaly region state to inspect.
    now_monotonic : float
        Monotonic timestamp against which liveness bounds are checked.
    params : AnomalyParams, optional
        Parameters supplying the liveness bounds.

    Returns
    -------
    frozenset[AnomalyCode]
        Active gating codes.
    """
    codes: set[AnomalyCode] = set()
    if state.rapid_loss.is_gating(now_monotonic, params.rapid_loss_liveness_seconds):
        codes.add(AnomalyCode.UNEXPECTED_RAPID_LOSS)
    if state.parasitic_loss.is_gating(now_monotonic, params.parasitic_liveness_seconds):
        codes.add(AnomalyCode.SUSTAINED_PARASITIC_LOSS)
    if state.no_heat_delivery.is_gating(now_monotonic):
        codes.add(AnomalyCode.NO_HEAT_DELIVERY)
    for entry in state.trvs.values():
        if entry.no_heat_delivery.is_gating(now_monotonic):
            codes.add(AnomalyCode.NO_HEAT_DELIVERY)
        if entry.valve_stuck_open.is_gating(now_monotonic):
            codes.add(AnomalyCode.VALVE_STUCK_OPEN)
    return frozenset(codes)


def _slot_deadline(
    slot: FaultSlot,
    *,
    dwell_seconds: float,
    recovery_seconds: float,
    liveness_seconds: float | None = None,
) -> float | None:
    """Return the next monotonic deadline at which ``slot`` could transition."""
    if slot.phase == FaultPhase.SUSPECT and slot.since is not None:
        return slot.since + dwell_seconds
    if slot.phase == FaultPhase.ACTIVE:
        if liveness_seconds is not None and slot.active_since is not None:
            return slot.active_since + liveness_seconds
        return None
    if slot.phase == FaultPhase.RECOVERING and slot.since is not None:
        recovery_at = slot.since + recovery_seconds
        if liveness_seconds is not None and slot.active_since is not None:
            return min(recovery_at, slot.active_since + liveness_seconds)
        return recovery_at
    if slot.phase == FaultPhase.EXPIRED and slot.since is not None:
        return slot.since + recovery_seconds
    return None


def next_evaluation_monotonic(
    state: AnomalyState,
    params: AnomalyParams = DEFAULT_ANOMALY_PARAMS,
    *,
    room_time_constant_minutes: float | None = None,
) -> float | None:
    """Return the earliest monotonic timestamp when a pending timer expires.

    Parameters
    ----------
    state : AnomalyState
        Current anomaly state.
    params : AnomalyParams, optional
        Dwell, recovery, and liveness durations.
    room_time_constant_minutes : float | None, optional
        Current room time constant in minutes for scaling ``NO_HEAT_DELIVERY``
        dwell; falls back to ``params.default_room_time_constant_minutes``.

    Returns
    -------
    float | None
        Earliest monotonic deadline, or ``None`` when no timer is running.
    """
    tau_minutes = (
        room_time_constant_minutes
        if room_time_constant_minutes is not None and room_time_constant_minutes > 0.0
        else params.default_room_time_constant_minutes
    )
    dynamic_no_heat_dwell = max(
        params.min_no_heat_dwell_seconds,
        min(
            params.max_no_heat_dwell_seconds,
            params.valve_command_delay_seconds + 0.5 * tau_minutes * 60.0,
        ),
    )

    candidates = [
        _slot_deadline(
            state.rapid_loss,
            dwell_seconds=params.rapid_loss_dwell_seconds,
            recovery_seconds=params.rapid_loss_recovery_seconds,
            liveness_seconds=params.rapid_loss_liveness_seconds,
        ),
        _slot_deadline(
            state.parasitic_loss,
            dwell_seconds=params.parasitic_dwell_seconds,
            recovery_seconds=params.parasitic_recovery_seconds,
            liveness_seconds=params.parasitic_liveness_seconds,
        ),
        _slot_deadline(
            state.no_heat_delivery,
            dwell_seconds=dynamic_no_heat_dwell,
            recovery_seconds=params.no_heat_recovery_seconds,
        ),
    ]
    for entry in state.trvs.values():
        candidates.append(
            _slot_deadline(
                entry.no_heat_delivery,
                dwell_seconds=dynamic_no_heat_dwell,
                recovery_seconds=params.no_heat_recovery_seconds,
            )
        )
        candidates.append(
            _slot_deadline(
                entry.valve_stuck_open,
                dwell_seconds=params.stuck_open_dwell_seconds,
                recovery_seconds=params.stuck_open_recovery_seconds,
            )
        )
    deadlines = [deadline for deadline in candidates if deadline is not None]
    return min(deadlines) if deadlines else None
