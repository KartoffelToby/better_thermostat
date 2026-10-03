"""Guard: an unawaited coroutine fails the test that created it.

The suite turns an unawaited coroutine into an error. Python only reports
one when the coroutine is collected, and a coroutine held in a reference
cycle is collected whenever the cycle collector next runs, which may be in
some later, unrelated test. The error has to land on the test that leaked it.
"""

from pathlib import Path
import shutil
import subprocess
import sys

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """\
import gc


async def _work():
    return 1


def test_a_leaks_a_coroutine_in_a_cycle():
    holder = {}
    holder["self"] = holder
    holder["coroutine"] = _work()


def test_b_collects_garbage():
    gc.collect()
"""


def test_the_leaking_test_is_the_one_that_fails(tmp_path):
    """The test that leaves a coroutine unawaited fails; the next one passes."""
    shutil.copy(_REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copy(_REPO_ROOT / "tests" / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text(_PROBE, encoding="utf-8")

    finished = subprocess.run(
        (sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA", "-q"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert "ERROR test_probe.py::test_a_leaks_a_coroutine_in_a_cycle" in (
        finished.stdout
    ), finished.stdout
    assert "PASSED test_probe.py::test_b_collects_garbage" in finished.stdout
