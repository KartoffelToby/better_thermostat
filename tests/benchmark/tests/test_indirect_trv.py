"""Indirect-TRV wrapper tests.

The wrapper translates an inner controller's valve-% intent through an
offset-mode TRV's own setpoint-quantisation + internal P-loop. These
tests verify each of those stages in isolation.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, override

import pytest

from tests.benchmark.adapters.base import (
    BenchmarkContext,
    BenchmarkOutput,
    ControllerFamily,
)
from tests.benchmark.adapters.indirect_trv import (
    BOSCH_PARAMS,
    SONOFF_TRVZB_PARAMS,
    TADO_PARAMS,
    TUYA_PARAMS,
    IndirectTrvAdapter,
    IndirectTrvParams,
)
from tests.benchmark.adapters.pid_adapter import PidAdapter
from tests.benchmark.runner import ADAPTER_FACTORIES


class _FakeValveAdapter:
    """Inner adapter whose valve demand is controlled by the test."""

    name = "fake"
    family: ControllerFamily = "valve"

    def __init__(self, percent: float, *, early_exit: bool = False) -> None:
        self.percent = percent
        self.early_exit = early_exit

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        _ = prior

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        _ = ctx
        if self.early_exit:
            return BenchmarkOutput(valve_percent=0.0, diagnostics={"early_exit": True})
        return BenchmarkOutput(valve_percent=self.percent)

    def export_state(self) -> dict[str, Any]:
        return {}


def _ctx(target: float = 21.0, current: float = 20.0) -> BenchmarkContext:
    return BenchmarkContext(
        t=0.0,
        dt=30.0,
        target_temperature=target,
        room_temperature=current,
        raw_room_temperature=current,
        trv_temperature=current,
        outdoor_temperature=5.0,
    )


def test_name_inherits_from_inner_with_suffix():
    """Name inherits from inner with suffix."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    assert adapter.name == "pid+indirect_trv"


