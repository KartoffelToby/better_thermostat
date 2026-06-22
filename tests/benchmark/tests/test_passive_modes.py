"""Tests for BT's offset-family calibration modes."""

from __future__ import annotations

import pytest

from tests.benchmark.adapters.base import BenchmarkContext
from tests.benchmark.adapters.passive_modes import (
    AggressiveCalibrationAdapter,
    DefaultCalibrationAdapter,
    NoCalibrationAdapter,
    PassiveModeParams,
)


def _ctx(
    target: float = 21.0, current: float = 20.0, trv: float | None = 20.0
) -> BenchmarkContext:
    return BenchmarkContext(
        t=0.0,
        dt=30.0,
        target_temp_C=target,
        current_temp_C=current,
        raw_room_temp_C=current,
        trv_temp_C=trv,
        outdoor_temp_C=5.0,
    )


# -- DefaultCalibrationAdapter ------------------------------------------------


def test_default_zero_demand_yields_zero_valve():
    """At-target → zero valve."""
    out = DefaultCalibrationAdapter().step(_ctx(target=20.0, current=20.0))
    assert out.valve_percent == 0.0


def test_default_negative_error_clamps_to_zero():
    """Room warmer than target → clamped to zero."""
    out = DefaultCalibrationAdapter().step(_ctx(target=20.0, current=22.0))
    assert out.valve_percent == 0.0


def test_default_proportional_to_error():
    """Valve = 30 %/K · error, e.g. 1 K → 30 %."""
    out = DefaultCalibrationAdapter().step(_ctx(target=21.0, current=20.0))
    assert out.valve_percent == pytest.approx(30.0)


def test_default_saturates_at_100():
    """Large error clamps to 100 %."""
    out = DefaultCalibrationAdapter().step(_ctx(target=25.0, current=20.0))
    assert out.valve_percent == 100.0


def test_default_ignores_trv_temp():
    """DEFAULT regulates against the external sensor, not the TRV body."""
    # External says 20, TRV body says 25. DEFAULT must use the external value.
    out = DefaultCalibrationAdapter().step(_ctx(target=21.0, current=20.0, trv=25.0))
    assert out.valve_percent == pytest.approx(30.0)


def test_default_custom_gain():
    """Custom p_gain overrides the default 30 %/K."""
    adapter = DefaultCalibrationAdapter(PassiveModeParams(p_gain=15.0))
    out = adapter.step(_ctx(target=21.0, current=20.0))
    assert out.valve_percent == pytest.approx(15.0)


# -- AggressiveCalibrationAdapter --------------------------------------------


def test_aggressive_zero_error_yields_zero_valve():
    """At-target → no boost, valve = 0."""
    out = AggressiveCalibrationAdapter().step(_ctx(target=20.0, current=20.0))
    assert out.valve_percent == 0.0


def test_aggressive_negative_error_clamps_to_zero():
    """Above target → clamped to 0 (no boost when not heating)."""
    out = AggressiveCalibrationAdapter().step(_ctx(target=20.0, current=22.0))
    assert out.valve_percent == 0.0


def test_aggressive_adds_2_5_K_boost_while_heating():
    """1 K error + 2.5 K boost = 30 + 75 = 105 → clamped to 100."""
    out = AggressiveCalibrationAdapter().step(_ctx(target=21.0, current=20.0))
    assert out.valve_percent == 100.0


def test_aggressive_small_error_with_boost_still_in_range():
    """Even a tiny error gets the boost: 0.1 K → 3 + 75 = 78 %."""
    out = AggressiveCalibrationAdapter().step(_ctx(target=20.1, current=20.0))
    assert out.valve_percent == pytest.approx(78.0)


def test_aggressive_diagnostics_expose_boost_amount():
    """Diagnostics include the boost contribution."""
    out = AggressiveCalibrationAdapter().step(_ctx(target=21.0, current=20.0))
    assert out.diagnostics["boost_pct"] == pytest.approx(75.0)
    out_off = AggressiveCalibrationAdapter().step(_ctx(target=20.0, current=22.0))
    assert out_off.diagnostics["boost_pct"] == 0.0


def test_aggressive_reset_clears_was_heating():
    """Reset flips the latched ``was_heating`` flag back off."""
    adapter = AggressiveCalibrationAdapter()
    adapter.step(_ctx(target=21.0, current=20.0))
    assert adapter.export_state()["was_heating"]
    adapter.reset()
    assert not adapter.export_state()["was_heating"]


# -- NoCalibrationAdapter ----------------------------------------------------


def test_no_calibration_uses_trv_internal_sensor():
    """NO_CALIBRATION regulates against ``trv_temp_C``, not the room sensor."""
    # Room says 20 (cold), TRV body says 22 (warm radiator backsplash).
    # The TRV's P-loop sees target − trv = 21 − 22 = −1 → clamped to 0 %.
    out = NoCalibrationAdapter().step(_ctx(target=21.0, current=20.0, trv=22.0))
    assert out.valve_percent == 0.0


def test_no_calibration_falls_back_to_external_when_trv_missing():
    """Sensorless plant variant: fall back to room reading."""
    out = NoCalibrationAdapter().step(_ctx(target=21.0, current=20.0, trv=None))
    assert out.valve_percent == pytest.approx(30.0)


def test_no_calibration_proportional_to_trv_error():
    """1 K of TRV-internal error → 30 %."""
    out = NoCalibrationAdapter().step(_ctx(target=21.0, current=20.0, trv=20.0))
    assert out.valve_percent == pytest.approx(30.0)


def test_no_calibration_diagnostics_show_which_temp_was_used():
    """``trv_temp_used_C`` reflects the actual reference temperature."""
    out = NoCalibrationAdapter().step(_ctx(target=21.0, current=20.0, trv=20.5))
    assert out.diagnostics["trv_temp_used_C"] == 20.5


# -- Cross-cutting -----------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_cls",
    [DefaultCalibrationAdapter, AggressiveCalibrationAdapter, NoCalibrationAdapter],
)
def test_all_passive_adapters_have_valve_family(adapter_cls):
    """All three modes are valve-family controllers."""
    assert adapter_cls().family == "valve"


@pytest.mark.parametrize(
    "adapter_cls,expected_name",
    [
        (DefaultCalibrationAdapter, "default"),
        (AggressiveCalibrationAdapter, "aggressive"),
        (NoCalibrationAdapter, "no_calibration"),
    ],
)
def test_adapter_names_match_calibration_mode(adapter_cls, expected_name):
    """Adapter name matches its production CalibrationMode key."""
    assert adapter_cls().name == expected_name


@pytest.mark.parametrize(
    "adapter_cls",
    [DefaultCalibrationAdapter, AggressiveCalibrationAdapter, NoCalibrationAdapter],
)
def test_export_state_is_dict(adapter_cls):
    """``export_state`` returns a serializable dict."""
    adapter = adapter_cls()
    adapter.step(_ctx())
    assert isinstance(adapter.export_state(), dict)
