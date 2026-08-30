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


def test_reference_tracks_a_feasibility_limit_that_moves_every_step() -> None:
    """The reference follows the plant's authority up as the weather releases it.

    A setpoint the plant can never hold keeps the governor engaged for the
    whole run, so nothing but the governor decides the reference. Warming
    outdoor air lifts the feasibility limit a little on every step, and the
    sequence has to ride it: never falling back, never passing the user
    setpoint, staying feasible against the outdoor temperature of its own
    step, and closing to within one bisection resolution of that step's limit.
    """
    params = GovernorParams()
    plant = PlantModelRC2(PlantParams(), dt_s=300.0)
    gov = ScalarReferenceGovernor(plant, params)
    T_sp, T_room_C, steps = 40.0, 19.0, 200
    outdoors_C = [16.0 * step / (steps - 1) for step in range(steps)]

    references = [
        gov.update(T_sp=T_sp, T_outdoor_C=T_outdoor_C, T_room_now=T_room_C)
        for T_outdoor_C in outdoors_C
    ]
    limits_C = [
        _highest_feasible_reference(plant, params, T_outdoor_C, T_sp)
        for T_outdoor_C in outdoors_C
    ]
    # Each step bisects a fraction of the distance from the previous reference
    # to the setpoint, which never exceeds the distance from the seed, so the
    # reference lands within this much of the limit it searched against.
    resolution_K = (T_sp - T_room_C) / 2**params.bisection_iters

    # A reference that stalls after two steps would satisfy everything below
    # without ever tracking anything.
    assert len(set(references)) > steps // 2
    assert references == sorted(references)
    assert all(reference <= T_sp for reference in references)
    assert all(
        plant.steady_input(reference, T_outdoor_C)
        <= params.u_max - params.safety_margin
        for reference, T_outdoor_C in zip(references, outdoors_C, strict=True)
    )
    assert all(
        limit_C - reference < resolution_K
        for reference, limit_C in zip(references, limits_C, strict=True)
    )


def test_lowered_setpoint_is_handed_back_unshaped() -> None:
    """Asking for less heat needs no shaping, so the setpoint passes straight through.

    The governor exists to keep the implied steady-state input inside the
    actuator's range, and closing the valve further is always available. From
    a settled warmer reference the first update therefore has to return the
    new setpoint itself, and every later one has to keep returning it.
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

    assert references == [T_sp] * 200


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
