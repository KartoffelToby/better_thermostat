"""Tests for watcher utility functions.

Tests the degraded mode functionality including entity availability checks,
optional vs critical sensor classification, and degraded mode state management.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest


def _close_coro(coro, *args, **kwargs):
    """Close a coroutine to avoid 'never awaited' RuntimeWarning."""
    if inspect.iscoroutine(coro):
        coro.close()


@pytest.fixture
def anyio_backend():
    """Configure anyio to use asyncio backend for async tests."""
    return "asyncio"


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_close_coro)
    hass.async_create_background_task = MagicMock(side_effect=_close_coro)
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
    bt._degraded_grace_until = None
    bt._degraded_warning_emitted = False
    bt._critical_grace_until = None
    bt.is_removed = False
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

    @pytest.mark.anyio
    async def test_no_issue_during_grace_period(self, mock_bt_instance):
        """Unavailable TRV during startup grace does not raise a repair issue."""
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        from custom_components.better_thermostat.utils.watcher import (
            check_critical_entities,
        )

        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_bt_instance.hass.states.get.return_value = mock_state
        mock_bt_instance._critical_grace_until = dt_util.now() + timedelta(minutes=2)

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            result = await check_critical_entities(mock_bt_instance)

        assert result is False
        assert len(mock_bt_instance.devices_errors) == 0
        assert not mock_ir.async_create_issue.called

    @pytest.mark.anyio
    async def test_issue_after_grace_expires(self, mock_bt_instance):
        """Unavailable TRV after grace expiry raises a repair issue."""
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        from custom_components.better_thermostat.utils.watcher import (
            check_critical_entities,
        )

        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_bt_instance.hass.states.get.return_value = mock_state
        # Grace expired one minute ago
        mock_bt_instance._critical_grace_until = dt_util.now() - timedelta(minutes=1)

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            result = await check_critical_entities(mock_bt_instance)

        assert result is False
        assert len(mock_bt_instance.devices_errors) > 0
        assert mock_ir.async_create_issue.called

    @pytest.mark.anyio
    async def test_auto_clear_issue_on_recovery(self, mock_bt_instance):
        """Issue is cleared when a previously-unavailable TRV becomes available."""
        from custom_components.better_thermostat.utils.watcher import (
            check_critical_entities,
        )

        mock_state = MagicMock()
        mock_state.state = "heat"
        mock_bt_instance.hass.states.get.return_value = mock_state
        # Simulate a previously raised error
        mock_bt_instance.devices_errors = ["climate.trv_1", "climate.trv_2"]

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            result = await check_critical_entities(mock_bt_instance)

        assert result is True
        assert len(mock_bt_instance.devices_errors) == 0
        # Issue must be deleted for each recovered TRV
        assert mock_ir.async_delete_issue.call_count == 2

    @pytest.mark.anyio
    async def test_clears_stale_issue_even_without_devices_errors(
        self, mock_bt_instance
    ):
        """A stale issue is cleared even when entity was never tracked as errored."""
        from custom_components.better_thermostat.utils.watcher import (
            check_critical_entities,
        )

        mock_state = MagicMock()
        mock_state.state = "heat"
        mock_bt_instance.hass.states.get.return_value = mock_state
        mock_bt_instance.devices_errors = []

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            result = await check_critical_entities(mock_bt_instance)

        assert result is True
        # delete is called idempotently for every available entity
        assert mock_ir.async_delete_issue.call_count == 2

    @pytest.mark.anyio
    async def test_partial_unavailability_clears_available(self, mock_bt_instance):
        """Available TRVs are processed even when another TRV is unavailable."""
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        from custom_components.better_thermostat.utils.watcher import (
            check_critical_entities,
        )

        def mock_get(entity_id):
            state = MagicMock()
            state.state = "unavailable" if entity_id == "climate.trv_1" else "heat"
            return state

        mock_bt_instance.hass.states.get.side_effect = mock_get
        mock_bt_instance._critical_grace_until = dt_util.now() - timedelta(minutes=1)
        mock_bt_instance.devices_errors = ["climate.trv_2"]

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            result = await check_critical_entities(mock_bt_instance)

        assert result is False
        # trv_1 (unavailable) added to errors and issue created
        assert "climate.trv_1" in mock_bt_instance.devices_errors
        # trv_2 (available) recovered — removed from errors and issue deleted
        assert "climate.trv_2" not in mock_bt_instance.devices_errors
        assert mock_ir.async_create_issue.called
        assert mock_ir.async_delete_issue.called


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

    @pytest.mark.anyio
    async def test_calls_get_battery_status_for_available_sensors(
        self, mock_bt_instance
    ):
        """Test that get_battery_status is called for available sensors."""
        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        mock_state = MagicMock()
        mock_state.state = "20.0"
        mock_bt_instance.hass.states.get.return_value = mock_state

        with patch("custom_components.better_thermostat.utils.watcher.ir"):
            with patch(
                "custom_components.better_thermostat.utils.watcher.get_battery_status"
            ):
                await check_and_update_degraded_mode(mock_bt_instance)

                # Should be called for all available optional sensors + room sensor
                # 4 optional sensors + 1 room sensor = 5 calls
                assert (
                    mock_bt_instance.hass.async_create_background_task.call_count == 5
                )


class TestDegradedModeGracePeriod:
    """Tests for the startup grace period that suppresses degraded-mode noise."""

    @staticmethod
    def _mock_get_with_unavailable(unavailable_id):
        def mock_get(entity_id):
            state = MagicMock()
            state.state = "unavailable" if entity_id == unavailable_id else "20.0"
            return state

        return mock_get

    @pytest.mark.anyio
    async def test_warning_and_issue_when_grace_inactive(
        self, mock_bt_instance, caplog
    ):
        """No grace set → first degraded transition logs WARNING and raises issue."""
        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        mock_bt_instance.hass.states.get.side_effect = self._mock_get_with_unavailable(
            "binary_sensor.window"
        )
        mock_bt_instance._degraded_grace_until = None

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            with caplog.at_level("WARNING"):
                await check_and_update_degraded_mode(mock_bt_instance)

        assert any("Entering degraded mode" in r.message for r in caplog.records)
        assert mock_ir.async_create_issue.called
        assert mock_bt_instance._degraded_warning_emitted is True

    @pytest.mark.anyio
    async def test_silent_during_grace_period(self, mock_bt_instance, caplog):
        """Grace active → degraded transition logs DEBUG, no issue, no WARNING."""
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        mock_bt_instance.hass.states.get.side_effect = self._mock_get_with_unavailable(
            "binary_sensor.window"
        )
        mock_bt_instance._degraded_grace_until = dt_util.now() + timedelta(minutes=5)

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            with caplog.at_level("WARNING"):
                await check_and_update_degraded_mode(mock_bt_instance)

        assert mock_bt_instance.degraded_mode is True
        assert not any("Entering degraded mode" in r.message for r in caplog.records)
        assert not mock_ir.async_create_issue.called
        assert mock_bt_instance._degraded_warning_emitted is False

    @pytest.mark.anyio
    async def test_warns_after_grace_expires(self, mock_bt_instance, caplog):
        """Grace passed → still-degraded re-check logs WARNING and raises issue."""
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        mock_bt_instance.hass.states.get.side_effect = self._mock_get_with_unavailable(
            "binary_sensor.window"
        )
        # Grace expired 1 minute ago
        mock_bt_instance._degraded_grace_until = dt_util.now() - timedelta(minutes=1)
        # Simulate that the silent-during-grace check already set degraded=True
        mock_bt_instance.degraded_mode = True

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            with caplog.at_level("WARNING"):
                await check_and_update_degraded_mode(mock_bt_instance)

        assert any("Entering degraded mode" in r.message for r in caplog.records)
        assert mock_ir.async_create_issue.called
        assert mock_bt_instance._degraded_warning_emitted is True

    @pytest.mark.anyio
    async def test_silent_recovery_during_grace(self, mock_bt_instance, caplog):
        """Recover during grace → no INFO log, no issue deleted (none was created)."""
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        # All sensors are available now
        mock_state = MagicMock()
        mock_state.state = "20.0"
        mock_bt_instance.hass.states.get.return_value = mock_state
        # Mock was previously set to degraded silently during grace
        mock_bt_instance._degraded_grace_until = dt_util.now() + timedelta(minutes=5)
        mock_bt_instance.degraded_mode = True
        mock_bt_instance._degraded_warning_emitted = False

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            with caplog.at_level("INFO"):
                await check_and_update_degraded_mode(mock_bt_instance)

        assert mock_bt_instance.degraded_mode is False
        assert not any("Exiting degraded mode" in r.message for r in caplog.records)
        assert not mock_ir.async_delete_issue.called

    @pytest.mark.anyio
    async def test_info_on_recovery_after_warned(self, mock_bt_instance, caplog):
        """Recover after we'd warned → INFO log + issue deleted."""
        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        mock_state = MagicMock()
        mock_state.state = "20.0"
        mock_bt_instance.hass.states.get.return_value = mock_state
        mock_bt_instance.degraded_mode = True
        mock_bt_instance._degraded_warning_emitted = True

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            with caplog.at_level("INFO"):
                await check_and_update_degraded_mode(mock_bt_instance)

        assert mock_bt_instance.degraded_mode is False
        assert any("Exiting degraded mode" in r.message for r in caplog.records)
        assert mock_ir.async_delete_issue.called
        assert mock_bt_instance._degraded_warning_emitted is False

    @pytest.mark.anyio
    async def test_no_double_warning(self, mock_bt_instance, caplog):
        """Subsequent check while already-warned → no second WARNING."""
        from custom_components.better_thermostat.utils.watcher import (
            check_and_update_degraded_mode,
        )

        mock_bt_instance.hass.states.get.side_effect = self._mock_get_with_unavailable(
            "binary_sensor.window"
        )
        mock_bt_instance.degraded_mode = True
        mock_bt_instance._degraded_warning_emitted = True

        with patch("custom_components.better_thermostat.utils.watcher.ir") as mock_ir:
            with caplog.at_level("WARNING"):
                await check_and_update_degraded_mode(mock_bt_instance)

        assert not any("Entering degraded mode" in r.message for r in caplog.records)
        assert not mock_ir.async_create_issue.called


