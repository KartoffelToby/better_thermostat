"""HeatingPower adapter tests."""

from __future__ import annotations

from custom_components.better_thermostat.utils.const import (
    MAX_HEATING_POWER,
    MIN_HEATING_POWER,
    VALVE_MIN_OPENING_LARGE_DIFF,
)
from tests.benchmark.adapters.base import BenchmarkContext
from tests.benchmark.adapters.heating_power_adapter import HeatingPowerAdapter


def _ctx(
    t: float = 0.0, target: float = 21.0, current: float = 20.0
) -> BenchmarkContext:
    return BenchmarkContext(
        t=t,
        dt=30.0,
        target_temp_C=target,
        current_temp_C=current,
        raw_room_temp_C=current,
        trv_temp_C=current,
        outdoor_temp_C=5.0,
    )


def test_name_and_family():
    """Name and family."""
    adapter = HeatingPowerAdapter()
    assert adapter.name == "heating_power"
    assert adapter.family == "valve"


def test_initial_heating_power_is_clamped_to_realistic_range():
    """Initial heating power is clamped to realistic range."""
    too_low = HeatingPowerAdapter(initial_heating_power=0.0001)
    too_high = HeatingPowerAdapter(initial_heating_power=10.0)
    assert too_low.heating_power == MIN_HEATING_POWER
    assert too_high.heating_power == MAX_HEATING_POWER


def test_zero_demand_produces_zero_valve():
    """At-target or over-target → zero valve."""
    adapter = HeatingPowerAdapter()
    assert adapter.step(_ctx(target=20.0, current=20.0)).valve_percent == 0.0
    assert adapter.step(_ctx(target=20.0, current=22.0)).valve_percent == 0.0


def test_positive_demand_produces_positive_valve():
    """Positive demand produces positive valve."""
    adapter = HeatingPowerAdapter()
    out = adapter.step(_ctx(target=21.0, current=20.0))
    assert out.valve_percent is not None
    assert out.valve_percent > 0.0


def test_large_diff_enforces_min_opening():
    """Large diff enforces min opening."""
    # With a fast heating_power the formula would underestimate; the
    # minimum-opening floor for diff > 0.3 K kicks in regardless.
    adapter = HeatingPowerAdapter(initial_heating_power=MAX_HEATING_POWER)
    out = adapter.step(_ctx(target=22.0, current=20.0))
    assert out.valve_percent is not None
    assert out.valve_percent >= VALVE_MIN_OPENING_LARGE_DIFF * 100.0


def test_valve_clamps_to_100_when_demand_is_extreme():
    """Valve clamps to 100 when demand is extreme."""
    adapter = HeatingPowerAdapter(initial_heating_power=MIN_HEATING_POWER)
    out = adapter.step(_ctx(target=22.0, current=18.0))
    assert out.valve_percent is not None
    assert out.valve_percent == 100.0


def test_reset_clears_cycle_and_restores_initial_power():
    """Reset clears cycle and restores initial power."""
    adapter = HeatingPowerAdapter(initial_heating_power=0.02)
    # Drive a heating cycle so internal state populates.
    for t in range(30):
        adapter.step(_ctx(t=t * 30.0, target=21.0, current=20.0))
    adapter.heating_power = 0.05  # mutate to verify reset path
    adapter.reset()
    assert adapter.heating_power == 0.02
    assert adapter._cycle_start_temp is None


def test_reset_seeds_from_prior():
    """Reset seeds from prior."""
    adapter = HeatingPowerAdapter(initial_heating_power=0.02)
    adapter.reset(prior={"heating_power": 0.05})
    assert adapter.heating_power == 0.05


def test_reset_ignores_non_numeric_prior():
    """Reset ignores non numeric prior."""
    adapter = HeatingPowerAdapter(initial_heating_power=0.02)
    adapter.reset(prior={"heating_power": "garbage"})
    assert adapter.heating_power == 0.02


def test_reset_clamps_seeded_power():
    """Reset clamps seeded power."""
    adapter = HeatingPowerAdapter()
    adapter.reset(prior={"heating_power": 5.0})
    assert adapter.heating_power == MAX_HEATING_POWER