def test_reset_clears_internal_caches():
    """Reset clears internal caches."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    adapter.step(_ctx(target=22.0, current=18.0))
    assert adapter._last_quantised_setpoint is not None
    adapter.reset()
    assert adapter._last_quantised_setpoint is None
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
    assert "indirect_setpoint" in out.diagnostics
    assert "indirect_quantised_diff_K" in out.diagnostics


def test_export_state_includes_inner_and_setpoint():
    """Export state includes inner and setpoint."""
    adapter = IndirectTrvAdapter(PidAdapter(), TADO_PARAMS)
    adapter.step(_ctx(target=22.0, current=18.0))
    snapshot = adapter.export_state()
    assert "inner" in snapshot
    assert "last_quantised_setpoint" in snapshot
    assert snapshot["last_quantised_setpoint"] is not None


def test_quantisation_to_setpoint_step():
    """Tuya params (1 K step) should produce integer-K setpoints."""
    adapter = IndirectTrvAdapter(PidAdapter(), TUYA_PARAMS)
    out = adapter.step(_ctx(target=21.0, current=18.0))
    sp = out.diagnostics["indirect_setpoint"]
    # 1 K quantisation → setpoint is an integer.
    assert abs(sp - round(sp)) < 1e-6


def test_hysteresis_holds_old_setpoint_inside_band():
    """Hysteresis holds old setpoint inside band."""
    # Tight hysteresis on a 0.5 K step: a tiny u-change inside the
    # hysteresis band must hold the previous setpoint.
    params = IndirectTrvParams(
        setpoint_step_K=0.5,
        internal_hysteresis_K=2.0,
        internal_p_gain=30.0,
        setpoint_mapping="heuristic",
    )
    adapter = IndirectTrvAdapter(PidAdapter(), params)
    adapter.step(_ctx(target=22.0, current=18.0))  # big jump first
    first_sp = adapter._last_quantised_setpoint
    # A slightly different target shifts the raw quantised setpoint, but the
    # hysteresis band (2.0 K) must still hold the previous setpoint. Without
    # hysteresis this second call would change the setpoint, so the assertion
    # only passes when the internal hysteresis logic is actually exercised.
    adapter.step(_ctx(target=23.0, current=18.0))
    assert adapter._last_quantised_setpoint == first_sp


def test_command_latency_delays_setpoint_change():
    """A setpoint change reaches the TRV only after command_latency_steps."""
    params = IndirectTrvParams(
        setpoint_step_K=0.5,
        internal_hysteresis_K=0.0,
        internal_p_gain=30.0,
        command_latency_steps=3,
        setpoint_mapping="heuristic",
    )
    inner = _FakeValveAdapter(0.0)
    adapter = IndirectTrvAdapter(inner, params)
    # Saturate the FIFO with the all-closed command (setpoint == target).
    out = adapter.step(_ctx(target=22.0, current=18.0))
    for _ in range(4):
        out = adapter.step(_ctx(target=22.0, current=18.0))
    old_sp = out.diagnostics["indirect_setpoint"]
    assert old_sp == pytest.approx(22.0)

    # Inner controller now demands full heat → new setpoint target+headroom.
    inner.percent = 100.0
    for _ in range(params.command_latency_steps):
        out = adapter.step(_ctx(target=22.0, current=18.0))
        assert out.diagnostics["indirect_setpoint"] == pytest.approx(old_sp)
    out = adapter.step(_ctx(target=22.0, current=18.0))
    assert out.diagnostics["indirect_setpoint"] == pytest.approx(
        22.0 + params.max_calibration_headroom_K
    )
    assert len(adapter._pending_setpoints) <= params.command_latency_steps + 1


def test_inversion_mapping_uses_current_temperature():
    """Inversion mapping uses current temperature."""
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
    sp = out.diagnostics["indirect_setpoint"]
    assert sp >= 19.0


def test_heuristic_mapping_uses_target_temperature():
    """Heuristic mapping uses target temperature."""
    params = IndirectTrvParams(
        setpoint_step_K=0.5,
        internal_hysteresis_K=0.0,
        internal_p_gain=30.0,
        setpoint_mapping="heuristic",
        max_calibration_headroom_K=5.0,
    )
    adapter = IndirectTrvAdapter(PidAdapter(), params)
    out = adapter.step(_ctx(target=21.0, current=19.0))
    sp = out.diagnostics["indirect_setpoint"]
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
    # lands at/near room temperature, so internal P-loop produces ~0 %.
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

    assert snapshot["last_inner_valve_pct"] == 100.0

    adapter.reset(snapshot)
    assert adapter._last_quantised_setpoint == snapshot["last_quantised_setpoint"]
    assert adapter._pending_setpoints == snapshot["pending_setpoints"]
    assert adapter._last_inner_valve_percent == 100.0

    adapter.reset()
    assert adapter._last_quantised_setpoint is None
    assert adapter._pending_setpoints == []
    assert adapter._last_inner_valve_percent == 0.0


class _FakeOffsetAdapter:
    """Inner adapter that emits a non-valve (offset) output."""

    name = "fake_offset"
    family: ControllerFamily = "offset"

    def reset(self, prior: dict[str, object] | None = None) -> None:
        _ = prior

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        _ = ctx
        return BenchmarkOutput(setpoint_offset_K=1.0)

    def export_state(self) -> dict[str, object]:
        return {}


def test_indirect_params_rejects_invalid_mapping():
    """An unknown setpoint_mapping is rejected at construction."""
    with pytest.raises(ValueError):
        IndirectTrvParams(setpoint_mapping="bogus")


@pytest.mark.parametrize(
    "fields",
    [
        {"min_setpoint": 30.0, "max_setpoint": 30.0},
        {"trv_sensor_rad_fraction": -0.1},
        {"trv_sensor_rad_fraction": 1.1},
    ],
)
def test_indirect_params_rejects_non_physical_trv_fields(fields):
    """An empty setpoint range or a sensor fraction outside [0, 1] is rejected."""
    with pytest.raises(ValueError):
        IndirectTrvParams(**fields)


# --- production mapping -------------------------------------------------


class _RecordingValveAdapter(_FakeValveAdapter):
    """Fake inner adapter that also records the contexts it was handed."""

    def __init__(self, percent: float) -> None:
        super().__init__(percent)
        self.seen: list[BenchmarkContext] = []

    @override
    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        self.seen.append(ctx)
        return super().step(ctx)


_PRODUCTION = IndirectTrvParams(
    setpoint_step_K=0.5,
    internal_hysteresis_K=0.0,
    internal_p_gain=30.0,
    min_setpoint=5.0,
    max_setpoint=30.0,
    trv_sensor_rad_fraction=0.1,
)


def _plant_ctx(
    *,
    target: float = 21.0,
    room: float = 20.0,
    rad: float | None = 40.0,
    last_valve: float = 0.0,
) -> BenchmarkContext:
    return BenchmarkContext(
        t=0.0,
        dt=30.0,
        target_temperature=target,
        room_temperature=room,
        raw_room_temperature=room,
        trv_temperature=rad,
        outdoor_temperature=5.0,
        last_valve_percent=last_valve,
    )


def test_production_trv_reads_a_share_of_the_radiator_excess():
    """The TRV reads room + fraction·(radiator − room), and the inner controller sees it."""
    inner = _RecordingValveAdapter(50.0)
    IndirectTrvAdapter(inner, _PRODUCTION).step(_plant_ctx(room=20.0, rad=40.0))
    assert inner.seen[0].trv_temperature == pytest.approx(22.0)


def test_production_trv_reads_the_room_without_a_radiator_temperature():
    """A plant without a radiator state leaves the TRV reading the room."""
    inner = _RecordingValveAdapter(50.0)
    IndirectTrvAdapter(inner, _PRODUCTION).step(_plant_ctx(room=20.0, rad=None))
    assert inner.seen[0].trv_temperature == pytest.approx(20.0)


def test_production_full_demand_sends_the_maximum_setpoint():
    """u = 100 % scales the setpoint all the way to the TRV's maximum."""
    adapter = IndirectTrvAdapter(_FakeValveAdapter(100.0), _PRODUCTION)
    out = adapter.step(_plant_ctx())
    assert out.diagnostics["indirect_setpoint"] == pytest.approx(30.0)
    assert out.valve_percent == 100.0


