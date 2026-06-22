"""Reporter rendering tests."""

from __future__ import annotations

from tests.benchmark.metrics import MetricValues
from tests.benchmark.reporter import (
    ScenarioResult,
    ScoredResult,
    _mean,
    _stdev,
    format_metric,
    render_per_scenario,
    render_plant_sweep,
    render_score_matrix,
    score_results,
)
from tests.benchmark.scoring import PROFILES, DimensionScores


def _m(rmse: float = 0.1, integral: float = 100.0, cycles: int = 10) -> MetricValues:
    return MetricValues(
        max_overshoot_K=0.2,
        max_undershoot_K=0.1,
        settling_time_min=10.0,
        steady_state_error_K=0.05,
        rmse_tracking_K=rmse,
        valve_cycle_count=cycles,
        integral_valve_pct_min=integral,
        total_valve_travel_pct=500.0,
        time_above_setpoint_K_h=0.1,
        time_below_setpoint_K_h=0.1,
        valve_sweet_spot_residency_pct=50.0,
    )


def _scored(
    controller: str,
    scenario: str,
    overall: float,
    comfort: float = 0.9,
    actuator: float = 0.7,
    energy: float = 0.85,
    block_label: str = "",
) -> ScoredResult:
    return ScoredResult(
        controller=controller,
        scenario=scenario,
        metrics=_m(),
        scores=DimensionScores(
            overall=overall, comfort=comfort, actuator=actuator, energy=energy
        ),
        block_label=block_label,
    )


def test_format_metric_renders_finite():
    """Format metric renders finite."""
    assert format_metric(1.234567, decimals=2) == "1.23"
    assert format_metric(0.0, decimals=3) == "0.000"


def test_format_metric_handles_inf_and_nan():
    """Format metric handles inf and nan."""
    assert format_metric(float("inf"), 2).strip() == "inf"
    assert format_metric(float("nan"), 2).strip() == "NaN"


def test_mean_empty_is_zero():
    """Mean empty is zero."""
    assert _mean([]) == 0.0


def test_mean_filters_nan():
    """Mean filters nan."""
    assert _mean([1.0, float("nan"), 3.0]) == 2.0


def test_stdev_one_value_is_zero():
    """Stdev one value is zero."""
    assert _stdev([0.5]) == 0.0


def test_stdev_empty_is_zero():
    """Stdev empty is zero."""
    assert _stdev([]) == 0.0


def test_stdev_two_values():
    """Stdev two values."""
    # pstdev of [1, 2] is 0.5 (population).
    assert abs(_stdev([1.0, 2.0]) - 0.5) < 1e-9


def test_score_results_joins_on_block_label():
    """Score results joins on block label."""
    results = {
        "block_a": [
            ScenarioResult(controller="pid", scenario="S01", metrics=_m()),
            ScenarioResult(controller="pid", scenario="S02", metrics=_m()),
        ]
    }
    oracle = {("block_a", "S01"): _m(), ("block_a", "S02"): _m()}
    scored = score_results(results, oracle, PROFILES["balanced"])
    assert len(scored) == 2
    assert {s.scenario for s in scored} == {"S01", "S02"}


def test_score_results_skips_when_oracle_missing():
    """Score results skips when oracle missing."""
    results = {
        "block_a": [
            ScenarioResult(controller="pid", scenario="S01", metrics=_m()),
            ScenarioResult(controller="pid", scenario="missing", metrics=_m()),
        ]
    }
    oracle = {("block_a", "S01"): _m()}  # no entry for "missing"
    scored = score_results(results, oracle, PROFILES["balanced"])
    assert len(scored) == 1
    assert scored[0].scenario == "S01"


