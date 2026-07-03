"""Unit tests for the Smith predictor."""

from __future__ import annotations

import numpy as np

from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
    PlantParams,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.smith import (
    SmithPredictor,
)


def _make_predictor() -> SmithPredictor:
    """Build a Smith predictor on a default RC2 plant."""
    return SmithPredictor(PlantModelRC2(PlantParams(), dt_s=30.0))


def test_zero_dead_time_returns_state_unchanged() -> None:
    """Zero dead time returns the input state unchanged."""
    sp = _make_predictor()
    x = np.array([20.0, 30.0])
    np.testing.assert_array_equal(sp.predict(x, [0.5, 0.7], 5.0, dead_time_s=0.0), x)


def test_empty_history_returns_state_unchanged() -> None:
    """An empty command history returns the input state unchanged."""
    sp = _make_predictor()
    x = np.array([20.0, 30.0])
    np.testing.assert_array_equal(sp.predict(x, [], 5.0, dead_time_s=120.0), x)


def test_predict_rolls_state_forward_one_step_per_history_entry() -> None:
    """A 60 s dead-time on a 30 s plant pulls in the last 2 history entries."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    sp = SmithPredictor(plant)
    x0 = np.array([20.0, 30.0])
    history = [0.2, 0.4, 0.6]  # most recent two consumed by dead_time=60s
    out = sp.predict(x0, history, T_outdoor_C=5.0, dead_time_s=60.0)

    # Independently propagate the same two commands and compare.
    expected = plant.discrete_step(x0, history[-2], T_outdoor_C=5.0)
    expected = plant.discrete_step(expected, history[-1], T_outdoor_C=5.0)
    np.testing.assert_allclose(out, expected, atol=1e-12)


def test_partial_step_overlap_replays_the_in_flight_command() -> None:
    """A dead time of 31 s on a 30 s grid replays 2 commands, not 1.

    The second command's interval only partially overlaps the delay window,
    but it is still in flight — ``ceil`` must pull it into the replay.
    """
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    sp = SmithPredictor(plant)
    x0 = np.array([20.0, 30.0])
    history = [0.2, 0.4, 0.6]
    out = sp.predict(x0, history, T_outdoor_C=5.0, dead_time_s=31.0)

    expected = plant.discrete_step(x0, history[-2], T_outdoor_C=5.0)
    expected = plant.discrete_step(expected, history[-1], T_outdoor_C=5.0)
    np.testing.assert_allclose(out, expected, atol=1e-12)


def test_long_dead_time_uses_full_history() -> None:
    """When dead_time spans the entire buffer the predictor walks all of it."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    sp = SmithPredictor(plant)
    x0 = np.array([19.5, 28.0])
    history = [0.1, 0.2, 0.3, 0.4]
    out = sp.predict(x0, history, T_outdoor_C=5.0, dead_time_s=200.0)
    expected = x0.copy()
    for u in history:
        expected = plant.discrete_step(expected, u, T_outdoor_C=5.0)
    np.testing.assert_allclose(out, expected, atol=1e-12)
