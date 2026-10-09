"""Branch coverage for the BetterThermostat.hvac_mode property.

The property maps the internal bt_hvac_mode onto a mode HA accepts, falling
back to the cooler-mapped mode or OFF when the result is not in the entity's
available list.
"""

from unittest.mock import patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from tests.factories import ThermostatStandIn

_CLIMATE = "custom_components.better_thermostat.climate"
_FULL_LIST = [HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.OFF]


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for the hvac_mode property."""
    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock.bt_hvac_mode = HVACMode.HEAT
    mock._hvac_list = list(_FULL_LIST)
    mock.map_on_hvac_mode = HVACMode.HEAT_COOL
    return mock


def _hvac_mode(bt):
    return BetterThermostat.hvac_mode.fget(bt)


def test_none_maps_to_off(bt):
    """A missing internal mode reads as OFF."""
    bt.bt_hvac_mode = None
    assert _hvac_mode(bt) == HVACMode.OFF


def test_enum_in_list_passthrough(bt):
    """An HVACMode already in the available list is returned unchanged."""
    with patch(f"{_CLIMATE}.get_hvac_bt_mode", return_value=HVACMode.HEAT):
        assert _hvac_mode(bt) == HVACMode.HEAT


def test_heat_not_in_list_maps_to_cooler_mode(bt):
    """When HEAT is unavailable, it maps to the cooler-mapped mode."""
    bt._hvac_list = [HVACMode.HEAT_COOL, HVACMode.OFF]
    with patch(f"{_CLIMATE}.get_hvac_bt_mode", return_value=HVACMode.HEAT):
        assert _hvac_mode(bt) == HVACMode.HEAT_COOL


def test_unavailable_non_heat_maps_to_off(bt):
    """A result that is unavailable and not HEAT degrades to OFF."""
    bt._hvac_list = [HVACMode.HEAT, HVACMode.OFF]
    with patch(f"{_CLIMATE}.get_hvac_bt_mode", return_value=HVACMode.HEAT_COOL):
        assert _hvac_mode(bt) == HVACMode.OFF
