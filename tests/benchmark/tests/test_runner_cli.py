"""CLI-level tests for ``runner.main``.

These exercise the argparse paths and the orchestration loop —
single controller, single scenario combinations — keeping wall-clock
manageable by restricting the controller/scenario set.
"""

from __future__ import annotations

import contextlib
import io

import pytest

from tests.benchmark.adapters.baselines import LinearPAdapter
from tests.benchmark.runner import main, run_scenario
from tests.benchmark.scenarios import S01_SETPOINT_STEP_SMALL


def _run(argv: list[str]) -> tuple[int, str]:
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        rc = main(argv)
    return rc, buf_out.getvalue() + buf_err.getvalue()


def test_main_single_controller_single_scenario():
    """Main single controller single scenario."""
    rc, out = _run(
        [
            "--controller",
            "linear_p",
            "--scenario",
            "S01_setpoint_step_small",
            "--stabilisation-min",
            "0",
            "--step-s",
            "60",
        ]
    )
    assert rc == 0
    assert "Score matrix" in out
    assert "linear_p" in out


def test_main_unknown_controller_exits_with_code_2():
    """Main unknown controller exits with code 2."""
    rc, out = _run(["--controller", "no_such_controller"])
    assert rc == 2
    assert "Unknown controller" in out


def test_main_unknown_scenario_exits_with_code_2():
    """Main unknown scenario exits with code 2."""
    rc, out = _run(["--scenario", "S99_does_not_exist"])
    assert rc == 2
    assert "Unknown scenario" in out


def test_main_unknown_plant_exits_with_code_2():
    """Main unknown plant exits with code 2."""
    rc, out = _run(
        [
            "--controller",
            "linear_p",
            "--scenario",
            "S01_setpoint_step_small",
            "--plant",
            "no_such_plant",
        ]
    )
    assert rc == 2
    assert "Unknown plant" in out


def test_main_per_scenario_flag_prints_detail_table():
    """Main per scenario flag prints detail table."""
    rc, out = _run(
        [
            "--controller",
            "linear_p",
            "--scenario",
            "S01_setpoint_step_small",
            "--scenario",
            "S02_setpoint_step_large",
            "--stabilisation-min",
            "0",
            "--step-s",
            "60",
            "--per-scenario",
        ]
    )
    assert rc == 0
    assert "Per-scenario" in out
    assert "S01_setpoint_step_small" in out
    assert "S02_setpoint_step_large" in out


def test_main_plant_override_runs_against_named_plant():
    """Main plant override runs against named plant."""
    rc, _out = _run(
        [
            "--controller",
            "linear_p",
            "--scenario",
            "S01_setpoint_step_small",
            "--plant",
            "realistic",
            "--stabilisation-min",
            "0",
            "--step-s",
            "60",
        ]
    )
    # We assert rc=0 — the plant label only surfaces under --plant-sweep.
    assert rc == 0


@pytest.mark.parametrize(
    "profile_name", ["balanced", "comfort_first", "longevity_first", "energy_first"]
)
def test_main_accepts_each_user_profile(profile_name):
    """Main accepts each user profile."""
    rc, out = _run(
        [
            "--controller",
            "linear_p",
            "--scenario",
            "S01_setpoint_step_small",
            "--profile",
            profile_name,
            "--stabilisation-min",
            "0",
            "--step-s",
            "60",
        ]
    )
    assert rc == 0
    assert profile_name in out


def test_main_multi_trv_block_runs():
    """Main multi trv block runs."""
    rc, out = _run(
        [
            "--controller",
            "linear_p",
            "--scenario",
            "S01_setpoint_step_small",
            "--stabilisation-min",
            "0",
            "--step-s",
            "120",
            "--multi-trv",
        ]
    )
    assert rc == 0
    # Multi-TRV emits an additional matrix block with one of the multi profiles.
    assert "multi-symmetric" in out or "multi-asymmetric" in out


def test_main_plant_sweep_prints_cross_plant_summary():
    """Main plant sweep prints cross plant summary."""
    rc, out = _run(
        [
            "--controller",
            "linear_p",
            "--scenario",
            "S01_setpoint_step_small",
            "--stabilisation-min",
            "0",
            "--step-s",
            "120",
            "--plant-sweep",
        ]
    )
    assert rc == 0
    assert "Cross-plant" in out
    # Per-plant labels appear.
    assert "plant=realistic" in out
    # All five DOE / realistic plants are represented.
    for p in (
        "realistic",
        "doe_sfd_pre1980",
        "doe_sfd_2004",
        "doe_sfd_2010",
        "doe_midrise_apt",
    ):
        assert p in out


def test_main_plant_all_keyword_runs_full_sweep():
    """Main plant all keyword runs full sweep."""
    rc, out = _run(
        [
            "--controller",
            "linear_p",
            "--scenario",
            "S01_setpoint_step_small",
            "--plant",
            "all",
            "--stabilisation-min",
            "0",
            "--step-s",
            "120",
        ]
    )
    # The "all" keyword expands to every PLANT_PROFILE; runs to completion.
    assert rc == 0
    assert "Score matrix" in out


def test_main_rejects_non_positive_step_s():
    """Main rejects non positive step_s with exit code 2."""
    rc, out = _run(
        [
            "--controller",
            "linear_p",
            "--scenario",
            "S01_setpoint_step_small",
            "--step-s",
            "0",
        ]
    )
    assert rc == 2
    assert "--step-s" in out


def test_main_plant_sweep_with_unknown_scenario_exits_with_code_2():
    """Unknown scenarios are reported before --plant-sweep filtering."""
    rc, out = _run(["--plant-sweep", "--scenario", "S99_does_not_exist"])
    assert rc == 2
    assert "Unknown scenario" in out


def test_run_scenario_rejects_non_positive_step_s():
    """run_scenario rejects non positive step_s."""
    with pytest.raises(ValueError):
        run_scenario(LinearPAdapter(), S01_SETPOINT_STEP_SMALL, step_s=0.0)
