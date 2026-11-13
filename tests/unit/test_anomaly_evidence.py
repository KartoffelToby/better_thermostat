"""Unit tests for ``utils/anomaly_evidence.py``, follow-up scheduling, and C5 sign recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.desired import AnomalyCode, HeatingBlock
from custom_components.better_thermostat.core.fsm.anomaly import (
    AnomalyState,
    FaultPhase,
    FaultSlot,
    TrvFaultState,
)
from custom_components.better_thermostat.core.snapshot import (
    ThermalEvidence,
    TrvReported,
)
from custom_components.better_thermostat.model_fixes import COZB0001
from custom_components.better_thermostat.utils.anomaly_evidence import (
    DEFAULT_ARM_SAMPLES,
    DEFAULT_MIN_SIGMA_KELVIN_PER_MIN,
    UNREAD_ROOM_BLOCKS,
    AnomalyEvidenceTracker,
    compute_expected_passive_slope,
    compute_rc2_model_slope,
    compute_slope_residual,
)
from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_ANOMALIES,
    ATTR_STATE_TRV_ANOMALIES,
    CalibrationOutput,
)
from custom_components.better_thermostat.utils.controlling import (
    _schedule_anomaly_followup,
)
from custom_components.better_thermostat.utils.snapshot import build_snapshot
from tests.factories import (
    ThermostatStandIn,
    make_snapshot,
    make_state,
    make_state_attributes_bt,
    trv_record,
)


class TestPureSlopeHelpers:
    """Pure slope and residual helper functions."""

    def test_expected_passive_slope_math_and_missing_inputs(self):
        """Passive cooling slope scales with positive indoor-outdoor delta and returns None when inputs are missing."""
        assert compute_expected_passive_slope(
            room_temperature=21.0,
            outdoor_temperature=1.0,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.03,
        ) == pytest.approx(-0.01)

        # Outdoor warmer than room clamps passive cooling delta at 0.0.
        assert compute_expected_passive_slope(
            room_temperature=20.0,
            outdoor_temperature=25.0,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.03,
        ) == pytest.approx(0.0)

        for kwargs in (
            {
                "room_temperature": None,
                "outdoor_temperature": 5.0,
                "heat_loss_rate_kelvin_per_hour_per_kelvin": 0.02,
            },
            {
                "room_temperature": 20.0,
                "outdoor_temperature": None,
                "heat_loss_rate_kelvin_per_hour_per_kelvin": 0.02,
            },
            {
                "room_temperature": 20.0,
                "outdoor_temperature": 5.0,
                "heat_loss_rate_kelvin_per_hour_per_kelvin": None,
            },
            {
                "room_temperature": 20.0,
                "outdoor_temperature": 5.0,
                "heat_loss_rate_kelvin_per_hour_per_kelvin": 0.0,
            },
        ):
            assert compute_expected_passive_slope(**kwargs) is None

    def test_rc2_model_slope_and_slope_residual(self):
        """2R2C slope combines radiator coupling, envelope loss, and baseline disturbance."""
        slope = compute_rc2_model_slope(
            room_temperature_hat=20.0,
            radiator_temperature_hat=30.0,
            outdoor_temperature=0.0,
            disturbance_baseline_kelvin_per_min=0.002,
            k_rad_room_per_min=0.005,
            k_loss_per_min=0.001,
        )
        # 0.005 * 10.0 - 0.001 * 20.0 + 0.002 = 0.05 - 0.02 + 0.002 = 0.032
        assert slope == pytest.approx(0.032)

        slope_no_outdoor = compute_rc2_model_slope(
            room_temperature_hat=20.0,
            radiator_temperature_hat=30.0,
            outdoor_temperature=None,
            disturbance_baseline_kelvin_per_min=0.0,
            k_rad_room_per_min=0.005,
            k_loss_per_min=0.001,
        )
        assert slope_no_outdoor == pytest.approx(0.05)

        assert (
            compute_rc2_model_slope(
                room_temperature_hat=None,
                radiator_temperature_hat=30.0,
                outdoor_temperature=0.0,
                disturbance_baseline_kelvin_per_min=0.0,
                k_rad_room_per_min=0.005,
                k_loss_per_min=0.001,
            )
            is None
        )

        assert compute_slope_residual(
            observed_slope_kelvin_per_min=-0.08, expected_slope_kelvin_per_min=-0.01
        ) == pytest.approx(-0.07)
        assert (
            compute_slope_residual(
                observed_slope_kelvin_per_min=None, expected_slope_kelvin_per_min=-0.01
            )
            is None
        )


class TestAnomalyEvidenceTracker:
    """Rolling residual statistics, observer caching, TRV slopes, and diagnostics export."""

    def test_sigma_arms_after_warmup_samples_and_freezes_when_held_or_anomalous(self):
        """Residual sigma stays None until warmup samples commit and freezes under holds or anomalies."""
        tracker = AnomalyEvidenceTracker()
        for step in range(DEFAULT_ARM_SAMPLES):
            t_mono = float((step + 1) * 60)
            alt_slope = -0.01 + (0.008 if step % 2 == 0 else -0.008)
            evidence, _ = tracker.observe_snapshot(
                now_monotonic=t_mono,
                room_temperature=20.0,
                room_temperature_filtered=20.0,
                outdoor_temperature=0.0,
                temperature_slope_kelvin_per_min=alt_slope,
                heat_loss_rate_kelvin_per_hour_per_kelvin=0.03,
                trv_raw_temperatures={},
            )
            if step < DEFAULT_ARM_SAMPLES - 1:
                assert evidence.slope_residual_sigma_kelvin_per_min is None
            snap = make_snapshot(
                now_monotonic=t_mono,
                room_temperature=20.0,
                temperature_slope=alt_slope,
                thermal_evidence=evidence,
            )
            tracker.commit_cycle(snap, AnomalyState())

        assert tracker.residual_samples == DEFAULT_ARM_SAMPLES
        armed_sigma = tracker.residual_sigma_kelvin_per_min
        assert armed_sigma is not None
        assert armed_sigma >= DEFAULT_MIN_SIGMA_KELVIN_PER_MIN

        # Calling observe_snapshot repeatedly without commit_cycle does not advance samples.
        tracker.observe_snapshot(
            now_monotonic=3000.0,
            room_temperature=20.0,
            room_temperature_filtered=20.0,
            outdoor_temperature=0.0,
            temperature_slope_kelvin_per_min=-0.25,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.03,
            trv_raw_temperatures={},
        )
        assert tracker.residual_samples == DEFAULT_ARM_SAMPLES

        # Extreme residual committed while learning_held=True or anomaly is SUSPECT leaves sigma frozen.
        extreme_snap = make_snapshot(
            now_monotonic=3060.0,
            room_temperature=18.5,
            temperature_slope=-0.25,
            thermal_evidence=evidence,
        )
        tracker.commit_cycle(extreme_snap, AnomalyState(), learning_held=True)
        assert tracker.residual_samples == DEFAULT_ARM_SAMPLES
        assert tracker.residual_sigma_kelvin_per_min == pytest.approx(armed_sigma)

        suspect_state = AnomalyState(
            rapid_loss=FaultSlot(phase=FaultPhase.SUSPECT, since=3000.0)
        )
        tracker.commit_cycle(extreme_snap, suspect_state, learning_held=False)
        assert tracker.residual_samples == DEFAULT_ARM_SAMPLES
        assert tracker.residual_sigma_kelvin_per_min == pytest.approx(armed_sigma)

    def test_observer_innovation_hold_and_baseline_ema_freeze(self):
        """Observer innovation holds across quantized sensor plateaus and resets on contact_open."""
        tracker = AnomalyEvidenceTracker()
        tracker.record_mpc_v2_diagnostics(
            now_monotonic=1000.0,
            room_temperature_hat=20.0,
            radiator_temperature_hat=25.0,
            disturbance_kelvin_per_min=-0.01,
            disturbance_at_bound=False,
            normalised_innovation=-4.0,
            room_time_constant_minutes=80.0,
            k_rad_room_per_min=0.005,
            k_loss_per_min=0.001,
            learning_held=False,
            room_temperature=19.8,
            contact_open=False,
        )
        baseline_after_first = tracker.disturbance_baseline_kelvin_per_min
        assert baseline_after_first < 0.0

        # Same quantized room_temperature with near-zero raw innovation holds/decays the negative innovation.
        tracker.record_mpc_v2_diagnostics(
            now_monotonic=1060.0,
            room_temperature_hat=19.8,
            radiator_temperature_hat=25.0,
            disturbance_kelvin_per_min=-0.01,
            disturbance_at_bound=False,
            normalised_innovation=0.02,
            room_time_constant_minutes=80.0,
            k_rad_room_per_min=0.005,
            k_loss_per_min=0.001,
            learning_held=True,
            room_temperature=19.8,
            contact_open=False,
        )
        # Baseline is frozen while learning_held=True.
        assert tracker.disturbance_baseline_kelvin_per_min == pytest.approx(
            baseline_after_first
        )
        ev_held, _ = tracker.observe_snapshot(
            now_monotonic=1060.0,
            room_temperature=19.8,
            room_temperature_filtered=19.8,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=-0.08,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={},
        )
        assert ev_held.observer is not None
        assert ev_held.observer.normalised_innovation < -3.0

        # Further room_temperature drop takes the steeper of raw and decayed innovation.
        tracker.record_mpc_v2_diagnostics(
            now_monotonic=1120.0,
            room_temperature_hat=19.6,
            radiator_temperature_hat=25.0,
            disturbance_kelvin_per_min=-0.012,
            disturbance_at_bound=True,
            normalised_innovation=-1.5,
            room_time_constant_minutes=80.0,
            k_rad_room_per_min=0.005,
            k_loss_per_min=0.001,
            learning_held=True,
            room_temperature=19.6,
            contact_open=False,
        )
        ev_drop, _ = tracker.observe_snapshot(
            now_monotonic=1120.0,
            room_temperature=19.6,
            room_temperature_filtered=19.6,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=-0.08,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={},
        )
        assert ev_drop.observer is not None
        assert ev_drop.observer.normalised_innovation < -2.0

        # Contact opening zeroes the stored innovation immediately.
        tracker.record_mpc_v2_diagnostics(
            now_monotonic=1180.0,
            room_temperature_hat=19.4,
            radiator_temperature_hat=20.0,
            disturbance_kelvin_per_min=-0.012,
            disturbance_at_bound=True,
            normalised_innovation=-5.0,
            room_time_constant_minutes=80.0,
            k_rad_room_per_min=0.005,
            k_loss_per_min=0.001,
            learning_held=True,
            room_temperature=19.4,
            contact_open=True,
        )
        ev_contact, _ = tracker.observe_snapshot(
            now_monotonic=1180.0,
            room_temperature=19.4,
            room_temperature_filtered=19.4,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=-0.1,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={},
            contact_open=True,
        )
        assert ev_contact.observer is not None
        assert ev_contact.observer.normalised_innovation == 0.0

    def test_windowed_room_slope_and_post_contact_flush(self):
        """Committed 3-minute room temperature window computes lag-free slope and suppresses post-contact EMA lag."""
        tracker = AnomalyEvidenceTracker()
        for idx, temperature in enumerate((20.0, 20.0, 20.0, 19.6)):
            t_mono = float((idx + 1) * 60)
            ev, _ = tracker.observe_snapshot(
                now_monotonic=t_mono,
                room_temperature=temperature,
                room_temperature_filtered=temperature,
                outdoor_temperature=5.0,
                temperature_slope_kelvin_per_min=-0.01,
                heat_loss_rate_kelvin_per_hour_per_kelvin=None,
                trv_raw_temperatures={},
            )
            tracker.commit_cycle(
                make_snapshot(
                    now_monotonic=t_mono,
                    room_temperature=temperature,
                    thermal_evidence=ev,
                ),
                AnomalyState(),
            )
        # At t=240s, window spans t=60s (20.0) to t=240s (19.6): -0.4 K / 3 min = -0.1333 K/min.
        assert ev.slope_residual_kelvin_per_min == pytest.approx(-0.4 / 3.0, abs=0.01)

        # Rising room temperature reports positive slope immediately.
        ev_rise, _ = tracker.observe_snapshot(
            now_monotonic=300.0,
            room_temperature=19.8,
            room_temperature_filtered=19.8,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=-0.08,
            heat_loss_rate_kelvin_per_hour_per_kelvin=None,
            trv_raw_temperatures={},
        )
        assert ev_rise.slope_residual_kelvin_per_min is not None
        assert ev_rise.slope_residual_kelvin_per_min > 0.0

        # Contact open clears the window and activates post-contact flush so stale EMA (-0.09) is ignored.
        tracker.commit_cycle(
            make_snapshot(now_monotonic=360.0, room_temperature=18.0),
            AnomalyState(),
            heating_blocked_by=HeatingBlock.WINDOW,
            contact_open=True,
        )
        ev_flush1, _ = tracker.observe_snapshot(
            now_monotonic=420.0,
            room_temperature=18.0,
            room_temperature_filtered=18.5,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=-0.09,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={},
            contact_open=False,
        )
        assert ev_flush1.slope_residual_kelvin_per_min == pytest.approx(
            0.00433, abs=0.01
        )
        tracker.commit_cycle(
            make_snapshot(
                now_monotonic=420.0, room_temperature=18.0, thermal_evidence=ev_flush1
            ),
            AnomalyState(),
        )
        ev_flush2, _ = tracker.observe_snapshot(
            now_monotonic=480.0,
            room_temperature=18.0,
            room_temperature_filtered=18.4,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=-0.09,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={},
            contact_open=False,
        )
        assert ev_flush2.slope_residual_kelvin_per_min == pytest.approx(
            0.00446, abs=0.01
        )

    def test_per_trv_raw_slope_and_export(self):
        """Per-TRV raw slope requires a baseline interval, smooths via EMA, resets across long gaps, and exports cleanly."""
        tracker = AnomalyEvidenceTracker()
        _, slopes0 = tracker.observe_snapshot(
            now_monotonic=100.0,
            room_temperature=20.0,
            room_temperature_filtered=20.0,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=0.0,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={"climate.trv1": 20.0, "climate.trv2": None},
        )
        assert slopes0 == {"climate.trv1": None, "climate.trv2": None}
        tracker.commit_cycle(
            make_snapshot(
                now_monotonic=100.0,
                trvs={
                    "climate.trv1": TrvReported(
                        entity_id="climate.trv1", raw_temperature=20.0
                    ),
                    "climate.trv2": TrvReported(
                        entity_id="climate.trv2", raw_temperature=None
                    ),
                },
            ),
            AnomalyState(),
        )

        # dt < 30s returns previous EMA (None on second sample).
        _, slopes_short = tracker.observe_snapshot(
            now_monotonic=115.0,
            room_temperature=20.0,
            room_temperature_filtered=20.0,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=0.0,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={"climate.trv1": 20.5},
        )
        assert slopes_short["climate.trv1"] is None

        # dt = 60s computes instantaneous slope (+0.6 K / 1 min = +0.6 K/min).
        _, slopes_60 = tracker.observe_snapshot(
            now_monotonic=160.0,
            room_temperature=20.0,
            room_temperature_filtered=20.0,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=0.0,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={"climate.trv1": 20.6},
        )
        assert slopes_60["climate.trv1"] == pytest.approx(0.6)
        tracker.commit_cycle(
            make_snapshot(
                now_monotonic=160.0,
                trvs={
                    "climate.trv1": TrvReported(
                        entity_id="climate.trv1",
                        raw_temperature=20.6,
                        raw_temperature_slope_kelvin_per_min=slopes_60["climate.trv1"],
                    )
                },
            ),
            AnomalyState(),
        )

        # Subsequent step at t=220s blends via EMA.
        _, slopes_120 = tracker.observe_snapshot(
            now_monotonic=220.0,
            room_temperature=20.0,
            room_temperature_filtered=20.0,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=0.0,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={"climate.trv1": 20.6},
        )
        assert slopes_120["climate.trv1"] is not None
        assert 0.0 < slopes_120["climate.trv1"] < 0.6

        # Gap > 1800s resets slope to None.
        _, slopes_gap = tracker.observe_snapshot(
            now_monotonic=3000.0,
            room_temperature=20.0,
            room_temperature_filtered=20.0,
            outdoor_temperature=5.0,
            temperature_slope_kelvin_per_min=0.0,
            heat_loss_rate_kelvin_per_hour_per_kelvin=0.02,
            trv_raw_temperatures={"climate.trv1": 22.0},
        )
        assert slopes_gap["climate.trv1"] is None
        active_state = AnomalyState(
            rapid_loss=FaultSlot(phase=FaultPhase.ACTIVE, active_since=2900.0),
            trvs={
                "climate.trv1": TrvFaultState(
                    valve_stuck_open=FaultSlot(
                        phase=FaultPhase.ACTIVE, active_since=2800.0
                    )
                )
            },
        )
        tracker.commit_cycle(
            make_snapshot(
                now_monotonic=3000.0,
                trvs={
                    "climate.trv1": TrvReported(
                        entity_id="climate.trv1", raw_temperature=22.0
                    )
                },
            ),
            active_state,
            anomaly_detection=True,
        )
        exported = tracker.export()
        assert exported["enabled"] is True
        assert exported["visible_anomalies"] == [
            AnomalyCode.UNEXPECTED_RAPID_LOSS.value,
            AnomalyCode.VALVE_STUCK_OPEN.value,
        ]
        assert exported["trv_anomalies"] == {
            "climate.trv1": [AnomalyCode.VALVE_STUCK_OPEN.value]
        }
        assert exported["gating_anomalies"] == [
            AnomalyCode.UNEXPECTED_RAPID_LOSS.value,
            AnomalyCode.VALVE_STUCK_OPEN.value,
        ]
        assert exported["next_evaluation_monotonic"] == 6500.0


def _observe_falling_room(
    tracker: AnomalyEvidenceTracker, minute: int, *, contact_open: bool = False
) -> ThermalEvidence:
    """Read a room that falls by 0.1 K a minute, at the given minute."""
    evidence, _ = tracker.observe_snapshot(
        now_monotonic=minute * 60.0,
        room_temperature=20.0 - 0.1 * minute,
        room_temperature_filtered=20.0 - 0.1 * minute,
        outdoor_temperature=5.0,
        temperature_slope_kelvin_per_min=-0.1,
        heat_loss_rate_kelvin_per_hour_per_kelvin=None,
        trv_raw_temperatures={},
        contact_open=contact_open,
    )
    return evidence


def _commit_falling_room(
    tracker: AnomalyEvidenceTracker,
    minutes: range,
    block: HeatingBlock | None,
    *,
    contact_open: bool = False,
) -> None:
    """Commit one cycle a minute of the falling room under ``block``."""
    for minute in minutes:
        evidence = _observe_falling_room(tracker, minute, contact_open=contact_open)
        tracker.commit_cycle(
            make_snapshot(
                now_monotonic=minute * 60.0,
                room_temperature=20.0 - 0.1 * minute,
                thermal_evidence=evidence,
            ),
            AnomalyState(),
            heating_blocked_by=block,
            contact_open=contact_open,
        )


class TestRoomSlopeUnderAHeatingBlock:
    """Which heating blocks leave the room's slope readable."""

    @pytest.mark.parametrize(
        "block",
        [
            HeatingBlock.SUSPECTED_WINDOW,
            HeatingBlock.HVAC_OFF,
            HeatingBlock.NO_CALL_FOR_HEAT,
        ],
    )
    def test_a_block_that_only_shuts_the_valves_leaves_the_slope_read(self, block):
        """The room cools the same whether the valves are shut or not."""
        tracker = AnomalyEvidenceTracker()
        _commit_falling_room(tracker, range(1, 5), None)
        _commit_falling_room(tracker, range(5, 12), block)

        evidence = _observe_falling_room(tracker, 12)

        assert evidence.slope_measured
        assert evidence.slope_residual_kelvin_per_min == pytest.approx(-0.1)

    @pytest.mark.parametrize("block", sorted(UNREAD_ROOM_BLOCKS))
    def test_a_room_out_of_control_gets_a_zero_slope_marked_as_not_measured(
        self, block
    ):
        tracker = AnomalyEvidenceTracker()
        _commit_falling_room(tracker, range(1, 5), None)
        _commit_falling_room(tracker, range(5, 8), block)

        evidence = _observe_falling_room(tracker, 8)

        assert not evidence.slope_measured
        assert evidence.slope_residual_kelvin_per_min == 0.0

    def test_the_slope_is_measured_again_once_the_flush_has_passed(self):
        tracker = AnomalyEvidenceTracker()
        _commit_falling_room(
            tracker, range(1, 4), HeatingBlock.WINDOW, contact_open=True
        )
        assert not _observe_falling_room(tracker, 3, contact_open=True).slope_measured

        _commit_falling_room(tracker, range(4, 9), None)
        # Five minutes on from the last cycle under the open contact.
        assert not _observe_falling_room(tracker, 8).slope_measured

        _commit_falling_room(tracker, range(9, 13), None)
        evidence = _observe_falling_room(tracker, 13)
        assert evidence.slope_measured
        assert evidence.slope_residual_kelvin_per_min == pytest.approx(-0.1)


