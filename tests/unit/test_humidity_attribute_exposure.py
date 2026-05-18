"""Tests for humidity attribute exposure in extra_state_attributes.

HA reserves the ``humidity`` state attribute for the target humidity used by
the ``climate.set_humidity`` service. BT does not implement that service, so
exposing the current-humidity reading under that key triggers the scene
reproduce-state path to call an unsupported action.
"""

from datetime import UTC, datetime
import json
from unittest.mock import MagicMock

from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode

from custom_components.better_thermostat.climate import BetterThermostat


def _make_mock_bt(**overrides):
    """Construct a mock_bt sufficient for the ``extra_state_attributes`` property."""
    bt = MagicMock()
    bt.window_open = False
    bt.call_for_heat = True
    bt.last_change = datetime(2026, 5, 18, tzinfo=UTC)
    bt._saved_temperature = None
    bt._preset_temperature = None
    bt._current_humidity = None
    bt.last_main_hvac_mode = HVACMode.HEAT
    bt.off_temperature = None
    bt.tolerance = 0.5
    bt.bt_target_temp_step = 0.5
    bt.heating_power = 0.1
    bt.heat_loss_rate = 0.0
    bt.devices_errors = []
    bt.devices_states = {}
    bt.cur_temp_filtered = 20.5
    bt.degraded_mode = False
    bt.unavailable_sensors = []
    bt.real_trvs = {}
    bt.heating_cycles = []
    bt.loss_cycles = []
    bt.last_heating_power_stats = {}
    bt.last_heat_loss_stats = {}
    bt.next_valve_maintenance = None
    for k, v in overrides.items():
        setattr(bt, k, v)
    return bt


class TestHumidityAttributeExposure:
    """Humidity must not leak into the reserved ``humidity`` state attribute."""

    def test_no_humidity_key_in_attributes_without_sensor(self):
        """Without a configured humidity sensor, no ``humidity`` key is exposed."""
        bt = _make_mock_bt(humidity_sensor_entity_id=None, _current_humidity=None)
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert "humidity" not in attrs

    def test_no_humidity_key_in_attributes_with_sensor(self):
        """Even with a sensor configured the reserved ``humidity`` key stays absent.

        The current-humidity reading is exposed via the ``current_humidity``
        property — using the reserved key collides with the climate target
        humidity attribute that drives ``climate.set_humidity``.
        """
        bt = _make_mock_bt(
            humidity_sensor_entity_id="sensor.room_humidity", _current_humidity=42.5
        )
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert "humidity" not in attrs

    def test_target_humidity_feature_not_advertised(self):
        """BT must not advertise ``TARGET_HUMIDITY`` in its supported feature set."""
        bt = _make_mock_bt(cooler_entity_id=None)
        bt._support_flags = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.PRESET_MODE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )
        features = BetterThermostat.supported_features.fget(bt)
        assert ClimateEntityFeature.TARGET_HUMIDITY not in ClimateEntityFeature(
            features
        )


class TestExtraStateAttributesSmoke:
    """Sanity check that the property still returns a usable dict."""

    def test_returns_dict_with_expected_keys(self):
        """The property returns a dict with the documented top-level keys."""
        bt = _make_mock_bt()
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert isinstance(attrs, dict)
        for required in (
            "window_open",
            "call_for_heat",
            "last_change",
            "external_temp_ema",
            "degraded_mode",
        ):
            assert required in attrs
        assert json.loads(attrs["errors"]) == []
        assert json.loads(attrs["batteries"]) == {}
