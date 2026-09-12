"""Tests for humidity attribute exposure in extra_state_attributes.

HA reserves the ``humidity`` state attribute for the target humidity used by
the ``climate.set_humidity`` service. BT does not implement that service, so
exposing the current-humidity reading under that key triggers the scene
reproduce-state path to call an unsupported action.
"""

import json

from homeassistant.components.climate.const import ClimateEntityFeature

from custom_components.better_thermostat.climate import BetterThermostat
from tests.factories import make_state_attributes_bt


class TestHumidityAttributeExposure:
    """Humidity must not leak into the reserved ``humidity`` state attribute."""

    def test_no_humidity_key_in_attributes_without_sensor(self):
        """Without a configured humidity sensor, no ``humidity`` key is exposed."""
        bt = make_state_attributes_bt(
            humidity_sensor_entity_id=None, _current_humidity=None
        )
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert "humidity" not in attrs

    def test_no_humidity_key_in_attributes_with_sensor(self):
        """Even with a sensor configured the reserved ``humidity`` key stays absent.

        The current-humidity reading is exposed via the ``current_humidity``
        property — using the reserved key collides with the climate target
        humidity attribute that drives ``climate.set_humidity``.
        """
        bt = make_state_attributes_bt(
            humidity_sensor_entity_id="sensor.room_humidity", _current_humidity=42.5
        )
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert "humidity" not in attrs

    def test_target_humidity_feature_not_advertised(self):
        """BT must not advertise ``TARGET_HUMIDITY`` in its supported feature set."""
        bt = make_state_attributes_bt(cooler_entity_id=None)
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
        bt = make_state_attributes_bt()
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
