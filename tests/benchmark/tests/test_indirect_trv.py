"""Indirect-TRV wrapper tests.

The wrapper translates an inner controller's valve-% intent through an
offset-mode TRV's own setpoint-quantisation + internal P-loop. These
tests verify each of those stages in isolation.
"""

from __future__ import annotations

import pytest

from tests.benchmark.adapters.base import BenchmarkContext, BenchmarkOutput
from tests.benchmark.adapters.indirect_trv import (
    BOSCH_PARAMS,
    SONOFF_TRVZB_PARAMS,
    TADO_PARAMS,
    TUYA_PARAMS,
    IndirectTrvAdapter,
    IndirectTrvParams,
)
from tests.benchmark.adapters.pid_adapter import PidAdapter


class _FakeValveAdapter:
    """Inner adapter whose valve demand is controlled by the test."""

    name = "fake"
    family = "valve"

    def __init__(self, pct: float) -> None:
        self.pct = pct

    def reset(self, prior=None) -> None:
        _ = prior

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        _ = ctx
        return BenchmarkOutput(valve_percent=self.pct)

    def export_state(self) -> dict:
        return {}


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


def test_name_inherits_from_inner_with_suffix():
    """Name inherits from inner with suffix."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    assert adapter.name == "pid+indirect_trv"


def test_reset_clears_internal_caches():
    """Reset clears internal caches."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    adapter.step(_ctx(target=22.0, current=18.0))
    assert adapter._last_quantised_setpoint_C is not None
    adapter.reset()
    assert adapter._last_quantised_setpoint_C is None
    assert adapter._pending_setpoints == []


def test_step_returns_valve_in_range():
    """Step returns valve in range."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    out = adapter.step(_ctx(target=22.0, current=18.0))
    assert out.valve_percent is not None
    assert 0.0 <= out.valve_percent <= 100.0


def test_diagnostics_include_indirect_keys():
    """Diagnostics include indirect keys."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    out = adapter.step(_ctx(target=21.0, current=20.0))
    assert "indirect_setpoint_C" in out.diagnostics
    assert "indirect_quantised_diff_K" in out.diagnostics


def test_export_state_includes_inner_and_setpoint():
    """Export state includes inner and setpoint."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    adapter.step(_ctx(target=22.0, current=18.0))
    snapshot = adapter.export_state()
    assert "inner" in snapshot
    assert "last_quantised_setpoint_C" in snapshot
    assert snapshot["last_quantised_setpoint_C"] is not None


def test_quantisation_to_setpoint_step():
    """Tuya params (1 K step) should produce integer-K setpoints."""
    adapter = IndirectTrvAdapter(PidAdapter(), TUYA_PARAMS)
    out = adapter.step(_ctx(target=21.0, current=18.0))
    sp = out.diagnostics["indirect_setpoint_C"]
    # 1 K quantisation → setpoint is an integer.
    assert abs(sp - round(sp)) < 1e-6


def test_hysteresis_holds_old_setpoint_inside_band():
    """Hysteresis holds old setpoint inside band."""
    # Tight hysteresis on a 0.5 K step: a tiny u-change inside the
    # hysteresis band must hold the previous setpoint.
    params = IndirectTrvParams(
        setpoint_step_K=0.5, internal_hysteresis_K=2.0, internal_p_gain=30.0
    )
    adapter = IndirectTrvAdapter(PidAdapter(), params)
    adapter.step(_ctx(target=22.0, current=18.0))  # big jump first
    first_sp = adapter._last_quantised_setpoint_C
    # Same call → setpoint must stay (within hysteresis).
    adapter.step(_ctx(target=22.0, current=18.0))
    assert adapter._last_quantised_setpoint_C == first_sp


def test_command_latency_delays_setpoint_change():
    """A setpoint change reaches the TRV only after command_latency_steps."""
    params = IndirectTrvParams(
        setpoint_step_K=0.5,
        internal_hysteresis_K=0.0,
        internal_p_gain=30.0,
        command_latency_steps=3,
    )
    inner = _FakeValveAdapter(0.0)
    adapter = IndirectTrvAdapter(inner, params)
    # Saturate the FIFO with the all-closed command (setpoint == target).
    out = adapter.step(_ctx(target=22.0, current=18.0))
    for _ in range(4):
        out = adapter.step(_ctx(target=22.0, current=18.0))
    old_sp = out.diagnostics["indirect_setpoint_C"]
    assert old_sp == pytest.approx(22.0)

    # Inner controller now demands full heat → new setpoint target+headroom.
    inner.pct = 100.0
    for _ in range(params.command_latency_steps):
        out = adapter.step(_ctx(target=22.0, current=18.0))
        assert out.diagnostics["indirect_setpoint_C"] == pytest.approx(old_sp)
    out = adapter.step(_ctx(target=22.0, current=18.0))
    assert out.diagnostics["indirect_setpoint_C"] == pytest.approx(
        22.0 + params.max_calibration_headroom_K
    )
    assert len(adapter._pending_setpoints) <= params.command_latency_steps + 1


def test_inversion_mapping_uses_current_temp():
    """Inversion mapping uses current temp."""
    params = IndirectTrvParams(
        setpoint_step_K=0.5,
        internal_hysteresis_K=0.0,
        internal_p_gain=30.0,
        setpoint_mapping="inversion",
    )
    adapter = IndirectTrvAdapter(PidAdapter(), params)
    out = adapter.step(_ctx(target=21.0, current=19.0))
    # Inversion ⇒ setpoint = current + bt_u/p_gain. Should be in a
    # plausible range above current_temp.
    sp = out.diagnostics["indirect_setpoint_C"]
    assert sp >= 19.0


def test_heuristic_mapping_uses_target_temp():
    """Heuristic mapping uses target temp."""
    params = IndirectTrvParams(
        setpoint_step_K=0.5,
        internal_hysteresis_K=0.0,
        internal_p_gain=30.0,
        setpoint_mapping="heuristic",
        max_calibration_headroom_K=5.0,
    )
    adapter = IndirectTrvAdapter(PidAdapter(), params)
    out = adapter.step(_ctx(target=21.0, current=19.0))
    sp = out.diagnostics["indirect_setpoint_C"]
    # Heuristic ⇒ setpoint = target + headroom · u/100 ∈ [target, target+headroom].
    assert 21.0 - 0.5 <= sp <= 21.0 + 5.0 + 0.5


def test_p_gain_clamps_internal_command_to_0_to_100():
    """Very high error must clamp the internal valve command to 100 %."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    out = adapter.step(_ctx(target=30.0, current=10.0))  # 20 K error
    assert out.valve_percent is not None
    assert out.valve_percent == 100.0