def test_render_score_matrix_orders_by_overall_descending():
    """Render score matrix orders by overall descending."""
    items = [
        _scored("a", "S01", overall=0.7),
        _scored("a", "S02", overall=0.9),
        _scored("b", "S01", overall=0.95),
        _scored("b", "S02", overall=0.85),
    ]
    out = render_score_matrix(items, PROFILES["balanced"])
    # Header bits.
    assert "Score matrix" in out
    assert "comfort" in out
    assert "σ" in out
    # The higher-mean controller (b, mean 0.9) outranks a (mean 0.8) and gets the * marker.
    a_line = [line for line in out.splitlines() if "a " in line and "b " not in line][
        -1
    ]
    b_line = [line for line in out.splitlines() if "b " in line][-1]
    # b appears above a in the body.
    assert out.index(b_line) < out.index(a_line)
    assert b_line.startswith(" *")


def test_render_score_matrix_handles_single_run():
    """Render score matrix handles single run."""
    items = [_scored("only", "S01", overall=0.8)]
    out = render_score_matrix(items, PROFILES["balanced"])
    assert "only" in out
    # σ = 0 for n = 1
    assert "0.000" in out


def test_render_per_scenario_lists_each_scenario_row():
    """Render per scenario lists each scenario row."""
    items = [
        _scored("pid", "S01", overall=0.8),
        _scored("pid", "S02", overall=0.7),
        _scored("mpc", "S01", overall=0.9),
        _scored("mpc", "S02", overall=0.6),
    ]
    out = render_per_scenario(items, PROFILES["balanced"])
    assert "S01" in out and "S02" in out
    assert "pid" in out and "mpc" in out


def test_render_plant_sweep_columns_and_cross_mean():
    """Render plant sweep columns and cross mean."""
    block_a = [_scored("pid", "S01", 0.8), _scored("pid", "S02", 0.7)]
    block_b = [_scored("pid", "S01", 0.85), _scored("pid", "S02", 0.75)]
    out = render_plant_sweep(
        {"plant=alpha": block_a, "plant=beta": block_b}, PROFILES["balanced"]
    )
    assert "Cross-plant" in out
    assert "plant=alpha" in out or "alpha" in out
    assert "plant=beta" in out or "beta" in out
    assert "mean" in out
    assert "±σ" in out


def test_render_plant_sweep_sorts_controllers_by_cross_mean():
    """Render plant sweep sorts controllers by cross mean."""
    block_a = [_scored("strong", "S01", 0.95), _scored("weak", "S01", 0.40)]
    block_b = [_scored("strong", "S01", 0.90), _scored("weak", "S01", 0.50)]
    out = render_plant_sweep(
        {"plant=a": block_a, "plant=b": block_b}, PROFILES["balanced"]
    )
    assert out.index("strong") < out.index("weak")


def test_score_results_threads_block_label():
    """score_results carries the block label into ScoredResult."""
    results = {"plant=a": [ScenarioResult("pid", "S01", _m())]}
    oracle = {("plant=a", "S01"): _m()}
    scored = score_results(results, oracle, PROFILES["balanced"])
    assert scored[0].block_label == "plant=a"


def test_per_scenario_qualifies_rows_per_block_label():
    """Same-named scenarios from different blocks render as separate rows."""
    scored = [
        _scored("pid", "S01", 0.9, block_label="plant=a"),
        _scored("pid", "S01", 0.5, block_label="plant=b"),
    ]
    out = render_per_scenario(scored, PROFILES["balanced"])
    assert "S01 [plant=a]" in out
    assert "S01 [plant=b]" in out


def test_plant_sweep_ranks_full_coverage_first():
    """A partially-covered controller sorts below full-sweep controllers."""
    scored_per_plant = {
        "plant=a": [
            _scored("low_full", "S01", 0.2),
            _scored("high_partial", "S01", 0.99),
        ],
        "plant=b": [_scored("low_full", "S01", 0.2)],
    }
    out = render_plant_sweep(scored_per_plant, PROFILES["balanced"])
    lines = out.splitlines()
    row_low = next(i for i, line in enumerate(lines) if "low_full" in line)
    row_high = next(i for i, line in enumerate(lines) if "high_partial" in line)
    assert row_low < row_high
    assert "2/2" in lines[row_low]
    assert "1/2" in lines[row_high]
