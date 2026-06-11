"""Tests for the foreign-temperature inbound boundary helpers.

``state_temperature_unit`` resolves the source unit of a state, falling back to
the Home Assistant system unit because ``climate`` entities report in that unit
and expose no unit attribute. ``attr_to_celsius`` wraps that resolution and the
Celsius conversion into the single inbound boundary used across the codebase.
"""

from types import SimpleNamespace

from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.utils.helpers import (
    attr_to_celsius,
    state_temperature_unit,
)


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
