"""Tests for BetterThermostatPresetNumber state restoration across unit systems.

The preset number entity declares its native unit as Celsius. Home Assistant
formats the entity's ``state`` in the system temperature unit (Fahrenheit on
Fahrenheit installations), so the stored last_state may be either unit. The
restore path must convert back to Celsius before writing the preset
temperature dict that ``BetterThermostat`` consumes.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import PRESET_HOME
from homeassistant.const import UnitOfTemperature
import pytest

from custom_components.better_thermostat.number import BetterThermostatPresetNumber


def _make_entity():
    bt_climate = MagicMock()
    bt_climate.unique_id = "test_bt"
    bt_climate.device_name = "Test BT"
    bt_climate.min_temp = 5.0
    bt_climate.max_temp = 30.0
    bt_climate.target_temperature_step = 0.5
    stored: dict[str, float] = {}
    bt_climate.preset_mgr.update_temperature.side_effect = stored.__setitem__
    bt_climate.preset_mgr.get_temperature.side_effect = stored.get
    bt_climate.preset_mgr.temperatures = stored
    entity = BetterThermostatPresetNumber(bt_climate, PRESET_HOME)
    return entity, bt_climate


def _last_state(state_value, unit):
    ls = MagicMock()
    ls.state = state_value
    ls.attributes = {"unit_of_measurement": unit} if unit is not None else {}
    return ls


class TestPresetNumberRestoreUnitConversion:
    """``last_state.state`` is in HA's display unit and must be normalised to Celsius."""

    @pytest.mark.asyncio
    async def test_restore_fahrenheit_state_stored_as_celsius(self):
        """``68 °F`` saved by HA is restored as ``20 °C`` in the preset dict."""
        entity, bt_climate = _make_entity()
        entity.async_get_last_state = AsyncMock(
            return_value=_last_state("68", UnitOfTemperature.FAHRENHEIT)
        )

        await entity.async_added_to_hass()

        assert bt_climate.preset_mgr.temperatures[PRESET_HOME] == pytest.approx(
            20.0, abs=0.01
        )

    @pytest.mark.asyncio
    async def test_restore_celsius_state_kept_as_is(self):
        """A Celsius-saved value is restored verbatim."""
        entity, bt_climate = _make_entity()
        entity.async_get_last_state = AsyncMock(
            return_value=_last_state("20.0", UnitOfTemperature.CELSIUS)
        )

        await entity.async_added_to_hass()

        assert bt_climate.preset_mgr.temperatures[PRESET_HOME] == pytest.approx(
            20.0, abs=0.01
        )

    @pytest.mark.asyncio
    async def test_restore_without_unit_attribute_treated_as_celsius(self):
        """When the saved state lacks a unit attribute the value is treated as native."""
        entity, bt_climate = _make_entity()
        entity.async_get_last_state = AsyncMock(return_value=_last_state("20.0", None))

        await entity.async_added_to_hass()

        assert bt_climate.preset_mgr.temperatures[PRESET_HOME] == pytest.approx(
            20.0, abs=0.01
        )

    @pytest.mark.asyncio
    async def test_restore_unknown_state_keeps_dict_empty(self):
        """``unknown`` / ``unavailable`` last states do not write anything."""
        entity, bt_climate = _make_entity()
        entity.async_get_last_state = AsyncMock(
            return_value=_last_state("unknown", UnitOfTemperature.FAHRENHEIT)
        )

        await entity.async_added_to_hass()

        assert PRESET_HOME not in bt_climate.preset_mgr.temperatures

    @pytest.mark.asyncio
    async def test_restore_no_last_state_keeps_dict_empty(self):
        """No prior state at all (fresh install) does not write anything."""
        entity, bt_climate = _make_entity()
        entity.async_get_last_state = AsyncMock(return_value=None)

        await entity.async_added_to_hass()

        assert PRESET_HOME not in bt_climate.preset_mgr.temperatures
