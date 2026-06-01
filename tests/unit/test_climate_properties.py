"""Branch coverage for BetterThermostat temperature/feature properties.

Covers the clamping in target_temperature, the cooler-gated low/high targets,
the configured min/max bounds, and the cooler-dependent supported_features.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import ClimateEntityFeature
import pytest

from custom_components.better_thermostat.climate import BetterThermostat


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for property access."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.bt_target_temp = 21.0
    mock.bt_target_cooltemp = 25.0
    mock.bt_min_temp = 5.0
    mock.bt_max_temp = 30.0
    mock.cooler_entity_id = None
    return mock


def _prop(name, bt):
    return getattr(BetterThermostat, name).fget(bt)


# --- target_temperature (clamping) -----------------------------------------


def test_target_temperature_none(bt):
    """No internal target -> None."""
    bt.bt_target_temp = None
    assert _prop("target_temperature", bt) is None


def test_target_temperature_without_bounds(bt):
    """Without bounds the raw target is returned."""
    bt.bt_min_temp = None
    bt.bt_max_temp = None
    bt.bt_target_temp = 99.0
    assert _prop("target_temperature", bt) == 99.0


def test_target_temperature_clamped_below_min(bt):
    """A target below min reads as min."""
    bt.bt_target_temp = 2.0
    assert _prop("target_temperature", bt) == 5.0


def test_target_temperature_clamped_above_max(bt):
    """A target above max reads as max."""
    bt.bt_target_temp = 99.0
    assert _prop("target_temperature", bt) == 30.0


def test_target_temperature_in_range(bt):
    """An in-range target is returned unchanged."""
    bt.bt_target_temp = 21.0
    assert _prop("target_temperature", bt) == 21.0


# --- target_temperature_low / _high (cooler gated) -------------------------


def test_target_low_none_without_cooler(bt):
    """Without a cooler the low target is None."""
    bt.cooler_entity_id = None
    assert _prop("target_temperature_low", bt) is None


def test_target_low_is_heat_target_with_cooler(bt):
    """With a cooler the low target is the heat target."""
    bt.cooler_entity_id = "climate.cooler"
    assert _prop("target_temperature_low", bt) == 21.0


def test_target_high_none_without_cooler(bt):
    """Without a cooler the high target is None."""
    bt.cooler_entity_id = None
    assert _prop("target_temperature_high", bt) is None


def test_target_high_is_cool_target_with_cooler(bt):
    """With a cooler the high target is the cool target."""
    bt.cooler_entity_id = "climate.cooler"
    assert _prop("target_temperature_high", bt) == 25.0


# --- min_temp / max_temp (configured bounds) -------------------------------


def test_min_temp_uses_configured(bt):
    """A configured min is returned directly."""
    assert _prop("min_temp", bt) == 5.0


def test_max_temp_uses_configured(bt):
    """A configured max is returned directly."""
    assert _prop("max_temp", bt) == 30.0


# --- supported_features (cooler dependent) ---------------------------------


def test_features_target_temperature_without_cooler(bt):
    """Without a cooler the single-setpoint feature is advertised."""
    bt.cooler_entity_id = None
    feats = _prop("supported_features", bt)
    assert feats & ClimateEntityFeature.TARGET_TEMPERATURE
    assert not (feats & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)


def test_features_target_range_with_cooler(bt):
    """With a cooler the range feature is advertised instead."""
    bt.cooler_entity_id = "climate.cooler"
    feats = _prop("supported_features", bt)
    assert feats & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    assert not (feats & ClimateEntityFeature.TARGET_TEMPERATURE)
