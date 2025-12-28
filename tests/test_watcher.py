"""Tests for watcher utility functions.

Tests the degraded mode functionality including entity availability checks,
optional vs critical sensor classification, and degraded mode state management.
"""

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def anyio_backend():
    """Configure anyio to use asyncio backend for async tests."""
    return "asyncio"


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.states = MagicMock()
    return hass


@pytest.fixture
def mock_bt_instance(mock_hass):
    """Create a mock BetterThermostat instance."""
    bt = MagicMock()
    bt.hass = mock_hass
    bt.device_name = "Test Thermostat"
    bt.sensor_entity_id = "sensor.room_temp"
    bt.window_id = "binary_sensor.window"
    bt.humidity_sensor_entity_id = "sensor.humidity"
    bt.outdoor_sensor = "sensor.outdoor_temp"
    bt.weather_entity = "weather.home"
    bt.real_trvs = {"climate.trv_1": {}, "climate.trv_2": {}}
    bt.devices_errors = []
    bt.degraded_mode = False
    bt.unavailable_sensors = []
    return bt


class TestIsEntityAvailable:
    """Tests for is_entity_available function."""

    def test_returns_false_for_none_entity(self, mock_hass):
        """Test that None entity returns False."""
        from custom_components.better_thermostat.utils.watcher import (
            is_entity_available,
        )

        result = is_entity_available(mock_hass, None)
        assert result is False

    def test_returns_false_for_missing_entity(self, mock_hass):
        """Test that non-existent entity returns False."""
        from custom_components.better_thermostat.utils.watcher import (
            is_entity_available,
        )

        mock_hass.states.get.return_value = None
        result = is_entity_available(mock_hass, "sensor.nonexistent")
        assert result is False

    def test_returns_false_for_unavailable_state(self, mock_hass):
        """Test that entity with 'unavailable' state returns False."""
        from custom_components.better_thermostat.utils.watcher import (
            is_entity_available,
        )

        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_hass.states.get.return_value = mock_state

        result = is_entity_available(mock_hass, "sensor.test")
        assert result is False

    def test_returns_false_for_unknown_state(self, mock_hass):
        """Test that entity with 'unknown' state returns False."""
        from custom_components.better_thermostat.utils.watcher import (
            is_entity_available,
        )

        mock_state = MagicMock()
        mock_state.state = "unknown"
        mock_hass.states.get.return_value = mock_state

        result = is_entity_available(mock_hass, "sensor.test")
        assert result is False

    def test_returns_true_for_valid_state(self, mock_hass):
        """Test that entity with valid state returns True."""
        from custom_components.better_thermostat.utils.watcher import (
            is_entity_available,
        )

        mock_state = MagicMock()
        mock_state.state = "21.5"
        mock_hass.states.get.return_value = mock_state

        result = is_entity_available(mock_hass, "sensor.temperature")
        assert result is True

    def test_returns_true_for_on_state(self, mock_hass):
        """Test that entity with 'on' state returns True."""
        from custom_components.better_thermostat.utils.watcher import (
            is_entity_available,
        )

        mock_state = MagicMock()
        mock_state.state = "on"
        mock_hass.states.get.return_value = mock_state

        result = is_entity_available(mock_hass, "binary_sensor.window")
        assert result is True


class TestGetOptionalSensors:
    """Tests for get_optional_sensors function."""

    def test_returns_all_optional_sensors(self, mock_bt_instance):
        """Test that all configured optional sensors are returned."""
        from custom_components.better_thermostat.utils.watcher import (
            get_optional_sensors,
        )

        result = get_optional_sensors(mock_bt_instance)

        assert "binary_sensor.window" in result
        assert "sensor.humidity" in result
        assert "sensor.outdoor_temp" in result
        assert "weather.home" in result
        assert len(result) == 4

    def test_excludes_none_sensors(self, mock_bt_instance):
        """Test that None sensors are not included."""
        from custom_components.better_thermostat.utils.watcher import (
            get_optional_sensors,
        )

        mock_bt_instance.window_id = None
        mock_bt_instance.humidity_sensor_entity_id = None

        result = get_optional_sensors(mock_bt_instance)

        assert "sensor.outdoor_temp" in result
        assert "weather.home" in result
        assert len(result) == 2

    def test_returns_empty_list_when_no_optional_sensors(self, mock_bt_instance):
        """Test that empty list is returned when no optional sensors configured."""
        from custom_components.better_thermostat.utils.watcher import (
            get_optional_sensors,
        )

        mock_bt_instance.window_id = None
        mock_bt_instance.humidity_sensor_entity_id = None
        mock_bt_instance.outdoor_sensor = None
        mock_bt_instance.weather_entity = None

        result = get_optional_sensors(mock_bt_instance)

        assert result == []


