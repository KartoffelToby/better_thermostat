"""Guard: a hung test fails on its own instead of taking the CI job with it.

Without a per-test deadline a test that waits forever runs until the CI job's
own limit cancels the job, and the log ends without a traceback or the name of
the test that hung.
"""

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/tests.yaml"


def test_a_hung_test_fails_before_the_ci_job_is_cancelled(pytestconfig):
    """Each test has a deadline, and it ends inside the CI job's time limit."""
    job_seconds = (
        yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["test"][
            "timeout-minutes"
        ]
        * 60
    )

    deadline = float(pytestconfig.getini("timeout") or 0)

    assert pytestconfig.pluginmanager.hasplugin("timeout")
    assert 0 < deadline < job_seconds
