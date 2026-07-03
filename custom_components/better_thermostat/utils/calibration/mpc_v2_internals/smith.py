"""Smith predictor — roll the observer state forward through in-flight commands.

When ``valve_command_delay_s`` > 0 (boiler→radiator pipe transport lag), the
Kalman state reflects the plant *as it is now*, but the MPC needs the state
*as it will be after the last few commands take effect*. The Smith predictor
re-runs those queued commands through the linear plant model so the
optimiser plans against the right initial state.

The command history holds one entry per MPC re-plan, so the entries are
spaced at the QP step (``qp.step_s``). The plant handed to this predictor
must therefore be discretised on that same grid — the controller binds the
coarse plant, not the fine observer plant.
"""

from __future__ import annotations

import math

from ._types import FloatArray
from .plant import PlantModelRC2


class SmithPredictor:
    """Rolls the observer state forward through in-flight valve commands."""

    def __init__(self, plant: PlantModelRC2) -> None:
        """Bind the plant model used to replay in-flight commands.

        Parameters
        ----------
        plant : PlantModelRC2
            RC2 plant whose one-step propagator rolls the observer state
            forward through the queued valve commands.
        """
        self.plant = plant

    def predict(
        self,
        x_now: FloatArray,
        u_recent_history: list[float],
        T_outdoor_C: float,
        dead_time_s: float,
    ) -> FloatArray:
        """Propagate ``x_now`` through the recent commands spanning the dead time.

        Replays the last ``ceil(dead_time_s / dt_s)`` commands through the
        plant model and returns the predicted state — ``ceil`` because a
        command whose interval only partially overlaps the delay window is
        still in flight and must be replayed. With no dead time or no command
        history it returns a copy of ``x_now`` unchanged.
        """
        if dead_time_s <= 0.0 or not u_recent_history:
            return x_now.copy()
        n_steps = max(1, math.ceil(dead_time_s / self.plant.dt_s))
        x = x_now.copy()
        for u in u_recent_history[-n_steps:]:
            x = self.plant.discrete_step(x, u, T_outdoor_C)
        return x