class TestAwaitOptionalSensors:
    """Tests for await_optional_sensors retry logic.

    Uses asyncio.run() to avoid the HA event-loop-policy issue that
    affects @pytest.mark.anyio tests in this project.
    """

    @staticmethod
    def _run(coro):
        """Run a coroutine in a fresh event loop (avoids HA plugin issues)."""
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_returns_empty_when_all_sensors_available_immediately(
        self, mock_bt_instance
    ):
        """All optional sensors available on first check → no sleep, empty result."""
        from custom_components.better_thermostat.utils.watcher import (
            await_optional_sensors,
        )

        mock_state = MagicMock()
        mock_state.state = "20.0"
        mock_bt_instance.hass.states.get.return_value = mock_state

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_optional_sensors(
                mock_bt_instance, delays=(3, 5, 10), _sleep=fake_sleep
            )
        )

        assert result == []
        assert sleep_calls == [], "Should not sleep when all sensors are available"

    def test_returns_empty_when_no_optional_sensors_configured(self, mock_bt_instance):
        """No optional sensors configured → immediate empty result."""
        from custom_components.better_thermostat.utils.watcher import (
            await_optional_sensors,
        )

        mock_bt_instance.window_id = None
        mock_bt_instance.humidity_sensor_entity_id = None
        mock_bt_instance.outdoor_sensor = None
        mock_bt_instance.weather_entity = None

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_optional_sensors(mock_bt_instance, delays=(3, 5), _sleep=fake_sleep)
        )

        assert result == []
        assert sleep_calls == []

    def test_retries_until_sensor_comes_online(self, mock_bt_instance):
        """Sensor unavailable on first check, available on second → one sleep."""
        from custom_components.better_thermostat.utils.watcher import (
            await_optional_sensors,
        )

        # Only outdoor sensor configured
        mock_bt_instance.window_id = None
        mock_bt_instance.humidity_sensor_entity_id = None
        mock_bt_instance.weather_entity = None

        call_count = 0

        def mock_get(entity_id):
            nonlocal call_count
            state = MagicMock()
            if entity_id == "sensor.outdoor_temp":
                call_count += 1
                # Unavailable on first call, available from second onwards
                state.state = "unavailable" if call_count <= 1 else "15.0"
            else:
                state.state = "20.0"
            return state

        mock_bt_instance.hass.states.get.side_effect = mock_get

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_optional_sensors(
                mock_bt_instance, delays=(3, 5, 10), _sleep=fake_sleep
            )
        )

        assert result == []
        assert sleep_calls == [3], "Should sleep once (3 s) before sensor comes online"

    def test_returns_pending_after_all_retries_exhausted(self, mock_bt_instance):
        """Sensor stays unavailable through all retries → returned in pending list."""
        from custom_components.better_thermostat.utils.watcher import (
            await_optional_sensors,
        )

        # Only outdoor sensor configured, permanently unavailable
        mock_bt_instance.window_id = None
        mock_bt_instance.humidity_sensor_entity_id = None
        mock_bt_instance.weather_entity = None

        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_bt_instance.hass.states.get.return_value = mock_state

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_optional_sensors(
                mock_bt_instance, delays=(2, 4, 8), _sleep=fake_sleep
            )
        )

        assert result == ["sensor.outdoor_temp"]
        assert sleep_calls == [2, 4, 8], "Should sleep through all delays"

    def test_delays_are_increasing(self, mock_bt_instance):
        """Verify that the actual sleep durations match the configured delays."""
        from custom_components.better_thermostat.utils.watcher import (
            DEFAULT_OPTIONAL_SENSOR_DELAYS,
            await_optional_sensors,
        )

        # All sensors permanently unavailable
        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_bt_instance.hass.states.get.return_value = mock_state

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        self._run(await_optional_sensors(mock_bt_instance, _sleep=fake_sleep))

        assert sleep_calls == list(DEFAULT_OPTIONAL_SENSOR_DELAYS)
        # Verify delays are strictly increasing
        for i in range(1, len(sleep_calls)):
            assert sleep_calls[i] > sleep_calls[i - 1], (
                f"Delay {i} ({sleep_calls[i]}) should be greater than "
                f"delay {i - 1} ({sleep_calls[i - 1]})"
            )

    def test_default_delays_total_roughly_60s(self):
        """The default delays should sum to approximately 60 seconds."""
        from custom_components.better_thermostat.utils.watcher import (
            DEFAULT_OPTIONAL_SENSOR_DELAYS,
        )

        total = sum(DEFAULT_OPTIONAL_SENSOR_DELAYS)
        assert 55 <= total <= 65, f"Expected ~60 s total, got {total} s"

    def test_custom_delays_are_respected(self, mock_bt_instance):
        """Passing custom delays overrides the default schedule."""
        from custom_components.better_thermostat.utils.watcher import (
            await_optional_sensors,
        )

        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_bt_instance.hass.states.get.return_value = mock_state

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        custom = (1, 2)
        self._run(
            await_optional_sensors(mock_bt_instance, delays=custom, _sleep=fake_sleep)
        )

        assert sleep_calls == [1, 2]

    def test_partial_sensors_come_online(self, mock_bt_instance):
        """Some sensors come online while others stay unavailable."""
        from custom_components.better_thermostat.utils.watcher import (
            await_optional_sensors,
        )

        # Only outdoor + weather configured
        mock_bt_instance.window_id = None
        mock_bt_instance.humidity_sensor_entity_id = None

        outdoor_calls = 0

        def mock_get(entity_id):
            nonlocal outdoor_calls
            state = MagicMock()
            if entity_id == "sensor.outdoor_temp":
                outdoor_calls += 1
                # Comes online after first sleep
                state.state = "unavailable" if outdoor_calls <= 1 else "12.0"
            elif entity_id == "weather.home":
                # Stays unavailable forever
                state.state = "unavailable"
            else:
                state.state = "20.0"
            return state

        mock_bt_instance.hass.states.get.side_effect = mock_get

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_optional_sensors(
                mock_bt_instance, delays=(2, 4, 8), _sleep=fake_sleep
            )
        )

        # weather.home never comes online
        assert result == ["weather.home"]
        assert sleep_calls == [2, 4, 8]

    def test_final_check_after_last_sleep(self, mock_bt_instance):
        """Sensor comes online during the last sleep → caught by final check."""
        from custom_components.better_thermostat.utils.watcher import (
            await_optional_sensors,
        )

        mock_bt_instance.window_id = None
        mock_bt_instance.humidity_sensor_entity_id = None
        mock_bt_instance.weather_entity = None

        get_count = 0

        def mock_get(entity_id):
            nonlocal get_count
            state = MagicMock()
            if entity_id == "sensor.outdoor_temp":
                get_count += 1
                # With delays=(2, 4):
                # loop check 1: get_count=1 → unavailable → sleep(2)
                # loop check 2: get_count=2 → unavailable → sleep(4)
                # final check:  get_count=3 → available
                state.state = "unavailable" if get_count <= 2 else "10.0"
            else:
                state.state = "20.0"
            return state

        mock_bt_instance.hass.states.get.side_effect = mock_get

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_optional_sensors(mock_bt_instance, delays=(2, 4), _sleep=fake_sleep)
        )

        assert result == [], (
            "Sensor came online during last sleep, final check should catch it"
        )
        assert sleep_calls == [2, 4]