def test_export_state_includes_heating_power():
    """Export state includes heating power."""
    adapter = HeatingPowerAdapter(initial_heating_power=0.03)
    snapshot = adapter.export_state()
    assert snapshot["heating_power"] == 0.03


def test_diagnostics_expose_heating_power():
    """Diagnostics expose heating power."""
    adapter = HeatingPowerAdapter()
    out = adapter.step(_ctx(target=21.0, current=20.0))
    assert out.diagnostics["heating_power"] == adapter.heating_power


def test_cycle_finalizes_when_room_cools_after_peak():
    """Cycle finalizes when room cools after peak."""
    adapter = HeatingPowerAdapter(initial_heating_power=0.02)
    # Phase 1: warm valve-open phase for 10 minutes, room rises 20 → 21.
    for i in range(20):
        cur = 20.0 + 0.05 * i
        adapter.step(_ctx(t=i * 30.0, target=22.0, current=cur))
    # Phase 2: target reached → valve closes. Room peaks at 21.2 then falls.
    adapter.step(_ctx(t=20 * 30.0, target=20.5, current=21.2))  # cycle end (valve = 0)
    adapter.step(_ctx(t=21 * 30.0, target=20.5, current=21.0))  # cooling → finalize
    # heating_power should have moved off its initial value.
    assert adapter.heating_power != 0.02
    # And cycle state has been cleared.
    assert adapter._cycle_start_temp is None


def test_cycle_finalize_clamps_to_max():
    """Cycle finalize clamps to max."""
    adapter = HeatingPowerAdapter(initial_heating_power=MAX_HEATING_POWER)
    # Implausibly fast heating: 5 K in 5 minutes = 1 K/min, way above MAX.
    for i in range(10):
        adapter.step(_ctx(t=i * 30.0, target=25.0, current=20.0 + 0.5 * i))
    adapter.step(_ctx(t=10 * 30.0, target=22.0, current=25.0))  # cycle end
    adapter.step(_ctx(t=11 * 30.0, target=22.0, current=24.0))  # cooling → finalize
    assert adapter.heating_power <= MAX_HEATING_POWER


def test_short_cycle_does_not_update_heating_power():
    """Cycles shorter than 1 minute are discarded."""
    adapter = HeatingPowerAdapter(initial_heating_power=0.02)
    # Open valve for one 30 s step, then close.
    adapter.step(_ctx(t=0.0, target=21.0, current=20.0))  # heating starts
    adapter.step(_ctx(t=20.0, target=20.0, current=20.5))  # valve = 0, peak
    adapter.step(_ctx(t=40.0, target=20.0, current=20.3))  # cooling → finalize
    # Duration was well below _MIN_CYCLE_DURATION (1 min) → no update.
    assert adapter.heating_power == 0.02


def test_repeated_steps_are_deterministic():
    """Repeated steps are deterministic."""
    a = HeatingPowerAdapter()
    b = HeatingPowerAdapter()
    for t in range(50):
        out_a = a.step(_ctx(t=t * 30.0, target=21.0, current=20.0 + 0.01 * t))
        out_b = b.step(_ctx(t=t * 30.0, target=21.0, current=20.0 + 0.01 * t))
        assert out_a.valve_percent == out_b.valve_percent
    assert a.heating_power == b.heating_power


def test_cycle_finalizes_when_heating_resumes_before_cooldown():
    """Demand resuming above the tracked peak still finalizes the cycle."""
    adapter = HeatingPowerAdapter(initial_heating_power=0.02)
    # Heating phase: room 20 → 21 over 10 minutes.
    for i in range(20):
        adapter.step(_ctx(t=i * 30.0, target=22.0, current=20.0 + 0.05 * i))
    # Valve closes and the post-heat peak is tracked, but demand resumes
    # before the room ever cools below that peak.
    adapter.step(_ctx(t=20 * 30.0, target=20.5, current=21.2))  # valve = 0, peak
    adapter.step(_ctx(t=21 * 30.0, target=23.0, current=21.2))  # heating resumes
    # The finished cycle updated the learner instead of being dropped.
    assert adapter.heating_power != 0.02
