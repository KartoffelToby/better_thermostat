"""Unit tests for the RLS room-balance identifier."""

from __future__ import annotations

import numpy as np

from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
    PlantParams,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.rls import (
    RLSIdentifier,
    RlsParams,
)


def _make_identifier(
    tau_room_min: float = 480.0,
) -> tuple[PlantModelRC2, RLSIdentifier]:
    """Build a plant and an RLS identifier seeded from its params."""
    plant = PlantModelRC2(PlantParams(tau_room_min=tau_room_min), dt_s=30.0)
    return plant, RLSIdentifier(plant, RlsParams())


def test_first_update_only_seeds_history() -> None:
    """The first update only fills the previous-sample buffer, no estimate runs."""
    plant, rls = _make_identifier()
    rls.update(t_s=0.0, T_room_C=20.0, T_rad_C=22.0, T_outdoor_C=5.0)
    # No update count change — only the previous-sample buffer was filled.
    assert rls.update_count == 0
    assert rls.skip_count == 0


def test_low_excitation_skips_update() -> None:
    """When all signals are equal the regressor is zero — must skip."""
    plant, rls = _make_identifier()
    rls.update(t_s=0.0, T_room_C=20.0, T_rad_C=20.0, T_outdoor_C=20.0)
    rls.update(t_s=30.0, T_room_C=20.0, T_rad_C=20.0, T_outdoor_C=20.0)
    assert rls.update_count == 0
    assert rls.skip_count >= 1


def test_rls_drifts_plant_params_under_persistent_signal() -> None:
    """Drive the identifier with a step in T_rad; tau_room should move."""
    plant, rls = _make_identifier(tau_room_min=480.0)
    initial_tau = plant.params.tau_room_min

    # Synthetic trajectory: T_room rises ~0.02 K per 30 s under T_rad − T_room = 5,
    # T_outdoor low — that implies a *smaller* effective tau_room.
    T_room = 20.0
    for k in range(200):
        t_s = float(k * 30)
        rls.update(t_s=t_s, T_room_C=T_room, T_rad_C=T_room + 5.0, T_outdoor_C=5.0)
        T_room += 0.02

    assert rls.update_count > 10
    # Plant param has moved (we don't pin a direction here — bounds-projection
    # may dominate — only that the identifier actually folded into the plant).
    assert plant.params.tau_room_min != initial_tau


def test_projection_keeps_params_in_bounds() -> None:
    """RLS estimates outside bounds must be projected back."""
    plant, rls = _make_identifier()
    bad = np.array([-1.0, 1e-9])  # negative coupling, near-zero b ⇒ huge tau
    projected = rls._project(bad)
    a_new, b_new = float(projected[0]), float(projected[1])
    tau = 1.0 / max(b_new, 1e-12)
    coupling = a_new / max(b_new, 1e-12)
    assert 60.0 <= tau <= 2000.0
    assert 0.2 <= coupling <= 2.0


def test_reset_restores_prior() -> None:
    """Reset clears the counters and the previous-sample buffer."""
    plant, rls = _make_identifier()
    rls.update(t_s=0.0, T_room_C=20.0, T_rad_C=25.0, T_outdoor_C=5.0)
    rls.update(t_s=30.0, T_room_C=20.1, T_rad_C=25.0, T_outdoor_C=5.0)
    rls.reset()
    assert rls.update_count == 0
    assert rls.skip_count == 0
    assert rls._prev_T_room is None
