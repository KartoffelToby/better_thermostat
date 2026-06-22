"""Tests for sensor thermal lag and pipe-transport delay (Phase D.2)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.benchmark.plant import (
    PROFILE_STANDARD,
    PlantParams,
    PlantState,
    TwoStatePlant,
)
from tests.benchmark.sensor import Sensor, SensorParams

# ---------- Sensor thermal lag ----------


def test_sensor_thermal_lag_zero_passes_temperature_through():
    """With thermal_lag_s = 0, the sensor returns T_true (modulo sampling)."""
    sensor = Sensor(SensorParams(sample_interval_s=0.0, thermal_lag_s=0.0))
    reading = sensor.read(0.0, 20.0)
    assert reading == 20.0


def test_sensor_thermal_lag_settles_to_steady_input():
    """With a steady input and enough time, the lagged reading equals T_true."""
    sensor = Sensor(SensorParams(sample_interval_s=0.0, thermal_lag_s=60.0))
    sensor.read(0.0, 20.0)  # initialise
    for t in range(1, 600):  # 10 min
        reading = sensor.read(float(t), 20.0)
    assert abs(reading - 20.0) < 1e-3


def test_sensor_thermal_lag_first_order_response_to_step():
    """A step in T_true takes about ``thermal_lag_s`` to reach 63 % of the new value."""
    tau_s = 120.0
    sensor = Sensor(SensorParams(sample_interval_s=0.0, thermal_lag_s=tau_s))
    sensor.read(0.0, 20.0)
    sensor.read(0.001, 20.0)  # ensure lag-state is fully initialised at 20.0
    # Apply a step to 22.0 and check the reading at one time constant.
    final = sensor.read(tau_s, 22.0)
    # After one tau, first-order response reaches ≈63 % of step = 20 + 0.63*2 ≈ 21.26
    assert 21.10 < final < 21.45


def test_sensor_thermal_lag_lags_step():
    """Immediately after a step, the reading should still be near the old value."""
    sensor = Sensor(SensorParams(sample_interval_s=0.0, thermal_lag_s=180.0))
    sensor.read(0.0, 20.0)
    sensor.read(0.1, 20.0)
    # 1 second after a 5 K step the reading must still be close to the old value
    reading = sensor.read(1.1, 25.0)
    assert reading < 20.2  # large lag → barely moved


# ---------- Pipe-transport delay ----------


def test_pipe_delay_zero_disables_buffer():
    """With valve_command_delay_s = 0 the plant behaves exactly as before."""
    plant = TwoStatePlant(PROFILE_STANDARD, PlantState(T_room_C=18.0, T_rad_C=18.0))
    for _ in range(60):
        plant.step(30.0, 1.0, 5.0)
    # No assertion on absolute value — sanity check it warmed.
    assert plant.state.T_room_C > 18.5


def test_pipe_delay_buffer_serves_old_value():
    """The radiator sees the buffered (old) u until the delay window passes.

    Setup: prime the buffer with u=0, then command u=1.
    """
    delayed = PlantParams(
        tau_room_min=60.0,
        tau_rad_min=10.0,
        gain_heater=2.0,
        T_water_C=65.0,
        valve_command_delay_s=120.0,
    )
    # Identical thermal constants — only the delay differs, so the
    # comparison isolates the pipe-delay behaviour.
    no_delay = replace(delayed, valve_command_delay_s=0.0)
    plant_a = TwoStatePlant(no_delay, PlantState(T_room_C=18.0, T_rad_C=18.0))
    plant_b = TwoStatePlant(delayed, PlantState(T_room_C=18.0, T_rad_C=18.0))

    # Prime both plants with u=0 so they share the same pre-state.
    for _ in range(4):  # 2 min of u=0
        plant_a.step(30.0, 0.0, 5.0)
        plant_b.step(30.0, 0.0, 5.0)
    # Now command u=1.0. The next 2 minutes (4 steps of 30 s) the
    # radiator should still see u=0 from the buffer; only after that
    # the u=1.0 starts arriving.
    plant_a.step(30.0, 1.0, 5.0)  # plant_a sees u=1.0 immediately
    plant_b.step(30.0, 1.0, 5.0)  # plant_b serves the head of the buffer (=0)
    # plant_a's radiator should have warmed, plant_b's not at all yet.
    assert plant_a.state.T_rad_C > plant_b.state.T_rad_C


def test_realistic_profile_has_both_features():
    """PROFILE_REALISTIC ships with RC3 wall + pipe delay enabled."""
    from tests.benchmark.plant import PROFILE_REALISTIC

    assert PROFILE_REALISTIC.tau_wall_min > 0.0
    assert PROFILE_REALISTIC.valve_command_delay_s > 0.0


def test_pipe_delay_rejects_changed_dt_s():
    """Changing dt_s after the delay buffer is sized fails fast."""
    params = replace(PROFILE_STANDARD, valve_command_delay_s=60.0)
    plant = TwoStatePlant(params, PlantState(T_room_C=20.0, T_rad_C=20.0))
    plant.step(30.0, 1.0, 5.0)  # sizes the buffer for dt_s=30
    with pytest.raises(ValueError):
        plant.step(60.0, 1.0, 5.0)  # different dt_s → reject
