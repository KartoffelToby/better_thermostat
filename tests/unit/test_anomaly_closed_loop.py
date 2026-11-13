"""The anomaly region and its evidence tracker over hours of one room.

Each test walks a room through control cycles a minute apart. The evidence
comes from the real ``AnomalyEvidenceTracker`` and the decision from the real
``decide()``, so a finding has to survive what its own consequence does to
the evidence it stands on.
"""

from __future__ import annotations

from custom_components.better_thermostat.core.decide import KernelState, decide
from custom_components.better_thermostat.core.desired import (
    AnomalyCode,
    DesiredState,
    HeatingBlock,
)
from custom_components.better_thermostat.core.fsm.anomaly import (
    DEFAULT_ANOMALY_PARAMS,
    FaultPhase,
    gating_codes,
)
from custom_components.better_thermostat.core.fsm.mode import ModeState
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.snapshot import HvacMode, TrvReported
from custom_components.better_thermostat.utils.anomaly_evidence import (
    DEFAULT_ARM_SAMPLES,
    AnomalyEvidenceTracker,
)
from tests.factories import make_snapshot, make_state

TRV = "climate.trv"
TARGET = 21.0
OUTDOOR = 0.0
# Learned envelope loss in (K/h)/K: a room 20 K above the outdoor
# temperature cools by 0.01 K/min while nothing heats it.
HEAT_LOSS_RATE = 0.03
PASSIVE_SLOPE = -0.01
OPEN_WINDOW_SLOPE = -0.15
LIVENESS_MINUTES = int(DEFAULT_ANOMALY_PARAMS.rapid_loss_liveness_seconds // 60)


class _Room:
    """A room whose temperature the test moves, minute by minute."""

    def __init__(self, state: KernelState, *, room_temperature: float = 20.0) -> None:
        self.state = state
        self.tracker = AnomalyEvidenceTracker()
        self.now_monotonic = 0.0
        self.room_temperature = room_temperature

    def minute(
        self,
        slope: float,
        *,
        trv_excess: float = 0.0,
        valve_shut: bool = False,
        call_for_heat: bool = True,
    ) -> DesiredState:
        """Run the control cycle a minute on, the room having moved by ``slope``."""
        self.now_monotonic += 60.0
        self.room_temperature += slope
        contact_open = (
            self.state.window.effective_open or self.state.door.effective_open
        )
        raw_temperature = self.room_temperature + trv_excess
        evidence, trv_slopes = self.tracker.observe_snapshot(
            now_monotonic=self.now_monotonic,
            room_temperature=self.room_temperature,
            room_temperature_filtered=self.room_temperature,
            outdoor_temperature=OUTDOOR,
            temperature_slope_kelvin_per_min=slope,
            heat_loss_rate_kelvin_per_hour_per_kelvin=HEAT_LOSS_RATE,
            trv_raw_temperatures={TRV: raw_temperature},
            contact_open=contact_open,
        )
        snapshot = make_snapshot(
            now_monotonic=self.now_monotonic,
            heat_target_temperature=TARGET,
            hvac_mode=self.state.mode.hvac_mode,
            room_temperature=self.room_temperature,
            room_temperature_filtered=self.room_temperature,
            temperature_slope=slope,
            outdoor_temperature=OUTDOOR,
            call_for_heat=call_for_heat,
            thermal_evidence=evidence,
            trvs={
                TRV: TrvReported(
                    entity_id=TRV,
                    hvac_mode=HvacMode.OFF if valve_shut else HvacMode.HEAT,
                    raw_temperature=raw_temperature,
                    raw_temperature_slope_kelvin_per_min=trv_slopes[TRV],
                    valve_percent=0.0 if valve_shut else 40.0,
                )
            },
        )
        desired, self.state = decide(snapshot, self.state)
        self.tracker.commit_cycle(
            snapshot,
            self.state.anomaly,
            anomaly_detection=self.state.anomaly_detection,
            control_mode=self.state.control_mode.mode,
            learning_held=bool(gating_codes(self.state.anomaly, self.now_monotonic)),
            heating_blocked_by=desired.heating_blocked_by,
            contact_open=contact_open,
        )
        return desired

    def settle(self) -> None:
        """Let the room cool at its usual rate until the residual band is armed."""
        for _ in range(DEFAULT_ARM_SAMPLES + 4):
            self.minute(PASSIVE_SLOPE)
        assert self.tracker.residual_sigma_kelvin_per_min is not None
        assert self.state.anomaly.rapid_loss.phase == FaultPhase.CLEAR


def _stuck_open_phase(room: _Room) -> FaultPhase:
    slots = room.state.anomaly.trvs.get(TRV)
    return FaultPhase.CLEAR if slots is None else slots.valve_stuck_open.phase


def test_a_suspected_window_stays_suppressed_until_its_liveness_bound() -> None:
    """Closing the valves does not end the finding that closed them.

    The window stays open, so the room keeps falling while heating is
    suppressed. The suppression holds for the whole liveness bound in one
    piece. After that the finding stays visible and the room is heated again.
    """
    room = _Room(make_state(anomaly_detection=True, anomaly_suppression=True))
    room.settle()

    blocks = [
        room.minute(OPEN_WINDOW_SLOPE).heating_blocked_by
        for _ in range(LIVENESS_MINUTES + 20)
    ]

    suppressed_from = blocks.index(HeatingBlock.SUSPECTED_WINDOW)
    assert suppressed_from <= 6
    assert set(blocks[:suppressed_from]) == {None}
    suppressed = blocks[suppressed_from : suppressed_from + LIVENESS_MINUTES]
    assert set(suppressed) == {HeatingBlock.SUSPECTED_WINDOW}
    assert set(blocks[suppressed_from + LIVENESS_MINUTES :]) == {None}
    assert room.state.anomaly.rapid_loss.phase == FaultPhase.EXPIRED

    still_open = room.minute(OPEN_WINDOW_SLOPE)
    assert still_open.heating_blocked_by is None
    assert still_open.anomalies == (AnomalyCode.UNEXPECTED_RAPID_LOSS,)


def test_a_suspected_window_that_closes_gives_the_room_its_heat_back() -> None:
    room = _Room(make_state(anomaly_detection=True, anomaly_suppression=True))
    room.settle()
    for _ in range(14):
        room.minute(OPEN_WINDOW_SLOPE)
    suppressed = room.minute(OPEN_WINDOW_SLOPE)
    assert suppressed.heating_blocked_by == HeatingBlock.SUSPECTED_WINDOW

    recovery_minutes = int(DEFAULT_ANOMALY_PARAMS.rapid_loss_recovery_seconds // 60)
    blocks = [
        room.minute(PASSIVE_SLOPE).heating_blocked_by
        for _ in range(recovery_minutes + 6)
    ]

    assert blocks[-1] is None
    assert room.state.anomaly.rapid_loss.phase == FaultPhase.CLEAR
    # Suppressed until the recovery dwell has run, then heated: one change.
    assert blocks == sorted(blocks, key=lambda block: block is None)


def test_a_room_cooling_under_a_warm_radiator_is_no_stuck_valve() -> None:
    """A radiator still warm above a room that cools on is no leaking valve.

    With heating off for the season the room loses heat faster than its
    envelope explains, so it is not being warmed: the TRV sits on a warm
    riser pipe.
    """
    room = _Room(make_state(anomaly_detection=True), room_temperature=22.0)

    for _ in range(45):
        room.minute(-0.02, trv_excess=4.0, valve_shut=True, call_for_heat=False)
        assert _stuck_open_phase(room) == FaultPhase.CLEAR


def test_a_room_warming_under_a_shut_valve_is_reported_as_stuck_open() -> None:
    room = _Room(make_state(anomaly_detection=True), room_temperature=22.0)
    dwell_minutes = int(DEFAULT_ANOMALY_PARAMS.stuck_open_dwell_seconds // 60)

    for _ in range(dwell_minutes + 1):
        room.minute(0.01, trv_excess=4.0, valve_shut=True, call_for_heat=False)
    desired = room.minute(0.01, trv_excess=4.0, valve_shut=True, call_for_heat=False)

    assert desired.heating_blocked_by == HeatingBlock.NO_CALL_FOR_HEAT
    assert _stuck_open_phase(room) == FaultPhase.ACTIVE
    assert desired.trv_anomalies == {TRV: (AnomalyCode.VALVE_STUCK_OPEN,)}


def test_a_room_warming_with_heating_switched_off_is_reported_as_stuck_open() -> None:
    room = _Room(
        make_state(anomaly_detection=True, mode=ModeState(hvac_mode=HvacMode.OFF)),
        room_temperature=22.0,
    )
    dwell_minutes = int(DEFAULT_ANOMALY_PARAMS.stuck_open_dwell_seconds // 60)

    for _ in range(dwell_minutes + 1):
        room.minute(0.01, trv_excess=4.0, valve_shut=True)
    desired = room.minute(0.01, trv_excess=4.0, valve_shut=True)

    assert desired.heating_blocked_by == HeatingBlock.HVAC_OFF
    assert _stuck_open_phase(room) == FaultPhase.ACTIVE


def test_an_open_window_is_no_evidence_of_a_stuck_valve() -> None:
    """The room is not read under an open contact, so it proves no leak.

    The TRV's own sensor trails the room air a window chills, which leaves
    it several Kelvin above the room for as long as the window is open.
    """
    room = _Room(
        make_state(anomaly_detection=True, window=WindowState(phase=WindowPhase.OPEN))
    )

    for _ in range(45):
        desired = room.minute(-0.1, trv_excess=4.0, valve_shut=True)
        assert desired.heating_blocked_by == HeatingBlock.WINDOW
        assert _stuck_open_phase(room) == FaultPhase.CLEAR