class TestGetCriticalEntities:
    """Tests for get_critical_entities function."""

    def test_returns_all_trvs(self, mock_bt_instance):
        """Test that all TRVs are returned as critical entities."""
        from custom_components.better_thermostat.utils.watcher import (
            get_critical_entities,
        )

        result = get_critical_entities(mock_bt_instance)

        assert "climate.trv_1" in result
        assert "climate.trv_2" in result
        assert len(result) == 2

    def test_returns_empty_list_when_no_trvs(self, mock_bt_instance):
        """Test that empty list is returned when no TRVs configured."""
        from custom_components.better_thermostat.utils.watcher import (
            get_critical_entities,
        )

        mock_bt_instance.real_trvs = {}

        result = get_critical_entities(mock_bt_instance)

        assert result == []


class TestCheckCriticalEntities:
    """Tests for check_critical_entities function."""

    @pytest.mark.anyio
    async def test_returns_true_when_all_trvs_available(self, mock_bt_instance):
        """Test that True is returned when all TRVs are available."""
        from custom_components.better_thermostat.utils.watcher import (
            check_critical_entities,
        )

        mock_state = MagicMock()
        mock_state.state = "heat"
        mock_bt_instance.hass.states.get.return_value = mock_state

        with patch("custom_components.better_thermostat.utils.watcher.ir"):
            result = await check_critical_entities(mock_bt_instance)

        assert result is True

    @pytest.mark.anyio
    async def test_returns_false_when_trv_unavailable(self, mock_bt_instance):
        """Test that False is returned when a TRV is unavailable."""
        from custom_components.better_thermostat.utils.watcher import (
            check_critical_entities,
        )

        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_bt_instance.hass.states.get.return_value = mock_state

        with patch("custom_components.better_thermostat.utils.watcher.ir"):
            result = await check_critical_entities(mock_bt_instance)

        assert result is False
        assert len(mock_bt_instance.devices_errors) > 0


class TestCheckAndUpdateDegradedMode:
    """Tests for check_and_update_degraded_mode function."""

    @pytest.mark.anyio
    async def test_sets_degraded_mode_when_optional_sensor_unavailable(
        self, mock_bt_instance
    ):
        """Test that degraded_mode is set when an optional sensor is unavailable."""
        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        def mock_get(entity_id):
            state = MagicMock()
            if entity_id == "binary_sensor.window":
                state.state = "unavailable"
            else:
                state.state = "20.0"
            return state

        mock_bt_instance.hass.states.get.side_effect = mock_get

        with patch("custom_components.better_thermostat.utils.watcher.ir"):
            result = await check_and_update_degraded_mode(mock_bt_instance)

        assert result is True
        assert mock_bt_instance.degraded_mode is True
        assert "binary_sensor.window" in mock_bt_instance.unavailable_sensors

    @pytest.mark.anyio
    async def test_no_degraded_mode_when_all_sensors_available(self, mock_bt_instance):
        """Test that degraded_mode is False when all sensors are available."""
        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        mock_state = MagicMock()
        mock_state.state = "20.0"
        mock_bt_instance.hass.states.get.return_value = mock_state

        with patch("custom_components.better_thermostat.utils.watcher.ir"):
            result = await check_and_update_degraded_mode(mock_bt_instance)

        assert result is False
        assert mock_bt_instance.degraded_mode is False
        assert mock_bt_instance.unavailable_sensors == []

    @pytest.mark.anyio
    async def test_includes_room_sensor_in_unavailable_list(self, mock_bt_instance):
        """Test that room temperature sensor is added to unavailable list when unavailable."""
        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        def mock_get(entity_id):
            state = MagicMock()
            if entity_id == "sensor.room_temp":
                state.state = "unavailable"
            else:
                state.state = "20.0"
            return state

        mock_bt_instance.hass.states.get.side_effect = mock_get

        with patch("custom_components.better_thermostat.utils.watcher.ir"):
            result = await check_and_update_degraded_mode(mock_bt_instance)

        assert result is True
        assert "sensor.room_temp" in mock_bt_instance.unavailable_sensors
