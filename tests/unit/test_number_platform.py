"""Comprehensive tests for Better Thermostat number platform.

Tests covering preset temperature numbers, PID parameter numbers, and valve max opening numbers.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate.const import PRESET_COMFORT, PRESET_ECO, PRESET_NONE
from homeassistant.config_entries import ConfigEntry

from custom_components.better_thermostat.number import (
    BetterThermostatPresetNumber,
    BetterThermostatPIDNumber,
    BetterThermostatValveMaxOpeningNumber,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    CalibrationMode,
    CalibrationType,
)


class TestAsyncSetupEntry:
    """Test async_setup_entry function."""

    @pytest.mark.asyncio
    async def test_setup_entry_creates_preset_numbers(self):
        """Test setup_entry creates number entities for preset modes."""
        mock_hass = MagicMock()
        mock_entry = MagicMock(spec=ConfigEntry)
        mock_entry.entry_id = "test_entry"
        mock_add_entities = AsyncMock()

        # Mock climate entity with presets
        mock_climate = MagicMock()
        mock_climate.preset_modes = [PRESET_ECO, PRESET_COMFORT]
        mock_climate.unique_id = "test_bt_climate"

        mock_hass.data = {"better_thermostat": {mock_entry.entry_id: {"climate": mock_climate}}}

        await async_setup_entry(mock_hass, mock_entry, mock_add_entities)

        # Should create 2 preset numbers (excluding PRESET_NONE)
        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        preset_entities = [e for e in entities if isinstance(e, BetterThermostatPresetNumber)]
        assert len(preset_entities) == 2

    @pytest.mark.asyncio
    async def test_setup_entry_creates_pid_numbers_when_enabled(self):
        """Test setup_entry creates PID number entities when PID calibration is enabled."""
        mock_hass = MagicMock()
        mock_entry = MagicMock(spec=ConfigEntry)
        mock_entry.entry_id = "test_entry"
        mock_add_entities = AsyncMock()

        mock_climate = MagicMock()
        mock_climate.preset_modes = [PRESET_NONE]
        mock_climate.unique_id = "test_bt_climate"
        mock_climate.all_trvs = [
            {
                "trv": "climate.test_trv",
                "advanced": {
                    CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION,
                    CONF_CALIBRATION: CalibrationType.TARGET_TEMP_BASED,
                },
            }
        ]

        mock_hass.data = {"better_thermostat": {mock_entry.entry_id: {"climate": mock_climate}}}

        await async_setup_entry(mock_hass, mock_entry, mock_add_entities)

        entities = mock_add_entities.call_args[0][0]
        pid_entities = [e for e in entities if isinstance(e, BetterThermostatPIDNumber)]
        # Should create 3 PID entities (kp, ki, kd)
        assert len(pid_entities) == 3

    @pytest.mark.asyncio
    async def test_setup_entry_creates_valve_max_opening_when_direct_valve(self):
        """Test setup_entry creates valve max opening number for direct valve control."""
        mock_hass = MagicMock()
        mock_entry = MagicMock(spec=ConfigEntry)
        mock_entry.entry_id = "test_entry"
        mock_add_entities = AsyncMock()

        mock_climate = MagicMock()
        mock_climate.preset_modes = [PRESET_NONE]
        mock_climate.unique_id = "test_bt_climate"
        mock_climate.all_trvs = [
            {
                "trv": "climate.test_trv",
                "advanced": {
                    CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION,
                    CONF_CALIBRATION: CalibrationType.DIRECT_VALVE_BASED,
                },
            }
        ]

        mock_hass.data = {"better_thermostat": {mock_entry.entry_id: {"climate": mock_climate}}}

        await async_setup_entry(mock_hass, mock_entry, mock_add_entities)

        entities = mock_add_entities.call_args[0][0]
        valve_entities = [e for e in entities if isinstance(e, BetterThermostatValveMaxOpeningNumber)]
        assert len(valve_entities) == 1

    @pytest.mark.asyncio
    async def test_setup_entry_handles_missing_climate(self):
        """Test setup_entry handles missing climate entity gracefully."""
        mock_hass = MagicMock()
        mock_entry = MagicMock(spec=ConfigEntry)
        mock_entry.entry_id = "test_entry"
        mock_add_entities = AsyncMock()

        mock_hass.data = {"better_thermostat": {mock_entry.entry_id: {}}}  # No climate

        await async_setup_entry(mock_hass, mock_entry, mock_add_entities)

        # Should not add any entities
        mock_add_entities.assert_not_called()


class TestBetterThermostatPresetNumber:
    """Test BetterThermostatPresetNumber class."""

    def test_preset_number_initialization(self):
        """Test preset number initializes with correct attributes."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate.min_temp = 10.0
        mock_climate.max_temp = 30.0
        mock_climate.target_temperature_step = 0.5

        number = BetterThermostatPresetNumber(mock_climate, PRESET_ECO)

        assert number._preset_mode == PRESET_ECO
        assert number._attr_native_min_value == 10.0
        assert number._attr_native_max_value == 30.0
        assert number._attr_native_step == 0.5

    @pytest.mark.asyncio
    async def test_preset_number_restores_state(self):
        """Test preset number restores value from last state."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate._preset_temperatures = {}
        mock_climate.min_temp = 10.0
        mock_climate.max_temp = 30.0
        mock_climate.target_temperature_step = 0.5

        number = BetterThermostatPresetNumber(mock_climate, PRESET_ECO)

        # Mock last state
        mock_last_state = MagicMock()
        mock_last_state.state = "18.5"
        number.async_get_last_state = AsyncMock(return_value=mock_last_state)

        await number.async_added_to_hass()

        # Should restore value
        assert mock_climate._preset_temperatures[PRESET_ECO] == 18.5

    def test_preset_number_native_value(self):
        """Test preset number returns current value."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate._preset_temperatures = {PRESET_ECO: 19.0}
        mock_climate.min_temp = 10.0
        mock_climate.max_temp = 30.0
        mock_climate.target_temperature_step = 0.5

        number = BetterThermostatPresetNumber(mock_climate, PRESET_ECO)

        assert number.native_value == 19.0

    @pytest.mark.asyncio
    async def test_preset_number_set_value_updates_climate_when_active(self):
        """Test setting preset value updates climate when preset is active."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate._preset_temperatures = {PRESET_ECO: 19.0}
        mock_climate.preset_mode = PRESET_ECO  # Active preset
        mock_climate.async_set_temperature = AsyncMock()
        mock_climate.min_temp = 10.0
        mock_climate.max_temp = 30.0
        mock_climate.target_temperature_step = 0.5

        number = BetterThermostatPresetNumber(mock_climate, PRESET_ECO)
        number.async_write_ha_state = MagicMock()

        await number.async_set_native_value(20.0)

        # Should update storage
        assert mock_climate._preset_temperatures[PRESET_ECO] == 20.0
        # Should update climate temperature
        mock_climate.async_set_temperature.assert_called_once_with(temperature=20.0)

    @pytest.mark.asyncio
    async def test_preset_number_set_value_no_update_when_inactive(self):
        """Test setting preset value does not update climate when preset is inactive."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate._preset_temperatures = {PRESET_ECO: 19.0}
        mock_climate.preset_mode = PRESET_COMFORT  # Different preset
        mock_climate.async_set_temperature = AsyncMock()
        mock_climate.min_temp = 10.0
        mock_climate.max_temp = 30.0
        mock_climate.target_temperature_step = 0.5

        number = BetterThermostatPresetNumber(mock_climate, PRESET_ECO)
        number.async_write_ha_state = MagicMock()

        await number.async_set_native_value(20.0)

        # Should update storage
        assert mock_climate._preset_temperatures[PRESET_ECO] == 20.0
        # Should NOT update climate temperature
        mock_climate.async_set_temperature.assert_not_called()


