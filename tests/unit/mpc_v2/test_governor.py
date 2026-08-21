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
