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

# The synthetic winter weeks run the standard plant through cold snaps down
# to about -15 °C, below what that plant is sized for; they saturate for
# about a quarter of the week by design of the weather generator.
_UNDERSIZED_BY_DESIGN = frozenset(
    {"S37_winter_week_humid_continental", "S38_winter_week_semi_arid"}
)


@pytest.mark.parametrize(
    "name", sorted(n for n in ALL_SCENARIOS if n not in _UNDERSIZED_BY_DESIGN)
)
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


def test_undersized_scenarios_still_exist() -> None:
    """The allow-list names only scenarios that are still in the library."""
    assert _UNDERSIZED_BY_DESIGN <= set(ALL_SCENARIOS)