class TestFollowUpSchedulingAndAttributes:
    """Follow-up timer coalescing, replacement, cancellation, and state attributes."""

    @pytest.mark.asyncio
    async def test_followup_schedules_coalesces_replaces_and_fires(self):
        """Follow-up timer coalesces later deadlines, replaces with earlier ones, cancels when clear, and fires a cycle."""
        bt = ThermostatStandIn()
        bt.device_name = "Test BT"
        bt.hass = MagicMock(loop=asyncio.get_running_loop())

        # Suspect slot due at 1180.0 schedules a task.
        bt.kernel_state = make_state(
            anomaly_detection=True,
            anomaly=AnomalyState(
                rapid_loss=FaultSlot(phase=FaultPhase.SUSPECT, since=1000.0)
            ),
        )
        _schedule_anomaly_followup(bt, 1000.0)
        first_task = bt._anomaly_followup_task
        assert first_task is not None
        assert bt._anomaly_followup_due == 1180.0

        # Same or later deadline keeps the existing task.
        _schedule_anomaly_followup(bt, 1010.0)
        assert bt._anomaly_followup_task is first_task

        # Earlier deadline cancels the existing task and schedules a new one.
        bt.kernel_state = make_state(
            anomaly_detection=True,
            anomaly=AnomalyState(
                rapid_loss=FaultSlot(phase=FaultPhase.SUSPECT, since=900.0)
            ),
        )
        _schedule_anomaly_followup(bt, 1079.95)
        second_task = bt._anomaly_followup_task
        assert second_task is not None and second_task is not first_task
        assert bt._anomaly_followup_due == 1080.0

        with patch(
            "custom_components.better_thermostat.utils.controlling.request_control_cycle"
        ) as mock_req:
            await second_task
        mock_req.assert_called_once_with(bt)
        assert bt._anomaly_followup_task is None
        assert bt._anomaly_followup_due is None

        # Clearing anomalies cancels any pending follow-up task.
        bt.kernel_state = make_state(
            anomaly_detection=True,
            anomaly=AnomalyState(
                rapid_loss=FaultSlot(phase=FaultPhase.SUSPECT, since=2000.0)
            ),
        )
        _schedule_anomaly_followup(bt, 2000.0)
        assert bt._anomaly_followup_task is not None
        bt.kernel_state = make_state(anomaly_detection=True, anomaly=AnomalyState())
        _schedule_anomaly_followup(bt, 2010.0)
        assert bt._anomaly_followup_task is None
        assert bt._anomaly_followup_due is None

    def test_extra_state_attributes_publishes_anomalies_and_trv_anomalies(self):
        """Climate extra_state_attributes publishes room anomalies and optional trv_anomalies."""
        bt = make_state_attributes_bt()
        from custom_components.better_thermostat.climate import BetterThermostat

        assert BetterThermostat.extra_state_attributes.fget is not None
        attrs_default = BetterThermostat.extra_state_attributes.fget(bt)
        assert attrs_default[ATTR_STATE_ANOMALIES] == []
        assert ATTR_STATE_TRV_ANOMALIES not in attrs_default

        bt.kernel_state = make_state(
            anomaly_detection=True,
            anomaly=AnomalyState(
                no_heat_delivery=FaultSlot(
                    phase=FaultPhase.ACTIVE, active_since=1000.0
                ),
                trvs={
                    "climate.trv": TrvFaultState(
                        no_heat_delivery=FaultSlot(
                            phase=FaultPhase.ACTIVE, active_since=1000.0
                        )
                    )
                },
            ),
        )
        attrs_active = BetterThermostat.extra_state_attributes.fget(bt)
        assert attrs_active[ATTR_STATE_ANOMALIES] == [
            AnomalyCode.NO_HEAT_DELIVERY.value
        ]
        assert attrs_active[ATTR_STATE_TRV_ANOMALIES] == {
            "climate.trv": [AnomalyCode.NO_HEAT_DELIVERY.value]
        }


