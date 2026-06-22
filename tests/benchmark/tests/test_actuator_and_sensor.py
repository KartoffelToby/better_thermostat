"""Actuator profile + sensor edge-case tests.

The single-TRV scenarios exercise most actuator/sensor paths implicitly,
but several branches (deadband, threshold profile, jitter, dropout) are
only hit under specific param combinations these unit tests pin down.
"""

from __future__ import annotations

from tests.benchmark.actuator import Actuator, ActuatorParams, ActuatorProfile
from tests.benchmark.sensor import Sensor, SensorParams


def test_actuator_linear_default():
    """Actuator linear default."""
    a = Actuator(ActuatorParams())
    assert a.apply(0.0) == 0.0
    assert a.apply(50.0) == 0.5
    assert a.apply(100.0) == 1.0


def test_actuator_clamps_input_range():
    """Actuator clamps input range."""
    a = Actuator(ActuatorParams())
    assert a.apply(-10.0) == 0.0
    assert a.apply(110.0) == 1.0


def test_actuator_threshold_profile():
    """Actuator threshold profile."""
    a = Actuator(ActuatorParams(profile=ActuatorProfile.THRESHOLD, dead_zone_pct=20.0))
    assert a.apply(10.0) == 0.0  # inside dead zone → zero flow
    # Just above dead zone: small but nonzero.
    out = a.apply(25.0)
    assert 0.0 < out < 0.1
    # Full open.
    assert abs(a.apply(100.0) - 1.0) < 1e-6


def test_actuator_exponential_profile():
    """Actuator exponential profile."""
    a = Actuator(ActuatorParams(profile=ActuatorProfile.EXPONENTIAL))
    assert abs(a.apply(50.0) - 0.25) < 1e-9
    assert a.apply(100.0) == 1.0


def test_actuator_equal_percentage_curve():
    """Actuator equal percentage curve."""
    a = Actuator(
        ActuatorParams(
            profile=ActuatorProfile.EQUAL_PERCENTAGE, equal_percentage_exponent=3.0
        )
    )
    assert abs(a.apply(50.0) - 0.125) < 1e-9
    assert a.apply(100.0) == 1.0


def test_actuator_deadband_zeroes_low_commands():
    """Actuator deadband zeroes low commands."""
    a = Actuator(ActuatorParams(deadband_pct=10.0))
    assert a.apply(5.0) == 0.0
    assert a.apply(9.99) == 0.0
    assert a.apply(10.0) > 0.0


def test_actuator_hysteresis_holds_last_value():
    """Actuator hysteresis holds last value."""
    a = Actuator(ActuatorParams(hysteresis_pct=5.0))
    a.apply(50.0)  # set baseline
    # Small wiggle inside the band → no movement.
    out_inside = a.apply(52.0)
    assert out_inside == 0.5
    # Step outside the band → moves.
    out_outside = a.apply(60.0)
    assert out_outside == 0.6


def test_actuator_quantize_snaps_to_grid():
    """Actuator quantize snaps to grid."""
    a = Actuator(ActuatorParams(quantize_pct=10.0))
    # 12 → 10, 17 → 20.
    assert a.apply(12.0) == 0.1
    assert a.apply(17.0) == 0.2


def test_sensor_noise_path_is_deterministic():
    """Two fresh Sensor instances with identical params produce identical reads."""
    p = SensorParams(noise_std_K=0.5, sample_interval_s=30.0)
    s1 = Sensor(p)
    s2 = Sensor(p)
    a = [s1.read(t * 30.0, 20.0) for t in range(5)]
    b = [s2.read(t * 30.0, 20.0) for t in range(5)]
    assert a == b
    assert any(v != 20.0 for v in a)  # noise actually fires


def test_sensor_seed_is_deterministic_and_decorrelating():
    """Same seed → identical noise; different seeds → different realisations."""
    p = SensorParams(noise_std_K=0.5, sample_interval_s=30.0)
    same_a = [Sensor(p, seed=7).read(t * 30.0, 20.0) for t in range(5)]
    same_b = [Sensor(p, seed=7).read(t * 30.0, 20.0) for t in range(5)]
    assert same_a == same_b  # reproducible
    other = [Sensor(p, seed=99).read(t * 30.0, 20.0) for t in range(5)]
    assert other != same_a  # decorrelated across seeds


def test_sensor_dropout_returns_none():
    """Sensor dropout returns none."""
    p = SensorParams(dropout_until_t_s=600.0)
    s = Sensor(p)
    assert s.read(0.0, 20.0) is None
    assert s.read(599.999, 20.0) is None
    assert s.read(600.0, 20.0) is not None  # past dropout window


def test_sensor_jitter_changes_next_interval():
    """Sensor jitter changes next interval."""
    p = SensorParams(sample_interval_s=30.0, jitter_std_s=10.0)
    s = Sensor(p)
    # Force the first sample so the jitter recomputation runs. The RNG is
    # deterministic, so the rolled interval must differ from the nominal.
    s.read(0.0, 20.0)
    assert s._next_sample_interval_s != 30.0
    # Drive several reads.
    for t in range(1, 20):
        s.read(t * 30.0, 20.0)


def test_sensor_thermal_lag_smooths_step():
    """Sensor thermal lag smooths step."""
    p = SensorParams(thermal_lag_s=120.0, sample_interval_s=30.0)
    s = Sensor(p)
    # Initial reading equals the true value (state init).
    first = s.read(0.0, 20.0)
    assert first == 20.0
    # Step the world to 22 — the sensor lags behind.
    second = s.read(30.0, 22.0)
    assert second is not None
    assert 20.0 < second < 22.0


def test_sensor_bias_and_drift_apply():
    """Sensor bias and drift apply."""
    p = SensorParams(bias_K=0.5, drift_K_per_h=0.1, sample_interval_s=30.0)
    s = Sensor(p)
    # At t=0: bias only.
    first = s.read(0.0, 20.0)
    assert first is not None
    assert abs(first - 20.5) < 1e-6
    # At t=1h: bias + 0.1 K drift.
    later = s.read(3600.0, 20.0)
    assert later is not None
    assert abs(later - 20.6) < 1e-6


def test_sensor_dropout_window_with_start():
    """The dropout honours its start timestamp, not just the end."""
    p = SensorParams(dropout_from_t_s=600.0, dropout_until_t_s=1200.0)
    s = Sensor(p)
    assert s.read(0.0, 20.0) is not None  # before the outage
    assert s.read(600.0, 20.0) is None
    assert s.read(1199.0, 20.0) is None
    assert s.read(1200.0, 20.0) is not None  # after the outage


def test_thermal_lag_advances_through_dropout():
    """The sensor body keeps tracking the room during the outage."""
    p = SensorParams(
        sample_interval_s=0.0,
        thermal_lag_s=60.0,
        dropout_from_t_s=10.0,
        dropout_until_t_s=600.0,
    )
    s = Sensor(p)
    s.read(0.0, 20.0)  # initialise the lag state at 20 °C
    # Room steps to 25 °C during the outage.
    for t in range(10, 600, 30):
        assert s.read(float(t), 25.0) is None
    after = s.read(600.0, 25.0)
    # ~10 lag time-constants passed → the first post-outage reading must
    # reflect the new room temperature, not the pre-outage state.
    assert after is not None
    assert after > 24.0