class TestAwaitCriticalEntities:
    """Tests for await_critical_entities retry logic.

    Mirrors TestAwaitOptionalSensors but for critical entities (TRVs), which
    on cloud-backed integrations (e.g. Tado) may take longer to initialise
    than Home Assistant itself.
    """

    @staticmethod
    def _run(coro):
        """Run a coroutine in a fresh event loop (avoids HA plugin issues)."""
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_returns_empty_when_all_trvs_available_immediately(self, mock_bt_instance):
        """All TRVs available on first check → no sleep, empty result."""
        from custom_components.better_thermostat.utils.watcher import (
            await_critical_entities,
        )

        mock_state = MagicMock()
        mock_state.state = "heat"
        mock_bt_instance.hass.states.get.return_value = mock_state

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_critical_entities(
                mock_bt_instance, delays=(3, 5, 10), _sleep=fake_sleep
            )
        )

        assert result == []
        assert sleep_calls == [], "Should not sleep when all TRVs are available"

    def test_returns_empty_when_no_trvs_configured(self, mock_bt_instance):
        """No critical entities configured → immediate empty result."""
        from custom_components.better_thermostat.utils.watcher import (
            await_critical_entities,
        )

        mock_bt_instance.real_trvs = {}

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_critical_entities(mock_bt_instance, delays=(3, 5), _sleep=fake_sleep)
        )

        assert result == []
        assert sleep_calls == []

    def test_retries_until_trv_comes_online(self, mock_bt_instance):
        """TRV unavailable on first check, available on second → one sleep."""
        from custom_components.better_thermostat.utils.watcher import (
            await_critical_entities,
        )

        # Single TRV configured
        mock_bt_instance.real_trvs = {"climate.trv_1": {}}

        call_count = 0

        def mock_get(entity_id):
            nonlocal call_count
            state = MagicMock()
            if entity_id == "climate.trv_1":
                call_count += 1
                # Unavailable on first call, available from second onwards
                state.state = "unavailable" if call_count <= 1 else "heat"
            else:
                state.state = "heat"
            return state

        mock_bt_instance.hass.states.get.side_effect = mock_get

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_critical_entities(
                mock_bt_instance, delays=(3, 5, 10), _sleep=fake_sleep
            )
        )

        assert result == []
        assert sleep_calls == [3], "Should sleep once (3 s) before TRV comes online"

    def test_stops_early_when_removed_mid_wait(self, mock_bt_instance):
        """A teardown during the wait aborts the retry schedule immediately."""
        from custom_components.better_thermostat.utils.watcher import (
            await_critical_entities,
        )

        mock_bt_instance.real_trvs = {"climate.trv_1": {}}
        mock_state = MagicMock()
        mock_state.state = "unavailable"  # never comes online
        mock_bt_instance.hass.states.get.return_value = mock_state

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)
            # Instance torn down while we were waiting.
            mock_bt_instance.is_removed = True

        result = self._run(
            await_critical_entities(
                mock_bt_instance, delays=(3, 5, 10, 15), _sleep=fake_sleep
            )
        )

        # Only the first delay elapses; the post-sleep is_removed check returns
        # instead of running the remaining schedule.
        assert sleep_calls == [3]
        assert result == ["climate.trv_1"]

    def test_returns_pending_after_all_retries_exhausted(self, mock_bt_instance):
        """TRV stays unavailable through all retries → returned in pending list."""
        from custom_components.better_thermostat.utils.watcher import (
            await_critical_entities,
        )

        mock_bt_instance.real_trvs = {"climate.trv_1": {}}

        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_bt_instance.hass.states.get.return_value = mock_state

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_critical_entities(
                mock_bt_instance, delays=(2, 4, 8), _sleep=fake_sleep
            )
        )

        assert result == ["climate.trv_1"]
        assert sleep_calls == [2, 4, 8], "Should sleep through all delays"

    def test_default_delays_increasing_and_total_roughly_90s(self):
        """Default critical delays are increasing and sum to roughly 90 s."""
        from custom_components.better_thermostat.utils.watcher import (
            DEFAULT_CRITICAL_ENTITY_DELAYS,
        )

        total = sum(DEFAULT_CRITICAL_ENTITY_DELAYS)
        assert 80 <= total <= 100, f"Expected ~90 s total, got {total} s"
        for i in range(1, len(DEFAULT_CRITICAL_ENTITY_DELAYS)):
            assert (
                DEFAULT_CRITICAL_ENTITY_DELAYS[i]
                > DEFAULT_CRITICAL_ENTITY_DELAYS[i - 1]
            )

    def test_partial_trvs_come_online(self, mock_bt_instance):
        """One TRV comes online while another stays unavailable."""
        from custom_components.better_thermostat.utils.watcher import (
            await_critical_entities,
        )

        mock_bt_instance.real_trvs = {"climate.trv_1": {}, "climate.trv_2": {}}

        trv1_calls = 0

        def mock_get(entity_id):
            nonlocal trv1_calls
            state = MagicMock()
            if entity_id == "climate.trv_1":
                trv1_calls += 1
                # Comes online after first sleep
                state.state = "unavailable" if trv1_calls <= 1 else "heat"
            elif entity_id == "climate.trv_2":
                # Stays unavailable forever
                state.state = "unavailable"
            else:
                state.state = "heat"
            return state

        mock_bt_instance.hass.states.get.side_effect = mock_get

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_critical_entities(
                mock_bt_instance, delays=(2, 4, 8), _sleep=fake_sleep
            )
        )

        assert result == ["climate.trv_2"]
        assert sleep_calls == [2, 4, 8]

    def test_final_check_after_last_sleep(self, mock_bt_instance):
        """TRV comes online during the last sleep → caught by final check."""
        from custom_components.better_thermostat.utils.watcher import (
            await_critical_entities,
        )

        mock_bt_instance.real_trvs = {"climate.trv_1": {}}

        get_count = 0

        def mock_get(entity_id):
            nonlocal get_count
            state = MagicMock()
            if entity_id == "climate.trv_1":
                get_count += 1
                # With delays=(2, 4): unavailable for the two loop checks,
                # available on the final check.
                state.state = "unavailable" if get_count <= 2 else "heat"
            else:
                state.state = "heat"
            return state

        mock_bt_instance.hass.states.get.side_effect = mock_get

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = self._run(
            await_critical_entities(mock_bt_instance, delays=(2, 4), _sleep=fake_sleep)
        )

        assert result == [], (
            "TRV came online during last sleep, final check should catch it"
        )
        assert sleep_calls == [2, 4]


