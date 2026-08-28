"""Unit tests for the static Scalar Reference Governor."""

from __future__ import annotations

from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.governor import (
    GovernorParams,
    ScalarReferenceGovernor,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
    PlantParams,
)


def _make_governor() -> ScalarReferenceGovernor:
    """Build a governor on a default RC2 plant."""
    plant = PlantModelRC2(PlantParams(), dt_s=300.0)
    return ScalarReferenceGovernor(plant, GovernorParams())


def test_disabled_returns_user_setpoint_unchanged() -> None:
    """A disabled governor passes the user setpoint through untouched."""
    plant = PlantModelRC2(PlantParams(), dt_s=300.0)
    gov = ScalarReferenceGovernor(plant, GovernorParams(enabled=False))
    v = gov.update(T_sp=25.0, T_outdoor_C=-10.0, T_room_now=18.0)
    assert v == 25.0


def test_feasible_setpoint_passes_through() -> None:
    """A feasible setpoint is left unchanged and stored as state."""
    gov = _make_governor()
    v = gov.update(T_sp=21.0, T_outdoor_C=5.0, T_room_now=20.5)
    assert abs(v - 21.0) < 1e-9
    assert gov.state() == v


def test_infeasible_setpoint_shaped_below_target() -> None:
    """A target the plant cannot hold in steady state must be shaped down."""
    gov = _make_governor()
    # Cold outdoor + small heater authority ⇒ u_ss > u_max-δ at high setpoint.
    v = gov.update(T_sp=40.0, T_outdoor_C=-20.0, T_room_now=19.0)
    assert v < 40.0
    assert v >= 19.0  # never below current room


def test_restore_state_round_trip() -> None:
    """A restored governor reproduces the saved reference state."""
    gov = _make_governor()
    gov.update(T_sp=21.0, T_outdoor_C=5.0, T_room_now=20.0)
    snapshot = gov.state()
    gov2 = _make_governor()
    assert gov2.state() is None
    gov2.restore(snapshot)
    assert gov2.state() == snapshot


def test_reset_drops_internal_reference() -> None:
    """Reset clears the governor's internal reference state."""
    gov = _make_governor()
    gov.update(T_sp=21.0, T_outdoor_C=5.0, T_room_now=20.0)
    gov.reset()
    assert gov.state() is None


def test_lowered_setpoint_is_reached_and_not_only_approached() -> None:
    """Lowering the setpoint in mild weather must reach the new target.

    A setpoint that needs almost no flow is still attainable — the valve
    closes. Holding the governed reference above it would keep the room warm
    against the user's wish.
    """
    gov = _make_governor()
    gov.update(T_sp=21.0, T_outdoor_C=16.0, T_room_now=21.0)
    v = 21.0
    for _ in range(200):
        v = gov.update(T_sp=17.0, T_outdoor_C=16.0, T_room_now=21.0)
    assert abs(v - 17.0) < 1e-9, f"governed reference stalled at {v:.3f} C"


def test_small_rise_in_mild_weather_reaches_the_setpoint() -> None:
    """A modest raise below the flow margin must not stall the reference."""
    gov = _make_governor()
    gov.update(T_sp=17.0, T_outdoor_C=16.0, T_room_now=17.0)
    v = gov.update(T_sp=17.5, T_outdoor_C=16.0, T_room_now=17.0)
    assert abs(v - 17.5) < 1e-9, f"governed reference stalled at {v:.3f} C"


def test_disturbance_estimate_enters_the_reachability_check() -> None:
    """Free heat counts toward the flow a setpoint needs."""
    gov = _make_governor()
    without_gain = gov.update(T_sp=25.0, T_outdoor_C=-20.0, T_room_now=19.0)
    assert without_gain < 25.0
    gov.reset()
    with_gain = gov.update(
        T_sp=25.0, T_outdoor_C=-20.0, T_room_now=19.0, D_hat_K_per_min=0.1
    )
    assert abs(with_gain - 25.0) < 1e-9
