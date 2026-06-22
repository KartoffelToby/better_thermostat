"""Smoke tests for every controller adapter.

These verify that each adapter:
  - constructs cleanly with default params
  - returns a sensible BenchmarkOutput on first step
  - resets state without throwing
  - exports state as a dict
"""

from __future__ import annotations

import pytest

from tests.benchmark.adapters.base import BenchmarkContext, BenchmarkOutput
from tests.benchmark.adapters.baselines import (
    BangBangAdapter,
    IdealOracleAdapter,
    LinearPAdapter,
)
from tests.benchmark.adapters.heating_power_adapter import HeatingPowerAdapter
from tests.benchmark.adapters.mpc_adapter import MpcAdapter
from tests.benchmark.adapters.passive_modes import (
    AggressiveCalibrationAdapter,
    DefaultCalibrationAdapter,
    NoCalibrationAdapter,
)
from tests.benchmark.adapters.pid_adapter import PidAdapter
from tests.benchmark.adapters.tpi_adapter import TpiAdapter

_ADAPTERS = [
    pytest.param(MpcAdapter, id="mpc"),
    pytest.param(TpiAdapter, id="tpi"),
    pytest.param(PidAdapter, id="pid"),
    pytest.param(HeatingPowerAdapter, id="heating_power"),
    pytest.param(DefaultCalibrationAdapter, id="default"),
    pytest.param(AggressiveCalibrationAdapter, id="aggressive"),
    pytest.param(NoCalibrationAdapter, id="no_calibration"),
    pytest.param(BangBangAdapter, id="bangbang"),
    pytest.param(LinearPAdapter, id="linear_p"),
    pytest.param(IdealOracleAdapter, id="ideal_oracle"),
]


def _ctx(target: float = 21.0, current: float = 20.0) -> BenchmarkContext:
    return BenchmarkContext(
        t=0.0,
        dt=30.0,
        target_temp_C=target,
        current_temp_C=current,
        raw_room_temp_C=current,
        trv_temp_C=current,
        outdoor_temp_C=5.0,
    )


@pytest.mark.parametrize("adapter_cls", _ADAPTERS)
def test_adapter_constructs_with_defaults(adapter_cls):
    """Every adapter constructs cleanly with no arguments."""
    adapter = adapter_cls()
    assert adapter.name
    assert adapter.family in ("valve", "offset", "duty")


@pytest.mark.parametrize("adapter_cls", _ADAPTERS)
def test_adapter_step_returns_output(adapter_cls):
    """Calling step() returns a BenchmarkOutput with at least one populated field."""
    adapter = adapter_cls()
    out = adapter.step(_ctx())
    populated = (
        out.valve_percent is not None
        or out.setpoint_offset_K is not None
        or out.duty_cycle_pct is not None
    )
    assert populated, f"{adapter.name} produced an output with no populated field"


@pytest.mark.parametrize("adapter_cls", _ADAPTERS)
def test_adapter_reset_does_not_throw(adapter_cls):
    """reset() must not raise even when called repeatedly."""
    adapter = adapter_cls()
    adapter.reset()
    adapter.reset()
    adapter.reset()


@pytest.mark.parametrize("adapter_cls", _ADAPTERS)
def test_adapter_export_state_is_dict(adapter_cls):
    """export_state() returns a dict (possibly empty)."""
    adapter = adapter_cls()
    adapter.step(_ctx())
    snapshot = adapter.export_state()
    assert isinstance(snapshot, dict)


def test_error_drives_demand_for_feedback_controllers():
    """When current < setpoint, a P-controller and bang-bang command non-zero output."""
    for adapter_cls in (LinearPAdapter, BangBangAdapter):
        adapter = adapter_cls()
        out = adapter.step(_ctx(target=22.0, current=18.0))
        valve = out.valve_percent or 0.0
        assert valve > 0.0, f"{adapter_cls.__name__} should command heat when cold"


def test_benchmark_output_requires_exactly_one_family_field():
    """The output contract rejects empty and mixed-family outputs."""
    with pytest.raises(ValueError):
        BenchmarkOutput()
    with pytest.raises(ValueError):
        BenchmarkOutput(valve_percent=50.0, setpoint_offset_K=1.0)
    # The documented duty controllers' pairing stays allowed.
    out = BenchmarkOutput(duty_cycle_pct=40.0, valve_percent=40.0)
    assert out.duty_cycle_pct == 40.0


def test_state_backed_adapters_use_unique_default_keys():
    """Two default-keyed instances must not share controller state entries."""
    for cls in (PidAdapter, TpiAdapter, MpcAdapter):
        a, b = cls(), cls()
        assert a._key != b._key
    assert PidAdapter(key="shared")._key == "shared"


def test_oracle_feedback_uses_plant_truth():
    """The oracle's P-term reads the plant truth, not the lagged sensor."""
    adapter = IdealOracleAdapter()
    ctx = BenchmarkContext(
        t=0.0,
        dt=30.0,
        target_temp_C=21.0,
        current_temp_C=18.0,  # lagged/noisy sensor reading
        raw_room_temp_C=21.0,  # plant truth already at setpoint
        trv_temp_C=None,
        outdoor_temp_C=5.0,
    )
    out = adapter.step(ctx)
    assert out.diagnostics["error_K"] == pytest.approx(0.0)
