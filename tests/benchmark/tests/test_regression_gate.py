"""Regression gate over the scored controller matrix.

The benchmark's headline output is a 0..1 score matrix. Beyond the unit
tests of individual components, this test turns the matrix itself into a
guard: it runs the production controllers over a representative,
noise-free scenario set and asserts the invariants that must always hold
if the plant, metrics, scoring and controllers are wired up correctly.

It is intentionally loose — wide of the current numbers — so it is not
flaky, but tight enough to catch a real regression: a controller change
that quietly halves a smart controller's score, a scoring bug that lets
a candidate beat the oracle, or a plant change that breaks tracking.

Scenarios here carry no sensor noise/jitter, so the per-scenario sensor
seed does not affect the numbers — the gate stays stable run to run.
"""

from __future__ import annotations

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
from tests.benchmark.scoring import PROFILES, compute_scores

_SCENARIOS = [
    S01_SETPOINT_STEP_SMALL,
    S02_SETPOINT_STEP_LARGE,
    S03_FROST_TO_COMFORT,
    S07_OUTDOOR_STEP_COLD,
    S08_OUTDOOR_RAMP_WARM,
]

# Smart controllers must stay comfortably above this. They currently sit
# around 0.72-0.78 on this set; a floor of 0.50 ignores normal scenario
# variation but trips on a genuine regression.
_SMART_OVERALL_FLOOR = 0.50

_SMART_CONTROLLERS = ("mpc", "pid", "tpi")


def _make_adapter(name: str):
    """Build a fresh adapter by registry name (deferred imports keep it cheap)."""
    from tests.benchmark.adapters.baselines import IdealOracleAdapter
    from tests.benchmark.adapters.mpc_adapter import MpcAdapter
    from tests.benchmark.adapters.passive_modes import DefaultCalibrationAdapter
    from tests.benchmark.adapters.pid_adapter import PidAdapter
    from tests.benchmark.adapters.tpi_adapter import TpiAdapter

    factories = {
        "ideal_oracle": IdealOracleAdapter,
        "mpc": MpcAdapter,
        "pid": PidAdapter,
        "tpi": TpiAdapter,
        "default": DefaultCalibrationAdapter,
    }
    return factories[name]()


@pytest.fixture(scope="module")
def mean_overall_scores() -> dict[str, dict[str, float]]:
    """Per-(controller, scenario) balanced overall score; oracle normalises to itself.

    Retained per scenario (not just the mean) so the ceiling check can catch a
    controller that beats the oracle on an individual scenario while losing on
    average.
    """
    profile = PROFILES["balanced"]
    controllers = ["ideal_oracle", "mpc", "pid", "tpi", "default"]

    # Oracle metrics per scenario are the normalisation baseline.
    oracle_metrics = {
        s.name: run_scenario(_make_adapter("ideal_oracle"), s).metrics
        for s in _SCENARIOS
    }

    out: dict[str, dict[str, float]] = {}
    for name in controllers:
        per: dict[str, float] = {}
        for s in _SCENARIOS:
            metrics = run_scenario(_make_adapter(name), s).metrics
            per[s.name] = compute_scores(
                metrics, oracle_metrics[s.name], profile
            ).overall
        out[name] = per
    return out


def test_oracle_is_the_ceiling(
    mean_overall_scores: dict[str, dict[str, float]],
) -> None:
    """No controller may out-score the IdealOracle on ANY scenario."""
    for s in _SCENARIOS:
        oracle = mean_overall_scores["ideal_oracle"][s.name]
        for name, per in mean_overall_scores.items():
            if name == "ideal_oracle":
                continue
            assert per[s.name] <= oracle + 1e-9, (
                f"{name} on {s.name} ({per[s.name]:.3f}) out-scored the oracle "
                f"({oracle:.3f}) — a scoring or normalisation regression."
            )


def test_oracle_scores_near_one(
    mean_overall_scores: dict[str, dict[str, float]],
) -> None:
    """The oracle normalises against itself, so it must sit near 1.0.

    A symmetric tolerance band catches normalisation drift in either direction
    (a score above 1.0 would let a candidate "beat" the oracle; below would
    mean the oracle failed to normalise to itself).
    """
    oracle = statistics.mean(mean_overall_scores["ideal_oracle"].values())
    assert oracle == pytest.approx(1.0, abs=1e-3), (
        f"oracle normalised to {oracle:.3f}, expected ~1.0 — plant/metric regression."
    )


@pytest.mark.parametrize("name", _SMART_CONTROLLERS)
def test_smart_controllers_above_floor(
    name: str, mean_overall_scores: dict[str, float]
) -> None:
    """Each smart controller must stay above the regression floor."""
    score = statistics.mean(mean_overall_scores[name].values())
    assert score >= _SMART_OVERALL_FLOOR, (
        f"{name} fell to {score:.3f} (floor {_SMART_OVERALL_FLOOR}) — "
        f"a controller regression."
    )
