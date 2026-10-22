"""A full-open valve cap leaves every controller's output unchanged.

The benchmark adapters pass no maximum opening, so they run on the
controllers' default cap. Production always passes the cap the user set,
which is 100 % unless lowered. Both runs must command the same valve on
every cycle, including the cycles where the valve sits fully open and the
MPC v2 anti-windup checks the upper rail.
"""

from __future__ import annotations

from functools import partial

import pytest

from custom_components.better_thermostat.utils.calibration.mpc import MpcInput
from custom_components.better_thermostat.utils.calibration.mpc_v2 import MpcV2Input
from custom_components.better_thermostat.utils.calibration.pid import compute_pid
from tests.benchmark.adapters import mpc_adapter, mpc_v2_adapter, pid_adapter
from tests.benchmark.adapters.base import (
    BenchmarkContext,
    BenchmarkOutput,
    ControllerAdapter,
)
from tests.benchmark.runner import run_scenario
from tests.benchmark.scenarios import (
    S02_SETPOINT_STEP_LARGE,
    S03_FROST_TO_COMFORT,
    ScenarioConfig,
)


def _valve_trace(
    monkeypatch: pytest.MonkeyPatch,
    adapter: ControllerAdapter,
    scenario: ScenarioConfig,
) -> list[float | None]:
    """Run ``scenario`` and return the valve percent of every controller step."""
    trace: list[float | None] = []
    step = adapter.step

    def recording_step(ctx: BenchmarkContext) -> BenchmarkOutput:
        out = step(ctx)
        trace.append(out.valve_percent)
        return out

    monkeypatch.setattr(adapter, "step", recording_step)
    run_scenario(adapter, scenario)
    return trace


_CASES = [
    pytest.param(
        mpc_adapter.MpcAdapter,
        "MpcInput",
        partial(MpcInput, max_opening_percent=100.0),
        id="mpc",
    ),
    pytest.param(
        mpc_v2_adapter.MpcV2Adapter,
        "MpcV2Input",
        partial(MpcV2Input, max_opening_percent=100.0),
        id="mpc_v2",
    ),
    pytest.param(
        pid_adapter.PidAdapter,
        "compute_pid",
        partial(compute_pid, max_opening_percent=100.0),
        id="pid",
    ),
]

_MODULES = {
    "MpcInput": mpc_adapter,
    "MpcV2Input": mpc_v2_adapter,
    "compute_pid": pid_adapter,
}


@pytest.mark.parametrize("scenario", [S02_SETPOINT_STEP_LARGE, S03_FROST_TO_COMFORT])
@pytest.mark.parametrize(("adapter_cls", "name", "capped"), _CASES)
def test_full_cap_matches_the_default_cap(
    monkeypatch: pytest.MonkeyPatch,
    adapter_cls: type[ControllerAdapter],
    name: str,
    capped: object,
    scenario: ScenarioConfig,
) -> None:
    """Passing 100 % commands the same valve as passing no cap, cycle by cycle."""
    default_trace = _valve_trace(monkeypatch, adapter_cls(), scenario)

    monkeypatch.setattr(_MODULES[name], name, capped)
    capped_trace = _valve_trace(monkeypatch, adapter_cls(), scenario)

    assert 100.0 in default_trace
    assert capped_trace == default_trace
