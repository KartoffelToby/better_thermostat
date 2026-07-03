"""Pure tests for the safety hull at the command boundary."""

from datetime import UTC, datetime

from custom_components.better_thermostat.core.desired import DesiredState, TrvDesired
from custom_components.better_thermostat.core.safety import clamp
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    TrvReported,
    WorldSnapshot,
)


def _snapshot(**trv_overrides) -> WorldSnapshot:
    defaults = {
        "entity_id": "climate.trv",
        "min_temp": 5.0,
        "max_temp": 30.0,
        "valve_max_opening": 80.0,
    }
    defaults.update(trv_overrides)
    return WorldSnapshot(
        now=datetime(2026, 1, 10, tzinfo=UTC),
        now_monotonic=0.0,
        trvs={"climate.trv": TrvReported(**defaults)},
    )


def _desired(setpoint=None, valve=None, mode=HvacMode.HEAT) -> DesiredState:
    return DesiredState(
        call_for_heat=True,
        trvs={
            "climate.trv": TrvDesired(
                entity_id="climate.trv",
                hvac_mode=mode,
                setpoint=setpoint,
                valve_percent=valve,
            )
        },
    )


def test_setpoint_above_max_is_capped():
    """A controller output above the device max is capped to it."""
    out = clamp(_desired(setpoint=42.0), _snapshot())
    assert out.trvs["climate.trv"].setpoint == 30.0


def test_frost_floor_holds_for_any_intent():
    """The min-temp floor applies regardless of the intent's mode."""
    out = clamp(_desired(setpoint=1.0, mode=HvacMode.OFF), _snapshot())
    assert out.trvs["climate.trv"].setpoint == 5.0


def test_valve_is_limited_to_max_opening():
    """Valve percentages are kept inside 0..valve_max_opening."""
    out = clamp(_desired(valve=150.0), _snapshot())
    assert out.trvs["climate.trv"].valve_percent == 80.0
    out = clamp(_desired(valve=-5.0), _snapshot())
    assert out.trvs["climate.trv"].valve_percent == 0.0


def test_within_limits_is_untouched():
    """Intents inside the limits pass through identically."""
    desired = _desired(setpoint=21.0, valve=40.0)
    assert clamp(desired, _snapshot()) == desired


def test_non_finite_setpoint_is_pinned_to_a_bound():
    """NaN/inf setpoints never slip past the hull as invalid payloads."""
    out = clamp(_desired(setpoint=float("nan")), _snapshot())
    assert out.trvs["climate.trv"].setpoint == 5.0
    out = clamp(_desired(setpoint=float("inf")), _snapshot())
    assert out.trvs["climate.trv"].setpoint == 5.0


def test_non_finite_valve_is_pinned_to_a_bound():
    """NaN/inf valve percentages are clamped instead of leaking through."""
    out = clamp(_desired(valve=float("inf")), _snapshot())
    assert out.trvs["climate.trv"].valve_percent == 0.0


def test_unknown_trv_keeps_intent_but_caps_valve_at_100():
    """Without reported limits, the valve still stays inside 0..100."""
    snapshot = WorldSnapshot(
        now=datetime(2026, 1, 10, tzinfo=UTC), now_monotonic=0.0, trvs={}
    )
    out = clamp(_desired(setpoint=42.0, valve=150.0), snapshot)
    assert out.trvs["climate.trv"].setpoint == 42.0
    assert out.trvs["climate.trv"].valve_percent == 100.0


def test_jump_limit_is_opt_in():
    """Without max_valve_jump, big changes pass; with it, they are limited."""
    previous = _desired(valve=10.0)
    out = clamp(_desired(valve=80.0), _snapshot(), previous=previous)
    assert out.trvs["climate.trv"].valve_percent == 80.0

    out = clamp(
        _desired(valve=80.0), _snapshot(), previous=previous, max_valve_jump=20.0
    )
    assert out.trvs["climate.trv"].valve_percent == 30.0

    out = clamp(
        _desired(valve=0.0),
        _snapshot(),
        previous=_desired(valve=50.0),
        max_valve_jump=20.0,
    )
    assert out.trvs["climate.trv"].valve_percent == 30.0


def test_none_values_stay_none():
    """Missing setpoint or valve intent stays missing (no write implied)."""
    out = clamp(_desired(), _snapshot())
    assert out.trvs["climate.trv"].setpoint is None
    assert out.trvs["climate.trv"].valve_percent is None


def test_offset_is_clamped_to_the_calibration_range():
    """Calibration offsets stay inside the device's local range."""
    snapshot = _snapshot(local_calibration_min=-7.0, local_calibration_max=7.0)
    desired = DesiredState(
        call_for_heat=True,
        trvs={"climate.trv": TrvDesired(entity_id="climate.trv", offset=-9.5)},
    )
    out = clamp(desired, snapshot)
    assert out.trvs["climate.trv"].offset == -7.0

    desired = DesiredState(
        call_for_heat=True,
        trvs={"climate.trv": TrvDesired(entity_id="climate.trv", offset=9.5)},
    )
    out = clamp(desired, snapshot)
    assert out.trvs["climate.trv"].offset == 7.0


def test_offset_inside_the_range_is_untouched():
    """Offsets inside the device range pass through identically."""
    snapshot = _snapshot(local_calibration_min=-7.0, local_calibration_max=7.0)
    desired = DesiredState(
        call_for_heat=True,
        trvs={"climate.trv": TrvDesired(entity_id="climate.trv", offset=-2.5)},
    )
    assert clamp(desired, snapshot) == desired