class TestBatteryStatusCalls:
    """Tests for battery status updates in entity checks."""

    @pytest.mark.anyio
    async def test_check_critical_entities_calls_battery_status(self, mock_bt_instance):
        """Test that check_critical_entities calls get_battery_status for available TRVs."""
        from custom_components.better_thermostat.utils.watcher import (
            check_critical_entities,
        )

        mock_state = MagicMock()
        mock_state.state = "heat"
        mock_bt_instance.hass.states.get.return_value = mock_state

        with patch("custom_components.better_thermostat.utils.watcher.ir"):
            with patch(
                "custom_components.better_thermostat.utils.watcher.get_battery_status"
            ):
                result = await check_critical_entities(mock_bt_instance)

                assert result is True
                # Should be called for each available TRV (2 TRVs in fixture)
                assert (
                    mock_bt_instance.hass.async_create_background_task.call_count == 2
                )

    @pytest.mark.anyio
    async def test_check_critical_entities_no_battery_call_when_unavailable(
        self, mock_bt_instance
    ):
        """Test that get_battery_status is not called for unavailable TRVs."""
        from custom_components.better_thermostat.utils.watcher import (
            check_critical_entities,
        )

        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_bt_instance.hass.states.get.return_value = mock_state

        with patch("custom_components.better_thermostat.utils.watcher.ir"):
            with patch(
                "custom_components.better_thermostat.utils.watcher.get_battery_status"
            ):
                result = await check_critical_entities(mock_bt_instance)

                assert result is False
                # Should not be called for unavailable TRVs
                assert mock_bt_instance.hass.async_create_task.call_count == 0