def test_production_partial_demand_scales_between_reading_and_maximum():
    """u scales the setpoint from the TRV reading to the maximum, rounded up."""
    # Reading 22.0; 22.0 + (30.0 - 22.0)·0.33 = 24.64 → rounded up to 25.0.
    adapter = IndirectTrvAdapter(_FakeValveAdapter(33.0), _PRODUCTION)
    out = adapter.step(_plant_ctx(room=20.0, rad=40.0))
    assert out.diagnostics["indirect_setpoint"] == pytest.approx(25.0)
    assert out.valve_percent == pytest.approx(30.0 * (25.0 - 22.0))


def test_production_zero_demand_pushes_the_setpoint_below_the_reading():
    """u = 0 without overshoot sends the reading minus one step, rounded down."""
    # Reading 22.2 → 22.2 - 0.5 = 21.7 → rounded down to 21.5.
    adapter = IndirectTrvAdapter(_FakeValveAdapter(0.0), _PRODUCTION)
    out = adapter.step(_plant_ctx(target=21.0, room=20.0, rad=42.0))
    assert out.diagnostics["indirect_setpoint"] == pytest.approx(21.5)
    assert out.valve_percent == 0.0


def test_production_zero_demand_backs_off_further_with_overshoot():
    """The further the room overshoots, the further below the reading u = 0 goes."""
    adapter = IndirectTrvAdapter(_FakeValveAdapter(0.0), _PRODUCTION)
    small = adapter.step(_plant_ctx(target=21.0, room=21.0, rad=21.0))
    adapter.reset()
    large = adapter.step(_plant_ctx(target=21.0, room=23.0, rad=23.0))
    small_gap = 21.0 - small.diagnostics["indirect_setpoint"]
    large_gap = 23.0 - large.diagnostics["indirect_setpoint"]
    assert small_gap == pytest.approx(0.5)
    # max_offset = 23 - 5 = 18; 18·(1 - e^-1) = 11.38 → 23 - 11.38 rounded down.
    assert large.diagnostics["indirect_setpoint"] == pytest.approx(11.5)
    assert large_gap > small_gap


def test_production_setpoint_stays_inside_the_trv_range():
    """A large overshoot must keep the setpoint within the TRV's range."""
    # A minimum off the 0.5 K grid: rounding down alone would land on 4.5.
    params = IndirectTrvParams(
        setpoint_step_K=0.5, internal_hysteresis_K=0.0, min_setpoint=4.8
    )
    adapter = IndirectTrvAdapter(_FakeValveAdapter(0.0), params)
    out = adapter.step(_plant_ctx(target=6.0, room=20.0, rad=20.0))
    assert out.diagnostics["indirect_setpoint"] >= 4.8


