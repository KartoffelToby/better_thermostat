"""MPC v2 tuning surface and plant priors."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..mpc_v2_internals.dob import DobParams
from ..mpc_v2_internals.governor import GovernorParams
from ..mpc_v2_internals.kalman import KalmanParams
from ..mpc_v2_internals.plant import PlantParams
from ..mpc_v2_internals.qp_optimiser import QpParams


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
    governor: GovernorParams = field(default_factory=GovernorParams)
    # Observer / plant-simulation cadence (Kalman, Smith). QP cadence lives
    # on ``qp.step_s``.
    plant_step_s: float = 30.0


# Static plant priors as an alternative to the AUTO path (which derives
# ``tau_room_min`` from BT's online learnings). The presets differ only in
# ``tau_room_min`` — the envelope speed that tracks room size. Gain, radiator
# time constant and coupling stay at the robust default: across realistic
# plant profiles ``gain_heater`` does not correlate with room size, so a
# single ~2.0 prior is more robust than size-scaled values.
PLANT_PRESETS: dict[str, PlantParams] = {
    "small_room": PlantParams(
        tau_room_min=180.0, tau_rad_min=15.0, gain_heater=2.0, coupling_rad_room=1.0
    ),
    "medium_room": PlantParams(
        tau_room_min=480.0, tau_rad_min=15.0, gain_heater=2.0, coupling_rad_room=1.0
    ),
    "large_room": PlantParams(
        tau_room_min=720.0, tau_rad_min=15.0, gain_heater=2.0, coupling_rad_room=1.0
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
       at ``typical_delta_K`` outdoor delta), clamped to a plausible range.
    3. Fall back to ``PlantParams()`` defaults when neither input applies.
    """
    if preset and preset in PLANT_PRESETS:
        return replace(PLANT_PRESETS[preset])
    params = PlantParams()
    if heat_loss_rate is not None and heat_loss_rate > 0.0:
        params.tau_room_min = max(60.0, min(2000.0, typical_delta_K / heat_loss_rate))
    # heating_power not mapped yet — the gain stays at the prior default.
    _ = heating_power
    return params
