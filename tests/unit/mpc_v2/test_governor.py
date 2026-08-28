"""Unit tests for the static Scalar Reference Governor."""

from __future__ import annotations

import pytest

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


def _highest_feasible_reference(
    plant: PlantModelRC2, params: GovernorParams, T_outdoor_C: float, above_C: float
) -> float:
    """Return the warmest reference the plant can still hold within its bounds.

    ``steady_input`` grows monotonically with the reference, so the feasible
    band has a single upper edge. Bisecting the plant model locates it without
    restating the plant algebra in the test; ``above_C`` is a reference known
    to be past the edge.
    """
    lo, hi = T_outdoor_C, above_C
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if plant.steady_input(mid, T_outdoor_C) <= params.u_max - params.safety_margin:
            lo = mid
        else:
            hi = mid
    return lo


def test_rise_beyond_plant_authority_converges_to_the_feasibility_limit() -> None:
    """A setpoint the plant cannot hold is approached up to its limit.

    Over the whole sequence the governed reference must never decrease, never
    pass the user setpoint, stay feasible at every step, and settle at the
    warmest feasible reference within the resolution of the bisection.
    """
    params = GovernorParams()
    plant = PlantModelRC2(PlantParams(), dt_s=300.0)
    gov = ScalarReferenceGovernor(plant, params)
    T_sp, T_outdoor_C, T_room_C = 40.0, 16.0, 19.0

    references = [
        gov.update(T_sp=T_sp, T_outdoor_C=T_outdoor_C, T_room_now=T_room_C)
        for _ in range(200)
    ]

    limit_C = _highest_feasible_reference(plant, params, T_outdoor_C, T_sp)
    # The bisection still accepts a step of 2**-iters of the remaining gap, so
    # it can only come to rest this far below the limit.
    stall_gap_K = (T_sp - limit_C) / (2**params.bisection_iters - 1)

    assert references == sorted(references)
    assert all(reference <= T_sp for reference in references)
    assert all(
        params.u_min + params.safety_margin
        <= plant.steady_input(reference, T_outdoor_C)
        <= params.u_max - params.safety_margin
        for reference in references
    )
    assert references[-1] == pytest.approx(limit_C, abs=stall_gap_K)


@pytest.mark.xfail(
    strict=True,
    reason="_is_feasible applies the lower input bound in both directions, so a "
    "lowered setpoint whose steady-state valve fraction sits below the safety "
    "margin is never released",
)
def test_lowered_setpoint_is_reached_within_a_bounded_number_of_steps() -> None:
    """Closing the valve is always available, so a lower setpoint is reachable.

    From a settled warmer reference the sequence must walk down to the new
    setpoint without ever dropping below it.
    """
    params = GovernorParams()
    plant = PlantModelRC2(PlantParams(), dt_s=300.0)
    gov = ScalarReferenceGovernor(plant, params)
    T_outdoor_C, T_start_C, T_sp = 16.0, 21.0, 17.0
    gov.update(T_sp=T_start_C, T_outdoor_C=T_outdoor_C, T_room_now=T_start_C)

    references = [
        gov.update(T_sp=T_sp, T_outdoor_C=T_outdoor_C, T_room_now=T_start_C)
        for _ in range(200)
    ]

    # A bisection that keeps stepping comes to rest at most this far above the
    # setpoint; a larger residual is a reference the governor refuses to give up.
    residual_K = (T_start_C - T_sp) / (2**params.bisection_iters - 1)

    assert references == sorted(references, reverse=True)
    assert all(reference >= T_sp for reference in references)
    assert references[-1] == pytest.approx(T_sp, abs=residual_K)
