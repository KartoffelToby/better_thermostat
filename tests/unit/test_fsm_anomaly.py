"""Unit tests for the pure anomaly FSM region and its decide() integration."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.better_thermostat.core.decide import decide
from custom_components.better_thermostat.core.desired import AnomalyCode, HeatingBlock
from custom_components.better_thermostat.core.fsm import anomaly as an
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.core.fsm.lifecycle import LifecycleState
from custom_components.better_thermostat.core.fsm.maintenance import (
    DUE_AT_FIRST_TICK,
    MaintenanceRunning,
)
from custom_components.better_thermostat.core.fsm.mode import ModeState
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    ObserverReading,
    ThermalEvidence,
    TrvReported,
)
from tests.factories import make_snapshot, make_state


def _observer(**overrides) -> ObserverReading:
    defaults = {
        "age_seconds": 30.0,
        "normalised_innovation": 0.0,
        "disturbance_kelvin_per_min": 0.0,
        "disturbance_at_bound": False,
        "room_time_constant_minutes": 90.0,
        "disturbance_baseline_kelvin_per_min": 0.0,
    }
    defaults.update(overrides)
    return ObserverReading(**defaults)


class TestFaultSlotLifecycle:
    """Transitions, dwell boundaries, recovery hysteresis, and liveness."""

    def test_clear_to_suspect_and_dwell_boundary(self):
        """Onset commits at the full dwell boundary and resets on early drop."""
        s0 = an.FaultSlot()
        assert (
            an.step_slot(s0, False, 100.0, dwell_seconds=180.0, recovery_seconds=300.0)
            == s0
        )

        s1 = an.step_slot(s0, True, 100.0, dwell_seconds=180.0, recovery_seconds=300.0)
        assert s1 == an.FaultSlot(phase=an.FaultPhase.SUSPECT, since=100.0)
        assert s1.is_visible is False
        assert s1.is_gating(100.0, 3600.0) is False

        # One tenth short of the dwell stays in SUSPECT.
        s2 = an.step_slot(s1, True, 279.9, dwell_seconds=180.0, recovery_seconds=300.0)
        assert s2 == s1

        # Dropping evidence before dwell elapses clears immediately.
        assert (
            an.step_slot(s2, False, 279.9, dwell_seconds=180.0, recovery_seconds=300.0)
            == an.FaultSlot()
        )

        # Reaching the dwell boundary commits ACTIVE.
        s3 = an.step_slot(s1, True, 280.0, dwell_seconds=180.0, recovery_seconds=300.0)
        assert s3 == an.FaultSlot(phase=an.FaultPhase.ACTIVE, active_since=280.0)
        assert s3.is_visible is True
        assert s3.is_gating(280.0, 3600.0) is True
        assert s3.is_gating(280.0, None) is True

    def test_zero_dwell_and_zero_recovery_commit_immediately(self):
        """Zero-second dwell or recovery skips intermediate pending phases."""
        active = an.step_slot(
            an.FaultSlot(), True, 50.0, dwell_seconds=0.0, recovery_seconds=0.0
        )
        assert active == an.FaultSlot(phase=an.FaultPhase.ACTIVE, active_since=50.0)
        cleared = an.step_slot(
            active, False, 55.0, dwell_seconds=0.0, recovery_seconds=0.0
        )
        assert cleared == an.FaultSlot()

        expired = an.FaultSlot(phase=an.FaultPhase.EXPIRED, active_since=10.0)
        assert (
            an.step_slot(expired, False, 60.0, dwell_seconds=10.0, recovery_seconds=0.0)
            == an.FaultSlot()
        )

    def test_active_recovery_and_relapse_keep_initial_active_since(self):
        """Relapsing out of RECOVERING preserves the original active_since."""
        active = an.FaultSlot(phase=an.FaultPhase.ACTIVE, active_since=100.0)
        assert (
            an.step_slot(
                active, True, 200.0, dwell_seconds=180.0, recovery_seconds=300.0
            )
            == active
        )

        rec = an.step_slot(
            active, False, 200.0, dwell_seconds=180.0, recovery_seconds=300.0
        )
        assert rec == an.FaultSlot(
            phase=an.FaultPhase.RECOVERING, since=200.0, active_since=100.0
        )
        assert rec.is_gating(250.0, 3600.0) is True

        # Still within recovery window.
        assert (
            an.step_slot(rec, False, 499.9, dwell_seconds=180.0, recovery_seconds=300.0)
            == rec
        )

        # Evidence returning restores ACTIVE without shifting active_since.
        relapsed = an.step_slot(
            rec, True, 450.0, dwell_seconds=180.0, recovery_seconds=300.0
        )
        assert relapsed == an.FaultSlot(phase=an.FaultPhase.ACTIVE, active_since=100.0)

        # Full recovery clears.
        cleared = an.step_slot(
            rec, False, 500.0, dwell_seconds=180.0, recovery_seconds=300.0
        )
        assert cleared == an.FaultSlot()

    def test_liveness_demotes_to_expired_and_requires_recovery_dwell(self):
        """Reaching the liveness cap stops gating while preserving visibility."""
        active = an.FaultSlot(phase=an.FaultPhase.ACTIVE, active_since=100.0)
        still_active = an.step_slot(
            active,
            True,
            3699.9,
            dwell_seconds=180.0,
            recovery_seconds=300.0,
            liveness_seconds=3600.0,
        )
        assert still_active == active

        expired = an.step_slot(
            active,
            True,
            3700.0,
            dwell_seconds=180.0,
            recovery_seconds=300.0,
            liveness_seconds=3600.0,
        )
        assert expired == an.FaultSlot(phase=an.FaultPhase.EXPIRED, active_since=100.0)
        assert expired.is_visible is True
        assert expired.is_gating(3700.0, 3600.0) is False

        # Brief drop in EXPIRED starts recovery countdown rather than clearing on noise.
        exp_rec = an.step_slot(
            expired,
            False,
            3800.0,
            dwell_seconds=180.0,
            recovery_seconds=300.0,
            liveness_seconds=3600.0,
        )
        assert exp_rec == an.FaultSlot(
            phase=an.FaultPhase.EXPIRED, since=3800.0, active_since=100.0
        )
        assert (
            an.step_slot(
                exp_rec,
                False,
                4099.0,
                dwell_seconds=180.0,
                recovery_seconds=300.0,
                liveness_seconds=3600.0,
            )
            == exp_rec
        )

        # Evidence returning resets the recovery countdown inside EXPIRED.
        assert (
            an.step_slot(
                exp_rec,
                True,
                4050.0,
                dwell_seconds=180.0,
                recovery_seconds=300.0,
                liveness_seconds=3600.0,
            )
            == expired
        )

        # Completing recovery dwell returns EXPIRED to CLEAR.
        assert (
            an.step_slot(
                exp_rec,
                False,
                4100.0,
                dwell_seconds=180.0,
                recovery_seconds=300.0,
                liveness_seconds=3600.0,
            )
            == an.FaultSlot()
        )

    def test_liveness_reached_without_evidence_clears_directly(self):
        """Hitting liveness while evidence is off clears ACTIVE and RECOVERING."""
        active = an.FaultSlot(phase=an.FaultPhase.ACTIVE, active_since=100.0)
        assert (
            an.step_slot(
                active,
                False,
                3700.0,
                dwell_seconds=180.0,
                recovery_seconds=300.0,
                liveness_seconds=3600.0,
            )
            == an.FaultSlot()
        )

        rec = an.FaultSlot(
            phase=an.FaultPhase.RECOVERING, since=3600.0, active_since=100.0
        )
        assert (
            an.step_slot(
                rec,
                False,
                3700.0,
                dwell_seconds=180.0,
                recovery_seconds=300.0,
                liveness_seconds=3600.0,
            )
            == an.FaultSlot()
        )
        assert an.step_slot(
            rec,
            True,
            3700.0,
            dwell_seconds=180.0,
            recovery_seconds=300.0,
            liveness_seconds=3600.0,
        ) == an.FaultSlot(phase=an.FaultPhase.EXPIRED, active_since=100.0)

    def test_malformed_timestamp_less_slots_reset_safely(self):
        """Imported slots missing their required timestamps reset to CLEAR."""
        for malformed in (
            an.FaultSlot(phase=an.FaultPhase.SUSPECT, since=None),
            an.FaultSlot(phase=an.FaultPhase.ACTIVE, active_since=None),
            an.FaultSlot(phase=an.FaultPhase.RECOVERING, since=None, active_since=1.0),
            an.FaultSlot(phase=an.FaultPhase.EXPIRED, active_since=None),
        ):
            assert (
                an.step_slot(
                    malformed, True, 100.0, dwell_seconds=10.0, recovery_seconds=10.0
                )
                == an.FaultSlot()
            )


class TestUnexpectedRapidLoss:
    """Rapid-loss detection via innovation or residual and contact-open reset."""

    def test_innovation_path_and_contact_open_forced_clear(self):
        """Negative Kalman innovation plus steep drop triggers rapid loss until contact opens."""
        snap = make_snapshot(
            now_monotonic=1000.0,
            temperature_slope=-0.08,
            thermal_evidence=ThermalEvidence(
                observer=_observer(normalised_innovation=-3.5)
            ),
        )
        s1 = an.step(
            an.AnomalyState(),
            snap,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s1.rapid_loss.phase == an.FaultPhase.SUSPECT

        s2 = an.step(
            s1,
            replace(snap, now_monotonic=1180.0),
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s2.rapid_loss.phase == an.FaultPhase.ACTIVE
        assert an.visible_codes(s2) == ((AnomalyCode.UNEXPECTED_RAPID_LOSS,), {})
        assert AnomalyCode.UNEXPECTED_RAPID_LOSS in an.gating_codes(s2, 1180.0)

        # Physical contact opening resets immediately.
        s3 = an.step(
            s2,
            replace(snap, now_monotonic=1200.0),
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=HeatingBlock.WINDOW,
            contact_open=True,
            addressed_trvs=("climate.trv1",),
        )
        assert s3.rapid_loss == an.FaultSlot()

    def test_model_free_residual_path_and_exclusions(self):
        """Slope residual triggers without an observer; degraded mode and stale observers do not."""
        snap_residual = make_snapshot(
            now_monotonic=1000.0,
            temperature_slope=-0.06,
            thermal_evidence=ThermalEvidence(
                slope_residual_kelvin_per_min=-0.05,
                slope_residual_sigma_kelvin_per_min=0.01,
                observer=_observer(age_seconds=900.0, normalised_innovation=-5.0),
            ),
        )
        s1 = an.step(
            an.AnomalyState(),
            snap_residual,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s1.rapid_loss.phase == an.FaultPhase.SUSPECT

        # Degraded control mode suppresses rapid-loss evidence.
        s_fallback = an.step(
            an.AnomalyState(),
            snap_residual,
            control_mode=ControlMode.SENSOR_FALLBACK,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s_fallback.rapid_loss == an.FaultSlot()

        # Gentle slope, setback cooldown, non-negative residual, or missing thermal evidence does not fire.
        for snap_quiet in (
            make_snapshot(now_monotonic=1000.0, temperature_slope=-0.01),
            make_snapshot(
                now_monotonic=1000.0,
                room_temperature=22.0,
                room_temperature_filtered=22.0,
                heat_target_temperature=20.0,
                temperature_slope=-0.08,
                thermal_evidence=ThermalEvidence(
                    observer=_observer(normalised_innovation=-4.0)
                ),
            ),
            make_snapshot(
                now_monotonic=1000.0,
                temperature_slope=-0.08,
                thermal_evidence=ThermalEvidence(
                    slope_residual_kelvin_per_min=0.0,
                    observer=_observer(normalised_innovation=-4.0),
                ),
            ),
            make_snapshot(
                now_monotonic=1000.0, temperature_slope=-0.08, thermal_evidence=None
            ),
            make_snapshot(
                now_monotonic=1000.0,
                temperature_slope=-0.08,
                thermal_evidence=ThermalEvidence(
                    slope_residual_kelvin_per_min=-0.01,
                    slope_residual_sigma_kelvin_per_min=0.01,
                ),
            ),
        ):
            assert (
                an.step(
                    an.AnomalyState(),
                    snap_quiet,
                    control_mode=ControlMode.OPTIMAL,
                    heating_blocked_by=None,
                    contact_open=False,
                    addressed_trvs=("climate.trv1",),
                ).rapid_loss
                == an.FaultSlot()
            )

        # Unexplained drop from residual + expected passive slope bridges EMA lag even when temperature_slope is None.
        snap_lag_bridge = make_snapshot(
            now_monotonic=1000.0,
            temperature_slope=None,
            thermal_evidence=ThermalEvidence(
                expected_passive_slope_kelvin_per_min=-0.01,
                slope_residual_kelvin_per_min=-0.06,
                slope_residual_sigma_kelvin_per_min=0.01,
            ),
        )
        assert (
            an.step(
                an.AnomalyState(),
                snap_lag_bridge,
                control_mode=ControlMode.OPTIMAL,
                heating_blocked_by=None,
                contact_open=False,
                addressed_trvs=("climate.trv1",),
            ).rapid_loss.phase
            == an.FaultPhase.SUSPECT
        )


class TestSustainedParasiticLoss:
    """Parasitic-loss detection, rapid-loss cooldown, and frozen-D_hat recovery."""

    def test_detects_sustained_negative_disturbance_and_recovers_via_innovation_or_residual(
        self,
    ):
        """Sustained negative disturbance triggers parasitic loss; positive innovation or healed residual clears it."""
        snap = make_snapshot(
            now_monotonic=1000.0,
            temperature_slope=-0.01,
            thermal_evidence=ThermalEvidence(
                slope_residual_kelvin_per_min=-0.01,
                observer=_observer(disturbance_kelvin_per_min=-0.01),
            ),
        )
        s1 = an.step(
            an.AnomalyState(),
            snap,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s1.parasitic_loss.phase == an.FaultPhase.SUSPECT

        s2 = an.step(
            s1,
            replace(snap, now_monotonic=1900.0),
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s2.parasitic_loss.phase == an.FaultPhase.ACTIVE
        assert an.visible_codes(s2) == ((AnomalyCode.SUSTAINED_PARASITIC_LOSS,), {})
        assert AnomalyCode.SUSTAINED_PARASITIC_LOSS in an.gating_codes(s2, 1900.0)

        # Stays ACTIVE while innovation is neutral and slope residual remains negative.
        s_still = an.step(
            s2,
            replace(snap, now_monotonic=2000.0),
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s_still.parasitic_loss.phase == an.FaultPhase.ACTIVE

        # Positive innovation while D_hat stays frozen negative starts recovery.
        snap_innov_rec = replace(
            snap,
            now_monotonic=2100.0,
            thermal_evidence=ThermalEvidence(
                slope_residual_kelvin_per_min=-0.01,
                observer=_observer(
                    disturbance_kelvin_per_min=-0.01, normalised_innovation=0.45
                ),
            ),
        )
        s_rec_innov = an.step(
            s2,
            snap_innov_rec,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s_rec_innov.parasitic_loss.phase == an.FaultPhase.RECOVERING

        # Healed slope residual with neutral innovation also starts recovery.
        snap_res_rec = replace(
            snap,
            now_monotonic=2100.0,
            thermal_evidence=ThermalEvidence(
                slope_residual_kelvin_per_min=0.0,
                observer=_observer(
                    disturbance_at_bound=True,
                    disturbance_kelvin_per_min=-0.002,
                    normalised_innovation=0.0,
                ),
            ),
        )
        s_rec_res = an.step(
            s2,
            snap_res_rec,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s_rec_res.parasitic_loss.phase == an.FaultPhase.RECOVERING

    def test_blocked_during_rapid_loss_cooldown_and_neutral_disturbance(self):
        """Recent rapid-loss activity or neutral disturbance suppresses parasitic onset."""
        snap = make_snapshot(
            now_monotonic=1500.0,
            thermal_evidence=ThermalEvidence(
                observer=_observer(disturbance_kelvin_per_min=-0.01)
            ),
        )
        prior_rapid = an.AnomalyState(last_rapid_loss_monotonic=1000.0)
        s_cooled = an.step(
            prior_rapid,
            snap,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1",),
        )
        assert s_cooled.parasitic_loss == an.FaultSlot()

        snap_neutral = make_snapshot(
            now_monotonic=3000.0,
            thermal_evidence=ThermalEvidence(
                observer=_observer(disturbance_kelvin_per_min=-0.001)
            ),
        )
        assert (
            an.step(
                an.AnomalyState(),
                snap_neutral,
                control_mode=ControlMode.OPTIMAL,
                heating_blocked_by=None,
                contact_open=False,
                addressed_trvs=("climate.trv1",),
            ).parasitic_loss
            == an.FaultSlot()
        )

        snap_warming = make_snapshot(
            now_monotonic=3000.0,
            temperature_slope=0.015,
            thermal_evidence=ThermalEvidence(
                observer=_observer(disturbance_kelvin_per_min=-0.01)
            ),
        )
        assert (
            an.step(
                an.AnomalyState(),
                snap_warming,
                control_mode=ControlMode.OPTIMAL,
                heating_blocked_by=None,
                contact_open=False,
                addressed_trvs=("climate.trv1",),
            ).parasitic_loss
            == an.FaultSlot()
        )


class TestNoHeatDelivery:
    """Room-wide supply outage vs single-TRV failure when a sibling warms."""

    def test_room_wide_cold_trvs_trigger_room_no_heat_with_tau_scaled_dwell(self):
        """Cold TRVs under high demand trigger room-level NO_HEAT_DELIVERY after tau-scaled dwell."""
        snap = make_snapshot(
            now_monotonic=1000.0,
            heat_target_temperature=21.0,
            room_temperature=19.0,
            room_temperature_filtered=19.0,
            temperature_slope=-0.01,
            trvs={
                "climate.trv1": TrvReported(
                    entity_id="climate.trv1",
                    valve_percent=80.0,
                    raw_temperature=19.5,
                    raw_temperature_slope_kelvin_per_min=0.0,
                ),
                "climate.trv2": TrvReported(
                    entity_id="climate.trv2",
                    setpoint=23.0,
                    current_temperature=19.5,
                    raw_temperature=None,
                ),
            },
            thermal_evidence=ThermalEvidence(
                observer=_observer(room_time_constant_minutes=60.0)
            ),
        )
        # Dwell = 90 + 0.5 * 60 * 60 = 1890 s.
        assert an.no_heat_dwell_seconds(snap) == pytest.approx(1890.0)

        s1 = an.step(
            an.AnomalyState(),
            snap,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1", "climate.trv2"),
        )
        assert s1.no_heat_delivery.phase == an.FaultPhase.SUSPECT
        assert dict(s1.trvs) == {}

        s2 = an.step(
            s1,
            replace(snap, now_monotonic=2890.0),
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.trv1", "climate.trv2"),
        )
        assert s2.no_heat_delivery.phase == an.FaultPhase.ACTIVE
        assert an.visible_codes(s2) == ((AnomalyCode.NO_HEAT_DELIVERY,), {})
        assert AnomalyCode.NO_HEAT_DELIVERY in an.gating_codes(s2, 2890.0)

    def test_sibling_warming_isolates_per_trv_no_heat(self):
        """A warming or hot sibling isolates NO_HEAT_DELIVERY to the cold demanding TRV."""
        snap = make_snapshot(
            now_monotonic=1000.0,
            heat_target_temperature=21.0,
            room_temperature=19.0,
            temperature_slope=0.005,
            trvs={
                "climate.cold": TrvReported(
                    entity_id="climate.cold",
                    setpoint=23.0,
                    raw_temperature=19.2,
                    raw_temperature_slope_kelvin_per_min=0.0,
                ),
                "climate.warm": TrvReported(
                    entity_id="climate.warm",
                    valve_percent=70.0,
                    raw_temperature=21.0,
                    raw_temperature_slope_kelvin_per_min=0.06,
                ),
                "climate.hot_steady": TrvReported(
                    entity_id="climate.hot_steady",
                    valve_percent=70.0,
                    raw_temperature=28.0,
                    raw_temperature_slope_kelvin_per_min=0.0,
                ),
            },
        )
        # Default tau = 90 min -> dwell = 90 + 2700 = 2790 s.
        s1 = an.step(
            an.AnomalyState(),
            snap,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.cold", "climate.warm", "climate.hot_steady"),
        )
        assert s1.no_heat_delivery == an.FaultSlot()
        assert s1.trvs["climate.cold"].no_heat_delivery.phase == an.FaultPhase.SUSPECT
        assert "climate.warm" not in s1.trvs
        assert "climate.hot_steady" not in s1.trvs
        assert an.visible_codes(s1) == ((), {})

        s2 = an.step(
            s1,
            replace(snap, now_monotonic=3790.0),
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=None,
            contact_open=False,
            addressed_trvs=("climate.cold", "climate.warm", "climate.hot_steady"),
        )
        assert an.visible_codes(s2) == (
            (AnomalyCode.NO_HEAT_DELIVERY,),
            {"climate.cold": (AnomalyCode.NO_HEAT_DELIVERY,)},
        )
        assert AnomalyCode.NO_HEAT_DELIVERY in an.gating_codes(s2, 3790.0)

    def test_low_demand_or_blocked_heating_skips_no_heat(self):
        """Low valve demand or blocked heating keeps NO_HEAT_DELIVERY clear."""
        low_demand = make_snapshot(
            now_monotonic=1000.0,
            heat_target_temperature=21.0,
            room_temperature=19.0,
            temperature_slope=-0.01,
            trvs={
                "climate.trv1": TrvReported(
                    entity_id="climate.trv1",
                    valve_percent=10.0,
                    setpoint=20.0,
                    raw_temperature=19.5,
                )
            },
        )
        assert (
            an.step(
                an.AnomalyState(),
                low_demand,
                control_mode=ControlMode.OPTIMAL,
                heating_blocked_by=None,
                contact_open=False,
                addressed_trvs=("climate.trv1",),
            ).no_heat_delivery
            == an.FaultSlot()
        )


class TestValveStuckOpen:
    """Per-TRV stuck-open detection while heating is off, plus solar/maintenance exclusions."""

    def test_detects_hot_trv_while_suppressed_and_respects_exclusions(self):
        """A TRV staying hot above room while heating is off triggers VALVE_STUCK_OPEN unless sunny or in maintenance."""
        snap = make_snapshot(
            now_monotonic=1000.0,
            hvac_mode=HvacMode.OFF,
            call_for_heat=False,
            room_temperature=21.0,
            room_temperature_filtered=21.0,
            temperature_slope=0.01,
            is_day=False,
            solar_intensity=0.0,
            trvs={
                "climate.stuck": TrvReported(
                    entity_id="climate.stuck",
                    hvac_mode=HvacMode.OFF,
                    raw_temperature=25.5,
                    valve_percent=0.0,
                ),
                "climate.offline": TrvReported(
                    entity_id="climate.offline", available=False, raw_temperature=26.0
                ),
            },
        )
        s1 = an.step(
            an.AnomalyState(),
            snap,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=HeatingBlock.HVAC_OFF,
            contact_open=False,
            addressed_trvs=("climate.stuck",),
        )
        assert s1.trvs["climate.stuck"].valve_stuck_open.phase == an.FaultPhase.SUSPECT
        assert "climate.offline" not in s1.trvs

        s2 = an.step(
            s1,
            replace(
                snap,
                now_monotonic=2800.0,
                thermal_evidence=ThermalEvidence(slope_residual_kelvin_per_min=0.01),
            ),
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=HeatingBlock.HVAC_OFF,
            contact_open=False,
            addressed_trvs=("climate.stuck",),
        )
        assert s2.trvs["climate.stuck"].valve_stuck_open.phase == an.FaultPhase.ACTIVE
        assert an.visible_codes(s2) == (
            (AnomalyCode.VALVE_STUCK_OPEN,),
            {"climate.stuck": (AnomalyCode.VALVE_STUCK_OPEN,)},
        )
        assert AnomalyCode.VALVE_STUCK_OPEN in an.gating_codes(s2, 2800.0)

        # High daytime solar intensity suppresses the stuck-open signature.
        sunny = replace(snap, now_monotonic=2900.0, is_day=True, solar_intensity=0.8)
        s_sun = an.step(
            s2,
            sunny,
            control_mode=ControlMode.OPTIMAL,
            heating_blocked_by=HeatingBlock.HVAC_OFF,
            contact_open=False,
            addressed_trvs=("climate.stuck",),
        )
        assert (
            s_sun.trvs["climate.stuck"].valve_stuck_open.phase
            == an.FaultPhase.RECOVERING
        )

        # Valve maintenance, active heating, or cool radiator does not trigger.
        base_trv = snap.trvs["climate.stuck"]
        for blocked, trv in (
            (HeatingBlock.VALVE_MAINTENANCE, base_trv),
            (None, replace(base_trv, hvac_mode=HvacMode.HEAT, valve_percent=40.0)),
            (HeatingBlock.HVAC_OFF, replace(base_trv, raw_temperature=22.0)),
        ):
            test_snap = replace(snap, trvs={"climate.stuck": trv})
            res = an.step(
                an.AnomalyState(),
                test_snap,
                control_mode=ControlMode.OPTIMAL,
                heating_blocked_by=blocked,
                contact_open=False,
                addressed_trvs=("climate.stuck",),
            )
            assert "climate.stuck" not in res.trvs


class TestNextEvaluationAndDecideIntegration:
    """Follow-up deadline calculation and decide() cascade integration."""

    def test_next_evaluation_monotonic_picks_earliest_pending_timer(self):
        """next_evaluation_monotonic returns None when idle and earliest deadline when active."""
        assert an.next_evaluation_monotonic(an.AnomalyState()) is None

        state = an.AnomalyState(
            rapid_loss=an.FaultSlot(phase=an.FaultPhase.ACTIVE, active_since=100.0),
            parasitic_loss=an.FaultSlot(
                phase=an.FaultPhase.RECOVERING, since=200.0, active_since=50.0
            ),
            no_heat_delivery=an.FaultSlot(
                phase=an.FaultPhase.ACTIVE, active_since=80.0
            ),
            trvs={
                "climate.trv1": an.TrvFaultState(
                    no_heat_delivery=an.FaultSlot(
                        phase=an.FaultPhase.SUSPECT, since=300.0
                    ),
                    valve_stuck_open=an.FaultSlot(
                        phase=an.FaultPhase.EXPIRED, since=250.0, active_since=10.0
                    ),
                )
            },
        )
        # parasitic recovery = 200 + 900 = 1100; stuck expired recovery = 250 + 900 = 1150;
        # trv no_heat suspect (tau=40 -> dwell=90+1200=1290) = 300 + 1290 = 1590;
        # rapid liveness = 100 + 3600 = 3700.
        assert an.next_evaluation_monotonic(
            state, room_time_constant_minutes=40.0
        ) == pytest.approx(1100.0)

        # Unbounded RECOVERING slot (no_heat_delivery) uses recovery_at directly,
        # and steady EXPIRED (since=None) contributes no deadline.
        unbounded_rec = an.AnomalyState(
            no_heat_delivery=an.FaultSlot(
                phase=an.FaultPhase.RECOVERING, since=500.0, active_since=100.0
            ),
            trvs={
                "climate.trv1": an.TrvFaultState(
                    valve_stuck_open=an.FaultSlot(
                        phase=an.FaultPhase.EXPIRED, since=None, active_since=10.0
                    )
                )
            },
        )
        assert an.next_evaluation_monotonic(unbounded_rec) == pytest.approx(1100.0)

    def test_decide_steps_anomaly_when_opted_in_and_clears_when_opted_out(self):
        """decide() attaches visible anomalies across tiers when opted in and resets when opted out."""
        snap = make_snapshot(
            now_monotonic=1000.0,
            temperature_slope=-0.08,
            thermal_evidence=ThermalEvidence(
                observer=_observer(normalised_innovation=-4.0)
            ),
        )
        state_on = make_state(
            anomaly_detection=True,
            anomaly=an.AnomalyState(
                rapid_loss=an.FaultSlot(phase=an.FaultPhase.SUSPECT, since=800.0)
            ),
        )
        desired, next_state = decide(snap, state_on)
        assert desired.anomalies == (AnomalyCode.UNEXPECTED_RAPID_LOSS,)
        assert next_state.anomaly.rapid_loss.phase == an.FaultPhase.ACTIVE

        # Survives on non-heating tiers such as HVAC_OFF and NO_CALL_FOR_HEAT.
        off_desired, off_state = decide(
            replace(snap, now_monotonic=1010.0, hvac_mode=HvacMode.OFF),
            replace(next_state, mode=ModeState(hvac_mode=HvacMode.OFF)),
        )
        assert off_desired.heating_blocked_by == HeatingBlock.HVAC_OFF
        assert off_desired.anomalies == (AnomalyCode.UNEXPECTED_RAPID_LOSS,)

        # Turning anomaly_detection off clears the region immediately.
        cleared_desired, cleared_state = decide(
            replace(snap, now_monotonic=1020.0),
            replace(off_state, anomaly_detection=False),
        )
        assert cleared_desired.anomalies == ()
        assert cleared_state.anomaly == an.AnomalyState()

        # Startup and maintenance tiers preserve orthogonal region behavior cleanly.
        for tier_state in (
            make_state(anomaly_detection=True, lifecycle=LifecycleState()),
            make_state(
                anomaly_detection=True,
                maintenance=MaintenanceRunning(
                    running_since=950.0, next_due=DUE_AT_FIRST_TICK
                ),
            ),
            make_state(
                anomaly_detection=True, window=WindowState(phase=WindowPhase.OPEN)
            ),
            make_state(
                anomaly_detection=True,
                control_mode=ControlModeState(mode=ControlMode.HOLD),
            ),
        ):
            tier_desired, _ = decide(snap, tier_state)
            assert isinstance(tier_desired.anomalies, tuple)
