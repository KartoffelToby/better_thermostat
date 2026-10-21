"""Every scored scenario must leave the oracle headroom to hold its setpoint.

A scenario whose setpoint the plant cannot hold saturates every controller
at a fully open valve: the run then measures the plant's capacity, not the
controller, and a naive controller ties the oracle.
"""

from __future__ import annotations

import pytest

from tests.benchmark.adapters.baselines import IdealOracleAdapter
from tests.benchmark.scenarios import ALL_SCENARIOS

# Steady-state valve opening above which the oracle has too little authority
# left to reject a disturbance.
_MAX_STEADY_STATE_VALVE_PCT = 90.0
_SAMPLE_INTERVAL_S = 300


@pytest.mark.parametrize("name", sorted(ALL_SCENARIOS))
def test_oracle_can_hold_every_setpoint_of_the_scenario(name: str) -> None:
    """The steady-state valve stays below the headroom limit at every sample."""
    scenario = ALL_SCENARIOS[name]
    oracle = IdealOracleAdapter(plant_params=scenario.plant)
    worst = max(
        (
            oracle.steady_state_valve_percent(
                scenario.setpoint_schedule(float(t)),
                scenario.outdoor_schedule(float(t)),
            ),
            t,
        )
        for t in range(0, scenario.duration_minutes * 60 + 1, _SAMPLE_INTERVAL_S)
    )
    assert worst[0] <= _MAX_STEADY_STATE_VALVE_PCT, (
        f"{name} needs {worst[0]:.0f} % valve at t={worst[1] / 60:.0f} min"
    )