class TestC5RawTrvTemperatureSignRecovery:
    """C5 sign check across calibration channels and local_calibration_shifts_setpoint quirk."""

    def _bt_with_trv(self, calibration_output: CalibrationOutput) -> ThermostatStandIn:
        bt = ThermostatStandIn()
        bt.window_sensor_entity_id = None
        bt.device_name = "Test BT"
        bt.clock = FakeClock(
            monotonic_value=1000.0, now_value=datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
        )
        bt.heat_target_temperature = 21.0
        bt.cool_target_temperature = None
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.room_temperature = 20.0
        bt.room_temperature_filtered = 20.0
        bt.temperature_slope = 0.0
        bt.call_for_heat = True
        bt.devices_released = False
        bt.preset_mode = None
        bt.tolerance = 0.0
        bt.outdoor_sensor_entity_id = None
        bt.weather_entity_id = None
        bt.bt_min_temp = 5.0
        bt.bt_max_temp = 30.0
        bt.real_trvs = {
            "climate.trv": trv_record(
                entity_id="climate.trv",
                hvac_mode="heat",
                current_temperature=20.0,
                commanded_setpoint=21.0,
                calibration=calibration_output,
                last_calibration=-4.5,
            )
        }
        bt.hass.states.get.return_value = State("climate.trv", "heat")
        return bt

    def test_c5_raw_temperature_recovers_physical_sensor_across_channels(self):
        """LOCAL_BASED subtracts negative offset to recover hot TRV body; setpoint/valve/COZB0001 read current_temperature."""
        # Standard LOCAL_BASED: physical TRV sensor is 24.5 C, offset is -4.5 K -> reported current_temperature is 20.0 C.
        # _raw_trv_temperature recovers 20.0 - (-4.5) = 24.5 C.
        bt_local = self._bt_with_trv(CalibrationOutput.LOCAL_BASED)
        snap_local = build_snapshot(bt_local)
        assert snap_local.trvs["climate.trv"].raw_temperature == pytest.approx(24.5)

        # COZB0001 quirk: local_calibration_shifts_setpoint is True, so current_temperature is already unshifted (24.5 C).
        bt_quirk = self._bt_with_trv(CalibrationOutput.LOCAL_BASED)
        bt_quirk.real_trvs["climate.trv"].model_quirks = COZB0001
        bt_quirk.real_trvs["climate.trv"].current_temperature = 24.5
        snap_quirk = build_snapshot(bt_quirk)
        assert snap_quirk.trvs["climate.trv"].raw_temperature == pytest.approx(24.5)

        # TARGET_TEMP_BASED and DIRECT_VALVE_BASED do not shift local_temperature: current_temperature is physical.
        for channel in (
            CalibrationOutput.TARGET_TEMP_BASED,
            CalibrationOutput.DIRECT_VALVE_BASED,
        ):
            bt_chan = self._bt_with_trv(channel)
            bt_chan.real_trvs["climate.trv"].current_temperature = 24.5
            snap_chan = build_snapshot(bt_chan)
            assert snap_chan.trvs["climate.trv"].raw_temperature == pytest.approx(24.5)