def test_zero_error_yields_zero_valve():
    """Zero error yields zero valve."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    # Inner PID with target == current → zero demand; quantised setpoint
    # lands at/near room temp, so internal P-loop produces ~0 %.
    out = adapter.step(_ctx(target=20.0, current=20.0))
    assert out.valve_percent is not None
    assert out.valve_percent == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize(
    "preset", [TADO_PARAMS, BOSCH_PARAMS, TUYA_PARAMS, SONOFF_TRVZB_PARAMS]
)
def test_every_vendor_preset_drives_a_full_step_cleanly(preset):
    """Every vendor preset drives a full step cleanly."""
    adapter = IndirectTrvAdapter(PidAdapter(), preset)
    out = adapter.step(_ctx(target=22.0, current=19.0))
    assert out.valve_percent is not None
    assert 0.0 <= out.valve_percent <= 100.0


def test_reset_restores_exported_state():
    """``reset(export_state())`` restores the wrapper's TRV-layer cache."""
    params = IndirectTrvParams(
        setpoint_step_K=0.5,
        internal_hysteresis_K=0.0,
        internal_p_gain=30.0,
        command_latency_steps=2,
    )
    adapter = IndirectTrvAdapter(_FakeValveAdapter(100.0), params)
    for _ in range(3):
        adapter.step(_ctx(target=22.0, current=18.0))
    snapshot = adapter.export_state()
    assert snapshot["pending_setpoints"]

    adapter.reset(snapshot)
    assert adapter._last_quantised_setpoint_C == snapshot["last_quantised_setpoint_C"]
    assert adapter._pending_setpoints == snapshot["pending_setpoints"]

    adapter.reset()
    assert adapter._last_quantised_setpoint_C is None
    assert adapter._pending_setpoints == []


class _FakeOffsetAdapter:
    """Inner adapter that emits a non-valve (offset) output."""

    name = "fake_offset"
    family = "offset"

    def reset(self, prior=None) -> None:
        _ = prior

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        _ = ctx
        return BenchmarkOutput(setpoint_offset_K=1.0)

    def export_state(self) -> dict:
        return {}


def test_indirect_params_rejects_invalid_mapping():
    """An unknown setpoint_mapping is rejected at construction."""
    with pytest.raises(ValueError):
        IndirectTrvParams(setpoint_mapping="bogus")


def test_indirect_rejects_missing_inner_valve():
    """Wrapping a non-valve inner controller fails fast instead of coercing to 0%."""
    wrapper = IndirectTrvAdapter(_FakeOffsetAdapter(), TADO_PARAMS)
    with pytest.raises(ValueError):
        wrapper.step(_ctx())