class TestBetterThermostatPIDNumber:
    """Test BetterThermostatPIDNumber class."""

    def test_pid_number_initialization_kp(self):
        """Test PID number initializes correctly for Kp parameter."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate.hass.states.get.return_value = MagicMock(name="Test TRV")

        number = BetterThermostatPIDNumber(mock_climate, "climate.test", "kp", show_trv_name=False)

        assert number._parameter == "kp"
        assert number._attr_native_min_value == 0.0
        assert number._attr_native_max_value == 1000.0
        assert number._attr_native_step == 0.1

    def test_pid_number_initialization_ki(self):
        """Test PID number initializes correctly for Ki parameter."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate.hass.states.get.return_value = MagicMock(name="Test TRV")

        number = BetterThermostatPIDNumber(mock_climate, "climate.test", "ki", show_trv_name=False)

        assert number._parameter == "ki"
        assert number._attr_native_min_value == 0.0
        assert number._attr_native_max_value == 100.0
        assert number._attr_native_step == 0.001

    def test_pid_number_initialization_kd(self):
        """Test PID number initializes correctly for Kd parameter."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate.hass.states.get.return_value = MagicMock(name="Test TRV")

        number = BetterThermostatPIDNumber(mock_climate, "climate.test", "kd", show_trv_name=False)

        assert number._parameter == "kd"
        assert number._attr_native_min_value == 0.0
        assert number._attr_native_max_value == 10000.0
        assert number._attr_native_step == 1.0

    @pytest.mark.asyncio
    async def test_pid_number_set_value_updates_pid_state(self):
        """Test setting PID value updates PID state."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate.device_name = "test"
        mock_climate.schedule_save_pid_state = MagicMock()
        mock_climate.hass.states.get.return_value = MagicMock(name="Test TRV")

        number = BetterThermostatPIDNumber(
            mock_climate, "climate.test", "kp", show_trv_name=False
        )
        number.async_write_ha_state = MagicMock()

        with patch(
            "custom_components.better_thermostat.number._PID_STATES", {}
        ) as mock_states:
            await number.async_set_native_value(50.0)

            # Should schedule save
            mock_climate.schedule_save_pid_state.assert_called_once()