@pytest.mark.parametrize(
    ("demand", "low", "high", "expected"),
    [
        pytest.param(100.0, 5.0, 30.3, 30.3, id="max-off-grid"),
        pytest.param(0.0, 4.6, 30.0, 4.6, id="min-off-grid"),
    ],
)
def test_production_setpoint_holds_an_off_grid_range_edge(
    demand: float, low: float, high: float, expected: float
):
    """The TRV receives a setpoint inside its range when an edge lies off the grid."""
    # 30.3 and 4.6 round to the nearest 0.5 K step at 30.5 and 4.5, both
    # outside the range.
    params = replace(_PRODUCTION, min_setpoint=low, max_setpoint=high)
    adapter = IndirectTrvAdapter(_FakeValveAdapter(demand), params)
    out = adapter.step(_plant_ctx(target=6.0, room=20.0, rad=20.0))
    setpoint = out.diagnostics["indirect_setpoint"]
    assert params.min_setpoint <= setpoint <= params.max_setpoint
    assert setpoint == pytest.approx(expected)


def test_production_inner_controller_sees_its_own_previous_command():
    """Without a reported position, the previous valve is BT's own command."""
    inner = _RecordingValveAdapter(40.0)
    adapter = IndirectTrvAdapter(inner, _PRODUCTION)
    adapter.step(_plant_ctx(last_valve=0.0))
    adapter.step(_plant_ctx(last_valve=90.0))
    assert inner.seen[1].last_valve_percent == pytest.approx(40.0)


def test_production_inner_controller_sees_the_reported_valve():
    """With a reported position, the previous valve is the one the TRV chose."""
    params = IndirectTrvParams(
        setpoint_step_K=0.5, internal_hysteresis_K=0.0, reports_valve_position=True
    )
    inner = _RecordingValveAdapter(40.0)
    adapter = IndirectTrvAdapter(inner, params)
    adapter.step(_plant_ctx(last_valve=0.0))
    adapter.step(_plant_ctx(last_valve=90.0))
    assert inner.seen[1].last_valve_percent == pytest.approx(90.0)


def test_heuristic_mapping_hands_the_inner_controller_the_context_unchanged():
    """The heuristic and inversion mappings leave the inner context untouched."""
    params = IndirectTrvParams(setpoint_mapping="heuristic")
    inner = _RecordingValveAdapter(40.0)
    ctx = _plant_ctx(rad=40.0, last_valve=90.0)
    IndirectTrvAdapter(inner, params).step(ctx)
    assert inner.seen[0] == ctx


def test_indirect_rejects_missing_inner_valve():
    """Wrapping a non-valve inner controller fails fast instead of coercing to 0%."""
    wrapper = IndirectTrvAdapter(_FakeOffsetAdapter(), TADO_PARAMS)
    with pytest.raises(ValueError):
        wrapper.step(_ctx())


def test_inner_early_exit_closes_the_valve_below_target():
    """An inner early exit closes the valve though the room is below target."""
    adapter = IndirectTrvAdapter(_FakeValveAdapter(0.0, early_exit=True), TADO_PARAMS)
    out = adapter.step(_ctx(target=22.0, current=18.0))
    assert out.valve_percent == 0.0
    assert out.diagnostics["early_exit"] is True


def test_open_window_closes_the_valve_on_a_zero_intent():
    """An open window closes the valve even without an inner early exit."""
    adapter = IndirectTrvAdapter(_FakeValveAdapter(0.0), TADO_PARAMS)
    out = adapter.step(replace(_ctx(target=22.0, current=18.0), window_open=True))
    assert out.valve_percent == 0.0


def test_inner_early_exit_keeps_the_trv_setpoint_cache():
    """A stood-down step pushes no setpoint, so the TRV cache stays put."""
    inner = _FakeValveAdapter(50.0)
    adapter = IndirectTrvAdapter(inner, BOSCH_PARAMS)
    adapter.step(_ctx(target=22.0, current=18.0))
    last = adapter._last_quantised_setpoint
    pending = list(adapter._pending_setpoints)
    inner.early_exit = True
    adapter.step(_ctx(target=22.0, current=18.0))
    assert adapter._last_quantised_setpoint == last
    assert adapter._pending_setpoints == pending


@pytest.mark.parametrize(
    "name", sorted(n for n in ADAPTER_FACTORIES if "+indirect_" in n)
)
def test_indirect_variants_do_not_heat_while_the_window_is_open(name: str):
    """Every indirect registration closes the valve on an open window."""
    adapter = ADAPTER_FACTORIES[name]()
    out = adapter.step(
        replace(
            _ctx(target=22.0, current=18.0), window_open=True, last_valve_percent=60.0
        )
    )
    assert out.valve_percent == 0.0
