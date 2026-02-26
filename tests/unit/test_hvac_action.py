"""Tests for the pure HVAC action computation module.

Covers:
  - should_heat_with_tolerance  (hysteresis helper)
  - to_pct                      (valve normalisation)
  - compute_hvac_action         (main FSM, TRV overrides, idempotency)
  - Hysteresis state transitions
"""

from homeassistant.components.climate.const import HVACAction, HVACMode
import pytest

from custom_components.better_thermostat.utils.hvac_action import (
    HvacActionResult,
    ToleranceHysteresis,
    TrvSnapshot,
    compute_hvac_action,
    should_heat_with_tolerance,
    to_pct,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _default_kwargs(**overrides):
    """Return compute_hvac_action kwargs with sensible defaults."""
    base = dict(
        hysteresis=ToleranceHysteresis(),
        cur_temp=20.0,
        target_temp=21.0,
        cool_target=None,
        hvac_mode=HVACMode.HEAT,
        bt_hvac_mode=HVACMode.HEAT,
        window_open=False,
        tolerance=0.5,
        ignore_states=False,
        trv_snapshots=[],
        device_name="Test",
    )
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# Group 1: should_heat_with_tolerance
# ═══════════════════════════════════════════════════════════════════════════


class TestShouldHeatWithTolerance:
    def test_starts_below_threshold(self):
        """Heating starts when temp < target - tolerance."""
        assert should_heat_with_tolerance(20.4, 21.0, 0.5, HVACAction.IDLE) is True

    def test_no_start_in_band_when_idle(self):
        """No start in [target-tol, target) when previously IDLE."""
        assert should_heat_with_tolerance(20.7, 21.0, 0.5, HVACAction.IDLE) is False

    def test_continues_in_band_when_heating(self):
        """Continue heating in band when already HEATING."""
        assert should_heat_with_tolerance(20.7, 21.0, 0.5, HVACAction.HEATING) is True

    def test_stops_at_target(self):
        assert should_heat_with_tolerance(21.0, 21.0, 0.5, HVACAction.HEATING) is False

    def test_stops_above_target(self):
        assert should_heat_with_tolerance(21.3, 21.0, 0.5, HVACAction.HEATING) is False

    def test_negative_tolerance_clamped(self):
        """Negative tolerance is clamped to 0 → same as zero tolerance."""
        # With tol=0, heat_on_threshold = target = 21.0; cur=20.9 < 21.0 → True
        assert should_heat_with_tolerance(20.9, 21.0, -1.0, HVACAction.IDLE) is True

    def test_zero_tolerance(self):
        """Zero tolerance: heat_on == heat_off == target."""
        assert should_heat_with_tolerance(20.99, 21.0, 0.0, HVACAction.IDLE) is True
        assert should_heat_with_tolerance(21.0, 21.0, 0.0, HVACAction.IDLE) is False

    def test_none_previous_action(self):
        """None previous_action treated as non-HEATING → strict threshold."""
        assert should_heat_with_tolerance(20.4, 21.0, 0.5, None) is True
        assert should_heat_with_tolerance(20.7, 21.0, 0.5, None) is False


# ═══════════════════════════════════════════════════════════════════════════
# Group 2: to_pct
# ═══════════════════════════════════════════════════════════════════════════


class TestToPct:
    def test_fraction_to_percent(self):
        assert to_pct(0.5) == 50.0

    def test_already_percent(self):
        assert to_pct(50) == 50.0

    def test_zero(self):
        assert to_pct(0.0) == 0.0

    def test_one_stays(self):
        """1.0 is NOT in [0, 1) → returned as-is (already percent)."""
        assert to_pct(1.0) == 1.0

    def test_invalid_returns_none(self):
        assert to_pct("abc") is None
        assert to_pct(None) is None

    def test_string_number(self):
        assert to_pct("0.5") == 50.0


# ═══════════════════════════════════════════════════════════════════════════
# Group 3: compute_hvac_action
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeHvacAction:
    # --- early exits -------------------------------------------------------

    def test_none_temps_idle(self):
        r = compute_hvac_action(**_default_kwargs(cur_temp=None))
        assert r.action == HVACAction.IDLE

    def test_none_target_idle(self):
        r = compute_hvac_action(**_default_kwargs(target_temp=None))
        assert r.action == HVACAction.IDLE

    def test_off_mode_returns_off(self):
        r = compute_hvac_action(**_default_kwargs(hvac_mode=HVACMode.OFF))
        assert r.action == HVACAction.OFF

    def test_bt_off_mode_returns_off(self):
        r = compute_hvac_action(**_default_kwargs(bt_hvac_mode=HVACMode.OFF))
        assert r.action == HVACAction.OFF

    def test_window_open_returns_idle(self):
        r = compute_hvac_action(**_default_kwargs(window_open=True))
        assert r.action == HVACAction.IDLE

    # --- heating decision --------------------------------------------------

    def test_heating_below_threshold(self):
        r = compute_hvac_action(**_default_kwargs(cur_temp=20.4))
        assert r.action == HVACAction.HEATING

    def test_idle_in_band(self):
        r = compute_hvac_action(**_default_kwargs(cur_temp=20.7))
        assert r.action == HVACAction.IDLE

    def test_continues_heating_in_band(self):
        hyst = ToleranceHysteresis(last_action=HVACAction.HEATING)
        r = compute_hvac_action(**_default_kwargs(hysteresis=hyst, cur_temp=20.7))
        assert r.action == HVACAction.HEATING

    def test_stops_at_target(self):
        hyst = ToleranceHysteresis(last_action=HVACAction.HEATING)
        r = compute_hvac_action(**_default_kwargs(hysteresis=hyst, cur_temp=21.0))
        assert r.action == HVACAction.IDLE

    # --- cooling -----------------------------------------------------------

    def test_cooling_in_heat_cool(self):
        r = compute_hvac_action(
            **_default_kwargs(
                hvac_mode=HVACMode.HEAT_COOL,
                bt_hvac_mode=HVACMode.HEAT_COOL,
                cur_temp=27.0,
                cool_target=25.0,
                tolerance=0.5,
            )
        )
        assert r.action == HVACAction.COOLING

    def test_no_cooling_within_tolerance(self):
        r = compute_hvac_action(
            **_default_kwargs(
                hvac_mode=HVACMode.HEAT_COOL,
                bt_hvac_mode=HVACMode.HEAT_COOL,
                cur_temp=25.3,
                cool_target=25.0,
                tolerance=0.5,
            )
        )
        assert r.action != HVACAction.COOLING

    # --- TRV override ------------------------------------------------------

    def test_trv_hvac_action_override(self):
        snap = TrvSnapshot(trv_id="trv1", hvac_action="heating")
        r = compute_hvac_action(
            **_default_kwargs(cur_temp=20.7, trv_snapshots=[snap])
        )
        assert r.action == HVACAction.HEATING

    def test_trv_valve_position_override(self):
        snap = TrvSnapshot(trv_id="trv1", valve_position=0.5)
        r = compute_hvac_action(
            **_default_kwargs(cur_temp=20.7, trv_snapshots=[snap])
        )
        assert r.action == HVACAction.HEATING

    def test_trv_last_valve_percent_override(self):
        snap = TrvSnapshot(trv_id="trv1", last_valve_percent=30.0)
        r = compute_hvac_action(
            **_default_kwargs(cur_temp=20.7, trv_snapshots=[snap])
        )
        assert r.action == HVACAction.HEATING

    def test_ignore_states_skips_trv_override(self):
        snap = TrvSnapshot(trv_id="trv1", hvac_action="heating")
        r = compute_hvac_action(
            **_default_kwargs(cur_temp=20.7, ignore_states=True, trv_snapshots=[snap])
        )
        assert r.action == HVACAction.IDLE

    def test_ignore_trv_states_per_trv(self):
        snap = TrvSnapshot(
            trv_id="trv1", ignore_trv_states=True, hvac_action="heating"
        )
        r = compute_hvac_action(
            **_default_kwargs(cur_temp=20.7, trv_snapshots=[snap])
        )
        assert r.action == HVACAction.IDLE

    def test_trv_zero_valve_no_override(self):
        snap = TrvSnapshot(trv_id="trv1", valve_position=0.0)
        r = compute_hvac_action(
            **_default_kwargs(cur_temp=20.7, trv_snapshots=[snap])
        )
        assert r.action == HVACAction.IDLE

    # --- idempotency -------------------------------------------------------

    def test_hysteresis_not_mutated(self):
        """Calling compute_hvac_action must NOT mutate the hysteresis input."""
        hyst = ToleranceHysteresis(last_action=HVACAction.HEATING, hold_active=True)
        compute_hvac_action(**_default_kwargs(hysteresis=hyst, cur_temp=21.0))
        assert hyst.last_action == HVACAction.HEATING
        assert hyst.hold_active is True

    def test_result_is_frozen(self):
        r = compute_hvac_action(**_default_kwargs())
        with pytest.raises(AttributeError):
            r.action = HVACAction.OFF  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════
# Group 4: Hysteresis state transitions
# ═══════════════════════════════════════════════════════════════════════════


class TestHysteresisTransitions:
    def test_tolerance_decision_not_corrupted_by_trv(self):
        """TRV override must not change tolerance_decision."""
        hyst = ToleranceHysteresis(last_action=HVACAction.HEATING)
        snap = TrvSnapshot(trv_id="trv1", hvac_action="heating")
        r = compute_hvac_action(
            **_default_kwargs(hysteresis=hyst, cur_temp=21.0, trv_snapshots=[snap])
        )
        # Tolerance says IDLE (at target), TRV overrides to HEATING
        assert r.tolerance_decision == HVACAction.IDLE
        assert r.action == HVACAction.HEATING
        # Hysteresis state follows tolerance, not TRV
        assert r.new_last_action == HVACAction.IDLE

    def test_hold_active_set_in_band(self):
        r = compute_hvac_action(**_default_kwargs(cur_temp=20.7))
        assert r.new_hold_active is True

    def test_hold_active_false_when_heating(self):
        r = compute_hvac_action(**_default_kwargs(cur_temp=20.4))
        assert r.new_hold_active is False

    def test_hold_active_false_when_cooling(self):
        r = compute_hvac_action(
            **_default_kwargs(
                hvac_mode=HVACMode.HEAT_COOL,
                bt_hvac_mode=HVACMode.HEAT_COOL,
                cur_temp=27.0,
                cool_target=25.0,
            )
        )
        assert r.new_hold_active is False

    def test_100_dashboard_reads_no_drift(self):
        """Repeated reads with same hysteresis must produce identical results."""
        hyst = ToleranceHysteresis(last_action=HVACAction.IDLE)
        kwargs = _default_kwargs(hysteresis=hyst, cur_temp=20.7)
        first = compute_hvac_action(**kwargs)
        for _ in range(100):
            r = compute_hvac_action(**kwargs)
            assert r == first
        # Hysteresis unchanged
        assert hyst.last_action == HVACAction.IDLE
        assert hyst.hold_active is False

    def test_full_cycle_heat_stop_band_restart(self):
        """Full cycle: cold → heat → reach target → band → cold → restart."""
        hyst = ToleranceHysteresis()

        # 1) Cold start → HEATING
        r = compute_hvac_action(**_default_kwargs(hysteresis=hyst, cur_temp=20.0))
        assert r.action == HVACAction.HEATING
        hyst.last_action = r.new_last_action
        hyst.hold_active = r.new_hold_active

        # 2) In band, still heating (hysteresis)
        r = compute_hvac_action(**_default_kwargs(hysteresis=hyst, cur_temp=20.7))
        assert r.action == HVACAction.HEATING
        hyst.last_action = r.new_last_action
        hyst.hold_active = r.new_hold_active

        # 3) Reach target → IDLE
        r = compute_hvac_action(**_default_kwargs(hysteresis=hyst, cur_temp=21.0))
        assert r.action == HVACAction.IDLE
        hyst.last_action = r.new_last_action
        hyst.hold_active = r.new_hold_active

        # 4) Drop into band → still IDLE (hysteresis prevents restart)
        r = compute_hvac_action(**_default_kwargs(hysteresis=hyst, cur_temp=20.7))
        assert r.action == HVACAction.IDLE
        hyst.last_action = r.new_last_action
        hyst.hold_active = r.new_hold_active

        # 5) Drop below band → HEATING again
        r = compute_hvac_action(**_default_kwargs(hysteresis=hyst, cur_temp=20.4))
        assert r.action == HVACAction.HEATING

    def test_off_resets_hysteresis(self):
        """OFF mode should reset hysteresis to IDLE."""
        hyst = ToleranceHysteresis(last_action=HVACAction.HEATING, hold_active=True)
        r = compute_hvac_action(**_default_kwargs(hysteresis=hyst, hvac_mode=HVACMode.OFF))
        assert r.new_last_action == HVACAction.IDLE
        assert r.new_hold_active is False