class TestBetterThermostatValveMaxOpeningNumber:
    """Test BetterThermostatValveMaxOpeningNumber class."""

    def test_valve_max_opening_initialization(self):
        """Test valve max opening number initializes correctly."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate.hass.states.get.return_value = MagicMock(name="Test TRV")

        number = BetterThermostatValveMaxOpeningNumber(
            mock_climate, "climate.test", show_trv_name=False
        )

        assert number._attr_native_min_value == 0.0
        assert number._attr_native_max_value == 100.0
        assert number._attr_native_step == 1.0

    @pytest.mark.asyncio
    async def test_valve_max_opening_restores_state(self):
        """Test valve max opening restores value from last state."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate.real_trvs = {"climate.test": {}}
        mock_climate.hass.states.get.return_value = MagicMock(name="Test TRV")

        number = BetterThermostatValveMaxOpeningNumber(
            mock_climate, "climate.test", show_trv_name=False
        )

        mock_last_state = MagicMock()
        mock_last_state.state = "75.0"
        number.async_get_last_state = AsyncMock(return_value=mock_last_state)

        await number.async_added_to_hass()

        # Should restore value
        assert mock_climate.real_trvs["climate.test"]["valve_max_opening"] == 75.0

    def test_valve_max_opening_get_value(self):
        """Test valve max opening returns current value."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate.real_trvs = {"climate.test": {"valve_max_opening": 80.0}}
        mock_climate.hass.states.get.return_value = MagicMock(name="Test TRV")

        number = BetterThermostatValveMaxOpeningNumber(
            mock_climate, "climate.test", show_trv_name=False
        )

        assert number.native_value == 80.0

    @pytest.mark.asyncio
    async def test_valve_max_opening_set_value_clamped(self):
        """Test valve max opening value is clamped to 0-100."""
        mock_climate = MagicMock()
        mock_climate.unique_id = "test_climate"
        mock_climate.real_trvs = {"climate.test": {}}
        mock_climate.hass.states.get.return_value = MagicMock(name="Test TRV")

        number = BetterThermostatValveMaxOpeningNumber(
            mock_climate, "climate.test", show_trv_name=False
        )
        number.async_write_ha_state = MagicMock()

        # Test clamping to max
        await number.async_set_native_value(150.0)
        assert mock_climate.real_trvs["climate.test"]["valve_max_opening"] == 100.0

        # Test clamping to min
        await number.async_set_native_value(-10.0)
        assert mock_climate.real_trvs["climate.test"]["valve_max_opening"] == 0.0


class TestAsyncUnloadEntry:
    """Test async_unload_entry function."""

    @pytest.mark.asyncio
    async def test_unload_entry_cleans_up_tracking(self):
        """Test unload_entry cleans up tracking data."""
        mock_hass = MagicMock()
        mock_entry = MagicMock(spec=ConfigEntry)
        mock_entry.entry_id = "test_entry"

        with patch(
            "custom_components.better_thermostat.number._ACTIVE_PRESET_NUMBERS", {"test_entry": {}}
        ), patch(
            "custom_components.better_thermostat.number._ACTIVE_PID_NUMBERS", {"test_entry": {}}
        ):
            result = await async_unload_entry(mock_hass, mock_entry)

            assert result is True