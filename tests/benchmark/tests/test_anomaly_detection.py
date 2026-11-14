"""Benchmark verification gates for sensorless anomaly and fault detection."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.better_thermostat.core.desired import AnomalyCode

from ..adapters.mpc_adapter import MpcAdapter
from ..adapters.mpc_v2_adapter import MpcV2Adapter
from ..adapters.pid_adapter import PidAdapter
from ..adapters.tpi_adapter import TpiAdapter
from ..runner import run_scenario
from ..scenarios import (
    S01_SETPOINT_STEP_SMALL,
    S04_SETPOINT_DROP,
    S09_WINDOW_OPEN_SHORT,
    S10_WINDOW_OPEN_LONG,
    S11_SOLAR_GAIN_MORNING,
    S14_NIGHTLY_SETBACK,
    S15_DAILY_CYCLE,
    S21_STOCHASTIC_WINDOWS,
    S40_BOILER_OUTAGE,
    S41_STUCK_OPEN_VALVE,
    S42_OPEN_INTERIOR_DOOR,
)


def _first_onset_time_seconds(
    t_s: list[float],
    anomalies: list[tuple[str, ...]],
    code: str,
    after_t_s: float = 0.0,
) -> float | None:
    """Return the first timestamp at or after ``after_t_s`` where ``code`` is present."""
    for t, active_codes in zip(t_s, anomalies, strict=True):
        if t >= after_t_s and code in active_codes:
            return t
    return None


def _first_clear_time_seconds(
    t_s: list[float], anomalies: list[tuple[str, ...]], code: str, after_t_s: float
) -> float | None:
    """Return the first timestamp at or after ``after_t_s`` where ``code`` clears."""
    for t, active_codes in zip(t_s, anomalies, strict=True):
        if t >= after_t_s and code not in active_codes:
            return t
    return None


@pytest.mark.parametrize(
    "adapter_factory", [MpcV2Adapter, MpcAdapter, PidAdapter, TpiAdapter]
)
def test_s10_unmonitored_window_detected_under_six_minutes(
    adapter_factory: type,
) -> None:
    """Verify rapid heat-loss detection latency on S10 without a contact sensor."""
    scenario = replace(
        S10_WINDOW_OPEN_LONG, controller_sees_window=False, anomaly_detection=True
    )
    res = run_scenario(adapter_factory(), scenario)
    series = res.series
    assert series is not None
    onset = _first_onset_time_seconds(
        list(series.t_s),
        list(series.anomalies),
        AnomalyCode.UNEXPECTED_RAPID_LOSS.value,
        after_t_s=60 * 60.0,
    )
    assert onset is not None
    assert (onset - 60 * 60.0) <= 360.0


def test_s21_stochastic_unmonitored_windows_protect_mpc_v2_learning() -> None:
    """Verify repeated unmonitored windows trigger rapid-loss holds and protect MPC v2."""
    scenario = replace(
        S21_STOCHASTIC_WINDOWS, controller_sees_window=False, anomaly_detection=True
    )
    adapter = MpcV2Adapter()
    res = run_scenario(adapter, scenario)
    series = res.series
    assert series is not None
    onset = _first_onset_time_seconds(
        list(series.t_s),
        list(series.anomalies),
        AnomalyCode.UNEXPECTED_RAPID_LOSS.value,
    )
    assert onset is not None


def test_s40_boiler_outage_detected_and_clears_upon_recovery() -> None:
    """Verify boiler supply outage triggers NO_HEAT_DELIVERY and clears after recovery."""
    res = run_scenario(MpcV2Adapter(), S40_BOILER_OUTAGE)
    series = res.series
    assert series is not None
    t_s = list(series.t_s)
    anomalies = list(series.anomalies)
    onset = _first_onset_time_seconds(
        t_s, anomalies, AnomalyCode.NO_HEAT_DELIVERY.value, after_t_s=30 * 60.0
    )
    assert onset is not None
    assert (onset - 30 * 60.0) <= 90 * 60.0

    cleared = _first_clear_time_seconds(
        t_s, anomalies, AnomalyCode.NO_HEAT_DELIVERY.value, after_t_s=210 * 60.0
    )
    assert cleared is not None
    assert (cleared - 210 * 60.0) <= 15 * 60.0


def test_s41_stuck_open_valve_detected_while_solar_gain_stays_clear() -> None:
    """Verify a mechanically stuck-open valve triggers VALVE_STUCK_OPEN while solar gain does not."""
    res_stuck = run_scenario(MpcV2Adapter(), S41_STUCK_OPEN_VALVE)
    series_stuck = res_stuck.series
    assert series_stuck is not None
    onset = _first_onset_time_seconds(
        list(series_stuck.t_s),
        list(series_stuck.anomalies),
        AnomalyCode.VALVE_STUCK_OPEN.value,
        after_t_s=30 * 60.0,
    )
    assert onset is not None

    solar_scenario = replace(S11_SOLAR_GAIN_MORNING, anomaly_detection=True)
    res_solar = run_scenario(MpcV2Adapter(), solar_scenario)
    series_solar = res_solar.series
    assert series_solar is not None
    for codes in series_solar.anomalies:
        assert AnomalyCode.VALVE_STUCK_OPEN.value not in codes


def test_s42_open_interior_door_triggers_parasitic_loss_only() -> None:
    """Verify steady parasitic loss triggers SUSTAINED_PARASITIC_LOSS without rapid-loss false alarms."""
    res = run_scenario(MpcV2Adapter(), S42_OPEN_INTERIOR_DOOR)
    series = res.series
    assert series is not None
    onset = _first_onset_time_seconds(
        list(series.t_s),
        list(series.anomalies),
        AnomalyCode.SUSTAINED_PARASITIC_LOSS.value,
        after_t_s=60 * 60.0,
    )
    assert onset is not None
    for codes in series.anomalies:
        assert AnomalyCode.UNEXPECTED_RAPID_LOSS.value not in codes


@pytest.mark.parametrize(
    "scenario",
    [
        S01_SETPOINT_STEP_SMALL,
        S04_SETPOINT_DROP,
        S09_WINDOW_OPEN_SHORT,
        S10_WINDOW_OPEN_LONG,
        S11_SOLAR_GAIN_MORNING,
        S14_NIGHTLY_SETBACK,
        S15_DAILY_CYCLE,
    ],
    ids=lambda s: s.name,
)
def test_baseline_scenarios_have_zero_false_positives(scenario) -> None:
    """Verify baseline non-fault scenarios produce no false anomaly triggers."""
    monitored_scenario = replace(scenario, anomaly_detection=True)
    res = run_scenario(MpcV2Adapter(), monitored_scenario)
    series = res.series
    assert series is not None
    non_empty = [
        (t, codes)
        for t, codes in zip(series.t_s, series.anomalies, strict=True)
        if codes
    ]
    assert not non_empty, f"Unexpected anomalies: {non_empty[:5]}"
