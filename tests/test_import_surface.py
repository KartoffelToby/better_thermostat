"""Guards on what the integration root drags into a Home Assistant process.

``config_flow`` and the three device-automation modules import the package
only for ``DOMAIN``. Home Assistant loads them whenever a user opens the
add-integration dialog or the automation editor, on installs that may have
no Better Thermostat entry at all. Anything reachable from the package's
module-level imports is paid for on those paths, and the persistence stack
is the expensive branch: it pulls the calibration models and through them
numpy, hundreds of milliseconds on a Raspberry Pi.

The check runs in a fresh interpreter because the test session itself has
long since imported both.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "custom_components.better_thermostat"

#: The integration root and every module that imports it for ``DOMAIN`` alone.
DOMAIN_ONLY_CONSUMERS = (
    PACKAGE,
    f"{PACKAGE}.config_flow",
    f"{PACKAGE}.device_action",
    f"{PACKAGE}.device_binding",
    f"{PACKAGE}.device_condition",
    f"{PACKAGE}.device_trigger",
)

#: Must stay off the module-level import graph of everything above.
FORBIDDEN = (f"{PACKAGE}.utils.state_manager", "numpy")

_PROBE = """
import importlib, json, sys
for name in {targets!r}:
    importlib.import_module(name)
print(json.dumps([name for name in {forbidden!r} if name in sys.modules]))
"""


@pytest.fixture(scope="module")
def loaded_forbidden_modules() -> list[str]:
    """Import the DOMAIN-only modules in a fresh interpreter, report offenders."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.format(
                targets=list(DOMAIN_ONLY_CONSUMERS), forbidden=list(FORBIDDEN)
            ),
        ],
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
        env=env,
        text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_domain_only_modules_leave_persistence_unloaded(
    loaded_forbidden_modules: list[str],
) -> None:
    """Importing the package for DOMAIN alone does not load the state store."""
    assert f"{PACKAGE}.utils.state_manager" not in loaded_forbidden_modules


def test_domain_only_modules_leave_numpy_unloaded(
    loaded_forbidden_modules: list[str],
) -> None:
    """Importing the package for DOMAIN alone does not load numpy."""
    assert "numpy" not in loaded_forbidden_modules
