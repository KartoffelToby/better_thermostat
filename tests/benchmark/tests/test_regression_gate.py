"""Regression gate over the scored controller matrix.

The benchmark's headline output is a 0..1 score matrix. Beyond the unit
tests of individual components, this test turns the matrix itself into a
guard: it runs the production controllers over a representative,
noise-free scenario set and asserts the invariants that must always hold
if the plant, metrics, scoring and controllers are wired up correctly.

Each controller holds a recorded floor per dimension, about 0.02 below
its current mean, so a controller change that costs a few hundredths of
comfort, valve travel or energy fails the build. The floors ratchet: a
floor more than 0.04 below the measured mean fails too, so an
improvement has to be recorded before the margin it opened can be spent
again. A scoring bug that lets a candidate beat the oracle fails the
ceiling check.

Scenarios here carry no sensor noise/jitter, so the per-scenario sensor
seed does not affect the numbers — the gate stays stable run to run.
"""

from __future__ import annotations

import math
import statistics

import pytest

from tests.benchmark.runner import run_scenario
from tests.benchmark.scenarios import (
    S01_SETPOINT_STEP_SMALL,
    S02_SETPOINT_STEP_LARGE,
    S03_FROST_TO_COMFORT,
    S07_OUTDOOR_STEP_COLD,
    S08_OUTDOOR_RAMP_WARM,
)
from tests.benchmark.scoring import PROFILES, DimensionScores, compute_scores

_SCENARIOS = [
    S01_SETPOINT_STEP_SMALL,
    S02_SETPOINT_STEP_LARGE,
    S03_FROST_TO_COMFORT,
    S07_OUTDOOR_STEP_COLD,
    S08_OUTDOOR_RAMP_WARM,
]

_DIMENSIONS = ("overall", "comfort", "actuator", "energy")

# Mean balanced score per dimension over _SCENARIOS, recorded about 0.02
# below the measured value and rounded down to the hundredth.
_FLOORS: dict[str, dict[str, float]] = {
    "mpc": {"overall": 0.69, "comfort": 0.87, "actuator": 0.21, "energy": 0.95},
    "mpc_v2": {"overall": 0.92, "comfort": 0.96, "actuator": 0.82, "energy": 0.97},
    "pid": {"overall": 0.82, "comfort": 0.79, "actuator": 0.78, "energy": 0.97},
    "tpi": {"overall": 0.73, "comfort": 0.53, "actuator": 0.94, "energy": 0.94},
    "default": {"overall": 0.64, "comfort": 0.38, "actuator": 0.94, "energy": 0.85},
}

# A floor further than this below the measured mean is stale and has to
# be raised, so the gate keeps following the controllers upward.
_MAX_FLOOR_SLACK = 0.04


def _make_adapter(name: str):
    """Build a fresh adapter by registry name (deferred imports keep it cheap)."""
    from tests.benchmark.adapters.baselines import IdealOracleAdapter
    from tests.benchmark.adapters.mpc_adapter import MpcAdapter
    from tests.benchmark.adapters.mpc_v2_adapter import MpcV2Adapter
    from tests.benchmark.adapters.passive_modes import DefaultCalibrationAdapter
    from tests.benchmark.adapters.pid_adapter import PidAdapter
    from tests.benchmark.adapters.tpi_adapter import TpiAdapter

    factories = {
        "ideal_oracle": IdealOracleAdapter,
        "mpc": MpcAdapter,
        "mpc_v2": MpcV2Adapter,
        "pid": PidAdapter,
        "tpi": TpiAdapter,
        "default": DefaultCalibrationAdapter,
    }
    return factories[name]()


@pytest.fixture(scope="module")
def scenario_scores() -> dict[str, dict[str, DimensionScores]]:
    """Per-(controller, scenario) balanced scores; oracle normalises to itself.

    Retained per scenario (not just the mean) so the ceiling check can catch a
    controller that beats the oracle on an individual scenario while losing on
    average.
    """
    profile = PROFILES["balanced"]
    controllers = ["ideal_oracle", "mpc", "mpc_v2", "pid", "tpi", "default"]

    # Oracle metrics per scenario are the normalisation baseline.
    oracle_metrics = {
        s.name: run_scenario(_make_adapter("ideal_oracle"), s).metrics
        for s in _SCENARIOS
    }

    out: dict[str, dict[str, DimensionScores]] = {}
    for name in controllers:
        out[name] = {
            s.name: compute_scores(
                run_scenario(_make_adapter(name), s).metrics,
                oracle_metrics[s.name],
                profile,
            )
            for s in _SCENARIOS
        }
    return out


def _mean(
    scores: dict[str, dict[str, DimensionScores]], name: str, dimension: str
) -> float:
    return statistics.mean(getattr(s, dimension) for s in scores[name].values())


def test_oracle_is_the_ceiling(
    scenario_scores: dict[str, dict[str, DimensionScores]],
) -> None:
    """No controller may out-score the IdealOracle on ANY scenario."""
    for s in _SCENARIOS:
        oracle = scenario_scores["ideal_oracle"][s.name].overall
        for name, per in scenario_scores.items():
            if name == "ideal_oracle":
                continue
            assert per[s.name].overall <= oracle + 1e-9, (
                f"{name} on {s.name} ({per[s.name].overall:.3f}) out-scored the "
                f"oracle ({oracle:.3f}) — a scoring or normalisation regression."
            )


def test_oracle_scores_near_one(
    scenario_scores: dict[str, dict[str, DimensionScores]],
) -> None:
    """The oracle normalises against itself, so it must sit near 1.0.

    A symmetric tolerance band catches normalisation drift in either direction
    (a score above 1.0 would let a candidate "beat" the oracle; below would
    mean the oracle failed to normalise to itself).
    """
    oracle = _mean(scenario_scores, "ideal_oracle", "overall")
    assert oracle == pytest.approx(1.0, abs=1e-3), (
        f"oracle normalised to {oracle:.3f}, expected ~1.0 — plant/metric regression."
    )


_GATED = [(name, dimension) for name in _FLOORS for dimension in _DIMENSIONS]


@pytest.mark.parametrize(("name", "dimension"), _GATED)
def test_controller_holds_its_floor(
    name: str, dimension: str, scenario_scores: dict[str, dict[str, DimensionScores]]
) -> None:
    """Each gated controller stays at or above its recorded floor."""
    score = _mean(scenario_scores, name, dimension)
    floor = _FLOORS[name][dimension]
    assert score >= floor, (
        f"{name} {dimension} fell to {score:.4f} (floor {floor:.2f}) — "
        f"a controller regression."
    )


@pytest.mark.parametrize(("name", "dimension"), _GATED)
def test_floor_follows_the_controller(
    name: str, dimension: str, scenario_scores: dict[str, dict[str, DimensionScores]]
) -> None:
    """A floor that trails the measured mean by too much has to be raised."""
    score = _mean(scenario_scores, name, dimension)
    floor = _FLOORS[name][dimension]
    raised = math.floor((score - 0.02) * 100.0) / 100.0
    assert score - floor <= _MAX_FLOOR_SLACK, (
        f"{name} {dimension} now scores {score:.4f}; raise its floor in "
        f"_FLOORS from {floor:.2f} to {raised:.2f}."
    )
