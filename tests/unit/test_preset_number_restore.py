"""Tests for BetterThermostatPresetNumber state restoration across unit systems.

The preset number entity declares its native unit as Celsius. Home Assistant
formats the entity's ``state`` in the system temperature unit (Fahrenheit on
Fahrenheit installations), so the stored last_state may be either unit. The
restore path must convert back to Celsius before writing the preset
temperature dict that ``BetterThermostat`` consumes.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import PRESET_HOME, HVACMode
from homeassistant.const import UnitOfTemperature
import pytest

from custom_components.better_thermostat.number import (
    BetterThermostatPresetCoolNumber,
    BetterThermostatPresetNumber,
)


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


class TestPresetCoolNumber:
    """Tests for cooling preset number behavior."""

    @pytest.mark.asyncio
    async def test_active_preset_stores_clamped_cooling_value(self):
        """The persisted cooling preset matches the value applied to the climate."""
        bt_climate = MagicMock()
        bt_climate.unique_id = "test_bt"
        bt_climate.device_name = "Test BT"
        bt_climate.min_temp = 5.0
        bt_climate.max_temp = 30.0
        bt_climate.target_temperature_step = 0.25
        bt_climate.bt_target_temp_step = 0.75
        bt_climate.preset_mode = PRESET_HOME
        bt_climate.bt_target_temp = 22.0
        bt_climate.bt_target_cooltemp = 24.0
        bt_climate.bt_hvac_mode = HVACMode.HEAT_COOL
        bt_climate._preset_cool_temperatures = {PRESET_HOME: 24.0}
        bt_climate.control_queue_task.put = AsyncMock()

        entity = BetterThermostatPresetCoolNumber(bt_climate, PRESET_HOME)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(20.0)

        assert bt_climate._preset_cool_temperatures[PRESET_HOME] == 22.75
        assert bt_climate.bt_target_cooltemp == 22.75
        bt_climate.control_queue_task.put.assert_awaited_once_with(bt_climate)

    @pytest.mark.asyncio
    async def test_active_preset_stores_unclamped_cooling_value(self):
        """Cooling values above the heat target are stored and applied verbatim."""
        bt_climate = MagicMock()
        bt_climate.unique_id = "test_bt"
        bt_climate.device_name = "Test BT"
        bt_climate.min_temp = 5.0
        bt_climate.max_temp = 30.0
        bt_climate.target_temperature_step = 0.25
        bt_climate.bt_target_temp_step = 0.75
        bt_climate.preset_mode = PRESET_HOME
        bt_climate.bt_target_temp = 22.0
        bt_climate.bt_target_cooltemp = 24.0
        bt_climate.bt_hvac_mode = HVACMode.HEAT_COOL
        bt_climate._preset_cool_temperatures = {PRESET_HOME: 24.0}
        bt_climate.control_queue_task.put = AsyncMock()

        entity = BetterThermostatPresetCoolNumber(bt_climate, PRESET_HOME)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(25.0)

        assert bt_climate._preset_cool_temperatures[PRESET_HOME] == 25.0
        assert bt_climate.bt_target_cooltemp == 25.0
        bt_climate.control_queue_task.put.assert_awaited_once_with(bt_climate)

    @pytest.mark.asyncio
    async def test_active_preset_keeps_the_cool_target_inside_the_range(self):
        """With the heat target at ``max_temp`` the cool target stops there.

        The pre-clamp bump lifts ``cool_value`` to ``heat + step`` and the
        ``max_temp`` clamp pulls it back down to ``max_temp``. The range holds no
        value above that, and both the active target and the persisted preset are
        written to the cooler, so the two targets meet at the maximum instead.
        """
        from custom_components.better_thermostat.climate import BetterThermostat

        bt_climate = MagicMock()
        bt_climate.unique_id = "test_bt"
        bt_climate.device_name = "Test BT"
        bt_climate.min_temp = 5.0
        bt_climate.max_temp = 30.0
        bt_climate.bt_max_temp = 30.0
        bt_climate.target_temperature_step = 0.5
        bt_climate.bt_target_temp_step = 0.5
        bt_climate.preset_mode = PRESET_HOME
        bt_climate.hvac_mode = HVACMode.HEAT_COOL
        bt_climate.bt_target_temp = 30.0
        bt_climate.bt_target_cooltemp = 30.0
        bt_climate.bt_hvac_mode = HVACMode.HEAT_COOL
        bt_climate._preset_cool_temperatures = {PRESET_HOME: 30.0}
        bt_climate.control_queue_task.put = AsyncMock()
        bt_climate._enforce_cool_above_heat.side_effect = lambda **kwargs: (
            BetterThermostat._enforce_cool_above_heat(bt_climate, **kwargs)
        )

        entity = BetterThermostatPresetCoolNumber(bt_climate, PRESET_HOME)
        entity.async_write_ha_state = MagicMock()

        await entity.async_set_native_value(20.0)

        assert bt_climate.bt_target_cooltemp == 30.0
        assert bt_climate._preset_cool_temperatures[PRESET_HOME] == 30.0
        bt_climate.control_queue_task.put.assert_awaited_once_with(bt_climate)

    @pytest.mark.asyncio
    async def test_restore_no_last_state_keeps_dict_empty(self):
        """No prior state leaves the cooling preset map unchanged."""
        bt_climate = MagicMock()
        bt_climate.unique_id = "test_bt"
        bt_climate.device_name = "Test BT"
        bt_climate.min_temp = 5.0
        bt_climate.max_temp = 30.0
        bt_climate.target_temperature_step = 0.5
        bt_climate.cooler_entity_id = "climate.cooler"
        bt_climate._preset_cool_temperatures = {}

        entity = BetterThermostatPresetCoolNumber(bt_climate, PRESET_HOME)
        entity.async_get_last_state = AsyncMock(return_value=None)

        await entity.async_added_to_hass()

        assert PRESET_HOME not in bt_climate._preset_cool_temperatures
