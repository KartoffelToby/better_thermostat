"""Tests for the foreign-temperature inbound boundary helpers.

``state_temperature_unit`` resolves the source unit of a state, falling back to
the Home Assistant system unit because ``climate`` entities report in that unit
and expose no unit attribute. ``attr_to_celsius`` wraps that resolution and the
Celsius conversion into the single inbound boundary used across the codebase.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
from homeassistant.util.unit_conversion import TemperatureConverter
import pytest

from custom_components.better_thermostat.utils.helpers import (
    attr_to_celsius,
    state_temperature_unit,
)
from tests.factories import make_entity_registry, make_registry_entry


def _bt(system_unit):
    """Minimal stand-in exposing the ``hass`` and ``device_name`` attr_to_celsius reads."""
    return SimpleNamespace(
        device_name="Test BT",
        hass=SimpleNamespace(
            config=SimpleNamespace(units=SimpleNamespace(temperature_unit=system_unit))
        ),
    )


def _state(attributes):
    """Build a minimal climate State carrying the given attributes."""
    return State("climate.trv", "heat", attributes=attributes)


class TestStateTemperatureUnit:
    """Unit resolution with the system-unit fallback."""

    def test_explicit_temperature_unit_wins(self):
        """An explicit temperature_unit attribute takes precedence."""
        attrs = {"temperature_unit": UnitOfTemperature.CELSIUS}
        assert (
            state_temperature_unit(attrs, UnitOfTemperature.FAHRENHEIT)
            == UnitOfTemperature.CELSIUS
        )

    def test_unit_of_measurement_fallback(self):
        """unit_of_measurement is used when temperature_unit is absent."""
        attrs = {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT}
        assert (
            state_temperature_unit(attrs, UnitOfTemperature.CELSIUS)
            == UnitOfTemperature.FAHRENHEIT
        )

    def test_no_attribute_uses_system_unit(self):
        """Without any unit attribute the system unit is the fallback."""
        assert (
            state_temperature_unit({}, UnitOfTemperature.FAHRENHEIT)
            == UnitOfTemperature.FAHRENHEIT
        )

    def test_none_attributes_uses_system_unit(self):
        """``None`` attributes fall back to the system unit."""
        assert (
            state_temperature_unit(None, UnitOfTemperature.CELSIUS)
            == UnitOfTemperature.CELSIUS
        )


class TestAttrToCelsius:
    """The combined read + convert boundary."""

    def test_fahrenheit_system_without_unit_attr(self):
        """A unit-less climate attribute is read via the Fahrenheit system unit."""
        bt = _bt(UnitOfTemperature.FAHRENHEIT)
        result = attr_to_celsius(bt, _state({"temperature": 64.0}), "temperature")
        assert result == pytest.approx(17.78, abs=0.05)

    def test_celsius_system_without_unit_attr(self):
        """A Celsius system leaves the value unchanged."""
        bt = _bt(UnitOfTemperature.CELSIUS)
        assert attr_to_celsius(bt, _state({"temperature": 20.0}), "temperature") == 20.0

    def test_explicit_unit_attribute_overrides_system(self):
        """An explicit Fahrenheit unit converts even on a Celsius system."""
        bt = _bt(UnitOfTemperature.CELSIUS)
        state = _state(
            {"temperature": 68.0, "temperature_unit": UnitOfTemperature.FAHRENHEIT}
        )
        assert attr_to_celsius(bt, state, "temperature") == pytest.approx(20.0)

    def test_missing_key_returns_default_converted(self):
        """A missing key uses the default, still unit-resolved."""
        bt = _bt(UnitOfTemperature.FAHRENHEIT)
        assert attr_to_celsius(bt, _state({}), "temperature", 50) == pytest.approx(10.0)

    def test_missing_key_no_default_returns_none(self):
        """A missing key with no default yields None."""
        bt = _bt(UnitOfTemperature.CELSIUS)
        assert attr_to_celsius(bt, _state({}), "temperature") is None

    def test_none_state_returns_default_converted(self):
        """A missing state falls back to the default, resolved via system unit."""
        bt = _bt(UnitOfTemperature.FAHRENHEIT)
        assert attr_to_celsius(bt, None, "temperature", 50) == pytest.approx(10.0)


# 70 °F treated as °C and converted again: 70 * 9/5 + 32 = 158.
_TUYA_DOUBLE_CONVERTED_F = 158.0
_TRUE_FAHRENHEIT = 70.0
_TUYA_ENTITY = "climate.garage"


def _celsius_from_fahrenheit(fahrenheit: float) -> float:
    """Return the °C value ``convert_to_float_celsius`` stores for a °F reading."""
    return round(
        TemperatureConverter.convert(
            fahrenheit, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        ),
        2,
    )


def _read_climate_temperature(system_unit, published, platform, key):
    """Read one climate attribute through the inbound boundary.

    ``platform`` is what the entity registry reports for the climate entity.
    ``None`` means the entity is not registered.
    """
    bt = _bt(system_unit)
    state = State(_TUYA_ENTITY, "heat", {key: published})
    entries = ()
    if platform is not None:
        entries = (make_registry_entry(_TUYA_ENTITY, platform=platform),)
    registry = make_entity_registry(*entries)
    with patch(
        "custom_components.better_thermostat.utils.helpers.er.async_get",
        return_value=registry,
    ):
        return attr_to_celsius(bt, state, key)


class TestTuyaFahrenheitDoubleConversion:
    """Compatibility shim for HA core's Tuya Fahrenheit double conversion.

    A Tuya climate entity on a Fahrenheit system can publish 70 °F as 158
    (70 treated as Celsius, then converted to Fahrenheit again). The shim
    undoes that one extra conversion. A plausible reading, a non-Tuya
    entity, and a Celsius install are left untouched.
    """

    @pytest.fixture(autouse=True)
    def _clear_warning_memory(self):
        """Each case starts without a prior warning for this entity."""
        from custom_components.better_thermostat.utils import helpers

        warned = getattr(helpers, "_tuya_double_conversion_warned", None)
        if isinstance(warned, set):
            warned.clear()
        yield
        if isinstance(warned, set):
            warned.clear()

    @pytest.mark.parametrize("key", ["current_temperature", "temperature"])
    def test_bad_tuya_reading_is_corrected_to_70_fahrenheit(self, key):
        """158 published by Tuya on a Fahrenheit system is read as 70 °F."""
        result = _read_climate_temperature(
            UnitOfTemperature.FAHRENHEIT, _TUYA_DOUBLE_CONVERTED_F, "tuya", key
        )
        assert result == _celsius_from_fahrenheit(_TRUE_FAHRENHEIT)

    @pytest.mark.parametrize("key", ["current_temperature", "temperature"])
    def test_correct_70_fahrenheit_is_left_alone(self, key):
        """A real 70 °F Tuya reading is converted once and not again."""
        result = _read_climate_temperature(
            UnitOfTemperature.FAHRENHEIT, _TRUE_FAHRENHEIT, "tuya", key
        )
        assert result == _celsius_from_fahrenheit(_TRUE_FAHRENHEIT)

    @pytest.mark.parametrize("key", ["current_temperature", "temperature"])
    def test_non_tuya_entity_is_left_alone(self, key):
        """The same 158 from another integration stays the naive conversion."""
        result = _read_climate_temperature(
            UnitOfTemperature.FAHRENHEIT, _TUYA_DOUBLE_CONVERTED_F, "mqtt", key
        )
        assert result == _celsius_from_fahrenheit(_TUYA_DOUBLE_CONVERTED_F)

    @pytest.mark.parametrize("key", ["current_temperature", "temperature"])
    def test_celsius_install_is_left_alone(self, key):
        """A Celsius system never reinterprets a Tuya reading as Fahrenheit."""
        result = _read_climate_temperature(
            UnitOfTemperature.CELSIUS, _TRUE_FAHRENHEIT, "tuya", key
        )
        assert result == _TRUE_FAHRENHEIT

    def test_warns_once_about_the_upstream_tuya_bug(self, caplog):
        """The workaround logs the upstream Tuya bug once per entity."""
        with caplog.at_level(logging.WARNING):
            first = _read_climate_temperature(
                UnitOfTemperature.FAHRENHEIT,
                _TUYA_DOUBLE_CONVERTED_F,
                "tuya",
                "current_temperature",
            )
            second = _read_climate_temperature(
                UnitOfTemperature.FAHRENHEIT,
                _TUYA_DOUBLE_CONVERTED_F,
                "tuya",
                "current_temperature",
            )
        assert first == second == _celsius_from_fahrenheit(_TRUE_FAHRENHEIT)
        warnings = [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING and "Tuya" in record.getMessage()
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "upstream" in message.lower()
        assert _TUYA_ENTITY in message

    def test_unreadable_registry_is_left_alone(self):
        """A registry that cannot be read leaves the published value untouched."""
        bt = _bt(UnitOfTemperature.FAHRENHEIT)
        state = State(
            _TUYA_ENTITY, "heat", {"current_temperature": _TUYA_DOUBLE_CONVERTED_F}
        )
        assert attr_to_celsius(bt, state, "current_temperature") == (
            _celsius_from_fahrenheit(_TUYA_DOUBLE_CONVERTED_F)
        )

    def test_unregistered_entity_is_left_alone(self):
        """No registry entry means the reading is not known to be Tuya."""
        result = _read_climate_temperature(
            UnitOfTemperature.FAHRENHEIT,
            _TUYA_DOUBLE_CONVERTED_F,
            None,
            "current_temperature",
        )
        assert result == _celsius_from_fahrenheit(_TUYA_DOUBLE_CONVERTED_F)

    def test_tuya_sensor_is_left_alone(self):
        """Only a Tuya climate entity is rewritten; a Tuya sensor is not."""
        bt = _bt(UnitOfTemperature.FAHRENHEIT)
        entity_id = "sensor.garage_temperature"
        state = State(
            entity_id, "heat", {"current_temperature": _TUYA_DOUBLE_CONVERTED_F}
        )
        registry = make_entity_registry(make_registry_entry(entity_id, platform="tuya"))
        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=registry,
        ):
            result = attr_to_celsius(bt, state, "current_temperature")
        assert result == _celsius_from_fahrenheit(_TUYA_DOUBLE_CONVERTED_F)

    def test_reversal_that_stays_implausible_is_left_alone(self):
        """A reading that is nonsense in both interpretations is not rewritten."""
        published = 300.0
        result = _read_climate_temperature(
            UnitOfTemperature.FAHRENHEIT, published, "tuya", "current_temperature"
        )
        assert result == _celsius_from_fahrenheit(published)
