"""MPC v2 tuning surface and plant priors."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..mpc_v2_internals.dob import DobParams
from ..mpc_v2_internals.governor import GovernorParams
from ..mpc_v2_internals.kalman import KalmanParams
from ..mpc_v2_internals.plant import PlantParams
from ..mpc_v2_internals.qp_optimiser import QpParams
from ..mpc_v2_internals.rls import RlsParams


@dataclass
class MpcV2Params:
    """Tuning surface for MPC v2.

    Defaults target a representative residential envelope; per-room overrides
    flow through the BT options flow.
    """

    plant: PlantParams = field(default_factory=PlantParams)
    kalman: KalmanParams = field(default_factory=KalmanParams)
    dob: DobParams = field(default_factory=DobParams)
    qp: QpParams = field(default_factory=QpParams)
    rls: RlsParams = field(default_factory=RlsParams)
    governor: GovernorParams = field(default_factory=GovernorParams)
    # Observer / plant-simulation cadence (Kalman, Smith, RLS sampling).
    # QP cadence lives on ``qp.step_s``.
    plant_step_s: float = 30.0
    enable_rls: bool = True


# Static plant priors keyed roughly to room size / envelope speed. These
# are alternatives to the AUTO path which derives ``tau_room_min`` from BT's
# online learnings. RLS still adapts on top of whichever prior is chosen.
PLANT_PRESETS: dict[str, PlantParams] = {
    "small_room": PlantParams(
        tau_room_min=240.0, tau_rad_min=10.0, gain_heater=2.5, coupling_rad_room=1.0
    ),
    "medium_room": PlantParams(
        tau_room_min=480.0, tau_rad_min=15.0, gain_heater=2.0, coupling_rad_room=1.0
    ),
    "large_room": PlantParams(
        tau_room_min=900.0, tau_rad_min=20.0, gain_heater=1.5, coupling_rad_room=1.0
    ),
}


def make_plant_prior(
    heating_power: float | None = None,
    heat_loss_rate: float | None = None,
    typical_delta_K: float = 15.0,
    preset: str | None = None,
) -> PlantParams:
    """Build an RC2 plant prior.

    Resolution order:

    1. If ``preset`` matches one of :data:`PLANT_PRESETS`, return that
       prior verbatim — the user explicitly opted out of auto-derivation.
    2. Otherwise derive ``tau_room_min`` from ``heat_loss_rate`` (assumed
       at ``typical_delta_K`` outdoor delta), clamped into the RLS bounds.
    3. Fall back to ``PlantParams()`` defaults when neither input applies.

    RLS still tunes the resulting prior online; the preset only sets the
    *starting point* for the identifier.
    """
    if preset and preset in PLANT_PRESETS:
        return replace(PLANT_PRESETS[preset])
    params = PlantParams()
    if heat_loss_rate is not None and heat_loss_rate > 0.0:
        params.tau_room_min = max(60.0, min(2000.0, typical_delta_K / heat_loss_rate))
    # heating_power not mapped yet — RLS adapts gain from the first cycle.
    _ = heating_power
    return params
