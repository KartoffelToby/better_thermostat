"""Drift guards binding benchmark adapters to their production sources.

The benchmark is only meaningful if its adapters reproduce the deployed
calibration logic. Most adapters call the production functions directly
(``compute_mpc`` / ``compute_pid`` / ``compute_tpi``) and cannot drift.
The heating-power adapter is the exception: it re-implements the
valve-position state machine in pure Python (production's
``heating_power_valve_position`` is bolted onto the HA entity and cannot
be called headless). That re-implementation duplicates the production
formula and its tuning constants, so it can silently drift when the
production heuristic changes.

This test pins the duplication: it drives the *real* production function
through a fake entity across a grid of ``(temp_diff, heating_power)`` and
asserts the benchmark adapter produces the identical valve position. If
anyone retunes ``heating_power_valve_position`` (coefficients, floors,
clamping) without updating the adapter, this fails.
"""

from __future__ import annotations

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.containers import BtConfig, BtRuntime
from custom_components.better_thermostat.utils.helpers import (
    heating_power_valve_position,
)
from custom_components.better_thermostat.utils.thermal_learning import (
    HeatingPowerTracker,
)
from tests.benchmark.adapters.base import BenchmarkContext
from tests.benchmark.adapters.heating_power_adapter import HeatingPowerAdapter

# Cover the full heating regime: tiny error (minimum-opening floors),
# mid-range, and saturation, across the learned heating-power band.
_TEMP_DIFFS_K = [0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 3.5, 5.0]
_HEATING_POWERS = [0.005, 0.01, 0.02, 0.035, 0.05]


def _entity(
    heat_target_temperature: float, room_temperature: float, heating_power: float
) -> BetterThermostat:
    """Entity shell carrying only the containers the valve heuristic reads."""
    entity = object.__new__(BetterThermostat)
    entity.config = BtConfig(device_name="drift-guard")
    entity.runtime = BtRuntime(
        heat_target_temperature=heat_target_temperature,
        room_temperature=room_temperature,
    )
    entity._heating_tracker = HeatingPowerTracker(heating_power=heating_power)
    return entity


def _production_valve_percent(
    temperature_difference_K: float, heating_power: float
) -> float:
    """Run the real production formula and return valve percent (0..100)."""
    entity = _entity(20.0 + temperature_difference_K, 20.0, heating_power)
    valve_fraction = heating_power_valve_position(
        entity, "drift_guard_trv", entity.room_temperature
    )
    assert valve_fraction is not None
    return valve_fraction * 100.0


def _adapter_valve_percent(
    temperature_difference_K: float, heating_power: float
) -> float:
    """Run the benchmark adapter's formula and return valve percent (0..100)."""
    adapter = HeatingPowerAdapter(initial_heating_power=heating_power)
    adapter.heating_power = heating_power
    ctx = BenchmarkContext(
        t=0.0,
        dt=30.0,
        target_temperature=20.0 + temperature_difference_K,
        room_temperature=20.0,
        raw_room_temperature=20.0,
        trv_temperature=None,
        outdoor_temperature=5.0,
    )
    return adapter._compute_valve_percent(ctx)


@pytest.mark.parametrize("temperature_difference_K", _TEMP_DIFFS_K)
@pytest.mark.parametrize("heating_power", _HEATING_POWERS)
def test_heating_power_adapter_matches_production(
    temperature_difference_K: float, heating_power: float
) -> None:
    """Adapter valve position equals the production heuristic across the grid."""
    expected = _production_valve_percent(temperature_difference_K, heating_power)
    actual = _adapter_valve_percent(temperature_difference_K, heating_power)
    assert actual == pytest.approx(expected, abs=1e-9), (
        f"heating-power adapter drifted from production at "
        f"temp_diff={temperature_difference_K} K, heating_power={heating_power}: "
        f"adapter={actual:.6f}%, production={expected:.6f}%"
    )


def test_non_heating_returns_zero_in_both() -> None:
    """Room at/above target → both production and adapter command 0 %."""
    assert _production_valve_percent(0.0, 0.02) == 0.0
    assert _adapter_valve_percent(0.0, 0.02) == 0.0
    # Room above target (negative diff): production short-circuits to 0.
    entity = _entity(19.5, 20.0, 0.02)
    assert heating_power_valve_position(entity, "trv", entity.room_temperature) == 0.0
