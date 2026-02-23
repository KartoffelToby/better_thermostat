"""Tests for Better Thermostat sensor platform (sensor.py).

Covers:
  - Sensor entity classes (state updates, availability, EMA math)
  - Setup & algorithm sensor creation
  - Active algorithm detection
  - Entity cleanup functions (preset, PID, switch)
  - Unload cleanup
"""

import math
from time import monotonic
from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.sensor import (
    BetterThermostatExternalTemp1hEMASensor,
    BetterThermostatExternalTempSensor,
    BetterThermostatHeatLossSensor,
    BetterThermostatHeatingPowerSensor,
    BetterThermostatMpcGainSensor,
    BetterThermostatMpcKaSensor,
    BetterThermostatMpcLossSensor,
    BetterThermostatSolarIntensitySensor,
    BetterThermostatTempSlopeSensor,
    BetterThermostatVirtualTempSensor,
    _ACTIVE_ALGORITHM_ENTITIES,
    _ACTIVE_PID_NUMBERS,
    _ACTIVE_PRESET_NUMBERS,
    _ACTIVE_SWITCH_ENTITIES,
    _BtMpcSensorBase,
    _BtSensorBase,
    _BtSimpleAttributeSensor,
    _DISPATCHER_UNSUBSCRIBES,
    _ENTITY_CLEANUP_CALLBACKS,
    _cleanup_pid_number_entities,
    _cleanup_pid_switch_entities,
    _cleanup_preset_number_entities,
    _cleanup_stale_algorithm_entities,
    _get_active_algorithms,
    _get_filtered_temp,
    _setup_algorithm_sensors,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION_MODE,
    CalibrationMode,
)

DOMAIN = "better_thermostat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bt_climate(**overrides):
    """Create a mock BT climate entity with sensible defaults."""
    bt = MagicMock()
    bt.unique_id = "test_bt_123"
    bt.device_name = "Test BT"
    bt.entity_id = "climate.test_bt"
    bt.device_info = {"identifiers": {(DOMAIN, "test_bt_123")}}
    bt._available = True
    bt.window_open = False
    bt.hvac_mode = "heat"
    bt.cur_temp_filtered = None
    bt.external_temp_ema = None
    bt.temp_slope = None
    bt.heating_power = None
    bt.heat_loss_rate = None
    bt.real_trvs = {}
    bt.preset_modes = []
    for k, v in overrides.items():
        setattr(bt, k, v)
    return bt


def _make_entry(entry_id="entry_1"):
    """Create a mock ConfigEntry."""
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _make_entity_registry():
    """Create a mock EntityRegistry."""
    reg = MagicMock()
    reg.async_get_entity_id = MagicMock(return_value=None)
    reg.async_remove = MagicMock()
    return reg


# ---------------------------------------------------------------------------
# Cleanup: reset module-level globals between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_globals():
    """Reset module-level tracking dicts before each test."""
    _ACTIVE_ALGORITHM_ENTITIES.clear()
    _ENTITY_CLEANUP_CALLBACKS.clear()
    _DISPATCHER_UNSUBSCRIBES.clear()
    _ACTIVE_PRESET_NUMBERS.clear()
    _ACTIVE_PID_NUMBERS.clear()
    _ACTIVE_SWITCH_ENTITIES.clear()
    yield
    _ACTIVE_ALGORITHM_ENTITIES.clear()
    _ENTITY_CLEANUP_CALLBACKS.clear()
    _DISPATCHER_UNSUBSCRIBES.clear()
    _ACTIVE_PRESET_NUMBERS.clear()
    _ACTIVE_PID_NUMBERS.clear()
    _ACTIVE_SWITCH_ENTITIES.clear()


# ===========================================================================
# 1. External Temp Sensor (EMA)
# ===========================================================================

class TestExternalTempSensor:
    """Tests for BetterThermostatExternalTempSensor."""

    def test_unique_id(self):
        bt = _make_bt_climate()
        sensor = BetterThermostatExternalTempSensor(bt)
        assert sensor._attr_unique_id == "test_bt_123_external_temp_ema"

    def test_update_from_cur_temp_filtered(self):
        bt = _make_bt_climate(cur_temp_filtered=21.5)
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 21.5

    def test_fallback_to_external_temp_ema(self):
        bt = _make_bt_climate(cur_temp_filtered=None, external_temp_ema=22.3)
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 22.3

    def test_none_when_both_missing(self):
        bt = _make_bt_climate(cur_temp_filtered=None, external_temp_ema=None)
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_float_returns_none(self):
        bt = _make_bt_climate(cur_temp_filtered="not_a_number")
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_string_number_converted(self):
        """A string like '20.5' should be converted to float."""
        bt = _make_bt_climate(cur_temp_filtered="20.5")
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 20.5


# ===========================================================================
# 2. External Temp 1h EMA Sensor
# ===========================================================================

class TestExternalTemp1hEMASensor:
    """Tests for BetterThermostatExternalTemp1hEMASensor."""

    def test_unique_id(self):
        bt = _make_bt_climate()
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        assert sensor._attr_unique_id == "test_bt_123_external_temp_ema_1h"

    def test_first_update_sets_ema_directly(self):
        bt = _make_bt_climate(cur_temp_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 20.0
        assert sensor._ema_value == 20.0

    def test_subsequent_update_applies_ema(self):
        bt = _make_bt_climate(cur_temp_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()  # first
        # Simulate time passing
        sensor._last_update_ts = monotonic() - 60  # 1 minute ago
        bt.cur_temp_filtered = 25.0
        sensor._update_state()  # second
        # EMA should be between 20 and 25, closer to 20
        assert 20.0 < sensor._attr_native_value < 25.0

    def test_ema_converges_over_time(self):
        """After many tau periods, EMA should be very close to new value."""
        bt = _make_bt_climate(cur_temp_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()
        # Simulate 5 tau (5 hours) passing in one step
        sensor._last_update_ts = monotonic() - (5 * 3600)
        bt.cur_temp_filtered = 25.0
        sensor._update_state()
        # After 5 tau, alpha ≈ 1 - e^(-5) ≈ 0.993
        assert abs(sensor._attr_native_value - 25.0) < 0.1

    def test_zero_dt_does_not_change_ema(self):
        """When dt=0, alpha=0, EMA should not change."""
        bt = _make_bt_climate(cur_temp_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()  # first → EMA = 20.0
        # Set last_update_ts to now so dt ≈ 0
        sensor._last_update_ts = monotonic()
        bt.cur_temp_filtered = 30.0
        sensor._update_state()
        # dt ≈ 0 → alpha ≈ 0 → EMA stays at 20.0
        assert sensor._attr_native_value == 20.0

    def test_none_value_gives_none(self):
        bt = _make_bt_climate(cur_temp_filtered=None, external_temp_ema=None)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_float_gives_none(self):
        bt = _make_bt_climate(cur_temp_filtered="invalid")
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_ema_math_correctness(self):
        """Verify the EMA formula matches expected math."""
        bt = _make_bt_climate(cur_temp_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_ema(20.0)  # first
        dt_s = 600.0  # 10 minutes
        sensor._last_update_ts = monotonic() - dt_s
        sensor._update_ema(25.0)
        expected_alpha = 1.0 - math.exp(-dt_s / 3600.0)
        expected_ema = 20.0 + expected_alpha * (25.0 - 20.0)
        assert abs(sensor._ema_value - expected_ema) < 0.001


# ===========================================================================
# 3. Simple attribute sensors (TempSlope, HeatingPower, HeatLoss)
# ===========================================================================

class TestSimpleAttributeSensors:
    """Tests for sensors that read a single attribute."""

    def test_temp_slope_with_value(self):
        bt = _make_bt_climate(temp_slope=0.0123)
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.0123

    def test_temp_slope_rounds_to_4_decimals(self):
        bt = _make_bt_climate(temp_slope=0.01236789)
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.0124

    def test_temp_slope_none(self):
        bt = _make_bt_climate(temp_slope=None)
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_heating_power_with_value(self):
        bt = _make_bt_climate(heating_power=0.05)
        sensor = BetterThermostatHeatingPowerSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.05

    def test_heating_power_none(self):
        bt = _make_bt_climate(heating_power=None)
        sensor = BetterThermostatHeatingPowerSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_heat_loss_with_value(self):
        bt = _make_bt_climate(heat_loss_rate=0.03)
        sensor = BetterThermostatHeatLossSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.03

    def test_heat_loss_none(self):
        bt = _make_bt_climate(heat_loss_rate=None)
        sensor = BetterThermostatHeatLossSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_string_returns_none(self):
        bt = _make_bt_climate(temp_slope="not_a_number")
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None


# ===========================================================================
# 4. MPC Sensors (VirtualTemp, Gain, Loss, Ka) – availability & state
# ===========================================================================

class TestMpcSensorAvailability:
    """Tests for the shared availability logic of MPC sensors."""

    @pytest.mark.parametrize("SensorClass", [
        BetterThermostatVirtualTempSensor,
        BetterThermostatMpcGainSensor,
        BetterThermostatMpcLossSensor,
        BetterThermostatMpcKaSensor,
    ])
    def test_available_when_all_ok(self, SensorClass):
        bt = _make_bt_climate(_available=True, window_open=False, hvac_mode="heat")
        sensor = SensorClass(bt)
        assert sensor.available is True

    @pytest.mark.parametrize("SensorClass", [
        BetterThermostatVirtualTempSensor,
        BetterThermostatMpcGainSensor,
        BetterThermostatMpcLossSensor,
        BetterThermostatMpcKaSensor,
    ])
    def test_unavailable_when_climate_unavailable(self, SensorClass):
        bt = _make_bt_climate(_available=False)
        sensor = SensorClass(bt)
        assert sensor.available is False

    @pytest.mark.parametrize("SensorClass", [
        BetterThermostatVirtualTempSensor,
        BetterThermostatMpcGainSensor,
        BetterThermostatMpcLossSensor,
        BetterThermostatMpcKaSensor,
    ])
    def test_unavailable_when_window_open(self, SensorClass):
        bt = _make_bt_climate(window_open=True)
        sensor = SensorClass(bt)
        assert sensor.available is False

    @pytest.mark.parametrize("SensorClass", [
        BetterThermostatVirtualTempSensor,
        BetterThermostatMpcGainSensor,
        BetterThermostatMpcLossSensor,
        BetterThermostatMpcKaSensor,
    ])
    def test_unavailable_when_hvac_off(self, SensorClass):
        bt = _make_bt_climate(hvac_mode="off")
        sensor = SensorClass(bt)
        assert sensor.available is False

    @pytest.mark.parametrize("SensorClass", [
        BetterThermostatVirtualTempSensor,
        BetterThermostatMpcGainSensor,
        BetterThermostatMpcLossSensor,
        BetterThermostatMpcKaSensor,
    ])
    def test_available_false_when_not_available(self, SensorClass):
        """If _available is False, sensor should be unavailable."""
        bt = _make_bt_climate()
        bt._available = False
        sensor = SensorClass(bt)
        assert sensor.available is False


class TestMpcSensorState:
    """Tests for MPC sensor state retrieval from calibration_balance debug."""

    def _make_trv_with_debug(self, **debug_values):
        return {
            "trv_1": {
                "calibration_balance": {
                    "debug": debug_values,
                },
            }
        }

    def test_virtual_temp_reads_from_debug(self):
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(mpc_virtual_temp=22.5))
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 22.5

    def test_mpc_gain_reads_from_debug(self):
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(mpc_gain=0.05))
        sensor = BetterThermostatMpcGainSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.05

    def test_mpc_loss_reads_from_debug(self):
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(mpc_loss=0.03))
        sensor = BetterThermostatMpcLossSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.03

    def test_mpc_ka_reads_from_debug(self):
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(mpc_ka=0.001))
        sensor = BetterThermostatMpcKaSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.001

    def test_no_calibration_balance_returns_none(self):
        bt = _make_bt_climate(real_trvs={"trv_1": {}})
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_no_debug_key_returns_none(self):
        bt = _make_bt_climate(real_trvs={"trv_1": {"calibration_balance": {}}})
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_real_trvs_none_returns_none(self):
        bt = _make_bt_climate(real_trvs=None)
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_empty_real_trvs_returns_none(self):
        bt = _make_bt_climate(real_trvs={})
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_debug_value_returns_none(self):
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(mpc_virtual_temp="bad"))
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_first_trv_with_debug_wins(self):
        """When multiple TRVs exist, the first with debug data should be used."""
        bt = _make_bt_climate(real_trvs={
            "trv_1": {},
            "trv_2": {"calibration_balance": {"debug": {"mpc_virtual_temp": 23.0}}},
        })
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 23.0


# ===========================================================================
# 5. Solar Intensity Sensor
# ===========================================================================

class TestSolarIntensitySensor:
    """Tests for BetterThermostatSolarIntensitySensor."""

    def test_unique_id(self):
        bt = _make_bt_climate()
        sensor = BetterThermostatSolarIntensitySensor(bt)
        assert sensor._attr_unique_id == "test_bt_123_solar_intensity"

    @patch("custom_components.better_thermostat.sensor._get_current_solar_intensity")
    def test_normal_value_converted_to_percent(self, mock_solar):
        mock_solar.return_value = 0.75
        bt = _make_bt_climate()
        sensor = BetterThermostatSolarIntensitySensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 75.0

    @patch("custom_components.better_thermostat.sensor._get_current_solar_intensity")
    def test_zero_intensity(self, mock_solar):
        mock_solar.return_value = 0.0
        bt = _make_bt_climate()
        sensor = BetterThermostatSolarIntensitySensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.0

    @patch("custom_components.better_thermostat.sensor._get_current_solar_intensity")
    def test_none_returns_zero(self, mock_solar):
        """When _get_current_solar_intensity returns None, sensor shows 0.0."""
        mock_solar.return_value = None
        bt = _make_bt_climate()
        sensor = BetterThermostatSolarIntensitySensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.0

    @patch("custom_components.better_thermostat.sensor._get_current_solar_intensity")
    def test_exception_returns_none(self, mock_solar):
        mock_solar.side_effect = RuntimeError("weather unavailable")
        bt = _make_bt_climate()
        sensor = BetterThermostatSolarIntensitySensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    @patch("custom_components.better_thermostat.sensor._get_current_solar_intensity")
    def test_full_intensity_gives_100_percent(self, mock_solar):
        mock_solar.return_value = 1.0
        bt = _make_bt_climate()
        sensor = BetterThermostatSolarIntensitySensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 100.0


# ===========================================================================
# 6. _get_active_algorithms
# ===========================================================================

class TestGetActiveAlgorithms:
    """Tests for _get_active_algorithms helper."""

    def test_no_real_trvs_returns_empty(self):
        bt = _make_bt_climate(real_trvs={})
        assert _get_active_algorithms(bt) == set()

    def test_real_trvs_none_returns_empty(self):
        bt = _make_bt_climate(real_trvs=None)
        assert _get_active_algorithms(bt) == set()

    def test_mpc_calibration_detected(self):
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION}},
        })
        result = _get_active_algorithms(bt)
        assert CalibrationMode.MPC_CALIBRATION in result

    def test_string_calibration_mode_converted(self):
        """String values should be auto-converted to CalibrationMode enum."""
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: "mpc_calibration"}},
        })
        result = _get_active_algorithms(bt)
        assert CalibrationMode.MPC_CALIBRATION in result

    def test_invalid_calibration_mode_skipped(self):
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: "totally_invalid_mode"}},
        })
        result = _get_active_algorithms(bt)
        assert result == set()

    def test_multiple_trvs_different_modes(self):
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION}},
            "trv_2": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION}},
        })
        result = _get_active_algorithms(bt)
        assert result == {CalibrationMode.MPC_CALIBRATION, CalibrationMode.PID_CALIBRATION}

    def test_none_calibration_mode_skipped(self):
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: None}},
        })
        result = _get_active_algorithms(bt)
        assert result == set()

    def test_missing_advanced_key_skipped(self):
        bt = _make_bt_climate(real_trvs={
            "trv_1": {},
        })
        result = _get_active_algorithms(bt)
        assert result == set()

    def test_real_trvs_none_returns_empty(self):
        """real_trvs=None should be handled as empty."""
        bt = _make_bt_climate(real_trvs=None)
        result = _get_active_algorithms(bt)
        assert result == set()


# ===========================================================================
# 7. _setup_algorithm_sensors
# ===========================================================================

class TestSetupAlgorithmSensors:
    """Tests for _setup_algorithm_sensors."""

    @pytest.mark.asyncio
    async def test_mpc_creates_four_sensors(self):
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry_1": {"climate": None}}}
        entry = _make_entry()
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION}},
        })
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            sensors = await _setup_algorithm_sensors(hass, entry, bt)
        assert len(sensors) == 4
        assert isinstance(sensors[0], BetterThermostatVirtualTempSensor)

    @pytest.mark.asyncio
    async def test_no_algorithms_returns_empty(self):
        hass = MagicMock()
        entry = _make_entry()
        bt = _make_bt_climate(real_trvs={})
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            sensors = await _setup_algorithm_sensors(hass, entry, bt)
        assert sensors == []

    @pytest.mark.asyncio
    async def test_algorithms_to_create_filters(self):
        """When algorithms_to_create is provided, only those algorithms create sensors."""
        hass = MagicMock()
        entry = _make_entry()
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION}},
        })
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            # Request only PID sensors → MPC should be excluded
            sensors = await _setup_algorithm_sensors(
                hass, entry, bt, algorithms_to_create={CalibrationMode.PID_CALIBRATION}
            )
        assert sensors == []

    @pytest.mark.asyncio
    async def test_mpc_tracking_registered(self):
        hass = MagicMock()
        entry = _make_entry()
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION}},
        })
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            await _setup_algorithm_sensors(hass, entry, bt)
        assert "entry_1" in _ACTIVE_ALGORITHM_ENTITIES
        assert CalibrationMode.MPC_CALIBRATION in _ACTIVE_ALGORITHM_ENTITIES["entry_1"]
        tracked_ids = _ACTIVE_ALGORITHM_ENTITIES["entry_1"][CalibrationMode.MPC_CALIBRATION]
        assert len(tracked_ids) == 5  # 4 sensors + mpc_status


# ===========================================================================
# 8. async_setup_entry
# ===========================================================================

class TestAsyncSetupEntry:
    """Tests for async_setup_entry."""

    @pytest.mark.asyncio
    async def test_no_climate_returns_early(self):
        """If climate entity not found, no sensors should be added."""
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry_1": {"climate": None}}}
        entry = _make_entry()
        async_add_entities = MagicMock()

        await async_setup_entry(hass, entry, async_add_entities)
        async_add_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_six_core_sensors(self):
        """Should create 6 core sensors when climate exists."""
        bt = _make_bt_climate()
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry_1": {"climate": bt}}}
        entry = _make_entry()
        async_add_entities = MagicMock()

        with patch(
            "custom_components.better_thermostat.sensor._setup_algorithm_sensors",
            return_value=[],
        ), patch(
            "custom_components.better_thermostat.sensor._register_dynamic_entity_callback",
        ):
            await async_setup_entry(hass, entry, async_add_entities)

        async_add_entities.assert_called_once()
        sensors = async_add_entities.call_args[0][0]
        assert len(sensors) == 6


# ===========================================================================
# 9. async_unload_entry
# ===========================================================================

class TestAsyncUnloadEntry:
    """Tests for async_unload_entry."""

    @pytest.mark.asyncio
    async def test_unsubscribes_dispatcher(self):
        entry = _make_entry()
        unsub = MagicMock()
        _DISPATCHER_UNSUBSCRIBES["entry_1"] = unsub
        hass = MagicMock()

        result = await async_unload_entry(hass, entry)
        assert result is True
        unsub.assert_called_once()
        assert "entry_1" not in _DISPATCHER_UNSUBSCRIBES

    @pytest.mark.asyncio
    async def test_cleans_all_tracking_dicts(self):
        entry = _make_entry()
        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {"algo": ["id1"]}
        _ENTITY_CLEANUP_CALLBACKS["entry_1"] = MagicMock()
        _ACTIVE_PRESET_NUMBERS["entry_1"] = {"uid": {}}
        _ACTIVE_PID_NUMBERS["entry_1"] = {"uid": {}}
        _ACTIVE_SWITCH_ENTITIES["entry_1"] = {"uid": {}}
        hass = MagicMock()

        await async_unload_entry(hass, entry)

        assert "entry_1" not in _ACTIVE_ALGORITHM_ENTITIES
        assert "entry_1" not in _ENTITY_CLEANUP_CALLBACKS
        assert "entry_1" not in _ACTIVE_PRESET_NUMBERS
        assert "entry_1" not in _ACTIVE_PID_NUMBERS
        assert "entry_1" not in _ACTIVE_SWITCH_ENTITIES

    @pytest.mark.asyncio
    async def test_no_dispatcher_no_error(self):
        """Unloading an entry without registered dispatcher should not fail."""
        entry = _make_entry()
        hass = MagicMock()
        result = await async_unload_entry(hass, entry)
        assert result is True


# ===========================================================================
# 10. _cleanup_stale_algorithm_entities
# ===========================================================================

class TestCleanupStaleAlgorithmEntities:
    """Tests for _cleanup_stale_algorithm_entities."""

    @pytest.mark.asyncio
    async def test_no_tracked_returns_early(self):
        """If entry not tracked, should return without error."""
        hass = MagicMock()
        bt = _make_bt_climate()
        await _cleanup_stale_algorithm_entities(hass, "entry_1", bt, set())
        # No exception → pass

    @pytest.mark.asyncio
    async def test_removes_stale_entities(self):
        """Entities for algorithms no longer active should be removed."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "sensor.mpc_virtual_temp"

        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {
            CalibrationMode.MPC_CALIBRATION: ["uid_1", "uid_2"],
        }

        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=reg,
        ):
            bt = _make_bt_climate()
            # current_algorithms is empty → MPC should be cleaned up
            await _cleanup_stale_algorithm_entities(hass=MagicMock(), entry_id="entry_1", bt_climate=bt, current_algorithms=set())

        assert reg.async_remove.call_count == 2
        # Tracking should be cleaned up
        assert "entry_1" not in _ACTIVE_ALGORITHM_ENTITIES

    @pytest.mark.asyncio
    async def test_keeps_active_algorithms(self):
        """Entities for still-active algorithms should NOT be removed."""
        reg = _make_entity_registry()
        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {
            CalibrationMode.MPC_CALIBRATION: ["uid_1"],
        }

        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=reg,
        ):
            bt = _make_bt_climate()
            await _cleanup_stale_algorithm_entities(
                hass=MagicMock(),
                entry_id="entry_1",
                bt_climate=bt,
                current_algorithms={CalibrationMode.MPC_CALIBRATION},
            )

        reg.async_remove.assert_not_called()
        # Tracking should still exist
        assert "entry_1" in _ACTIVE_ALGORITHM_ENTITIES

    @pytest.mark.asyncio
    async def test_partial_removal_keeps_tracking(self):
        """If not all entities could be removed, tracking should remain."""
        reg = _make_entity_registry()
        # First entity found, second not found
        reg.async_get_entity_id.side_effect = ["sensor.found", None]

        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {
            CalibrationMode.MPC_CALIBRATION: ["uid_1", "uid_2"],
        }

        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=reg,
        ):
            bt = _make_bt_climate()
            await _cleanup_stale_algorithm_entities(
                hass=MagicMock(), entry_id="entry_1", bt_climate=bt, current_algorithms=set()
            )

        # Only 1 entity removed (other not found in registry)
        assert reg.async_remove.call_count == 1
        # Since only 1 of 2 removed, algorithm tracking should remain
        assert CalibrationMode.MPC_CALIBRATION in _ACTIVE_ALGORITHM_ENTITIES.get("entry_1", {})

    @pytest.mark.asyncio
    async def test_remove_exception_handled_gracefully(self):
        """If async_remove raises, it should be caught and logged."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "sensor.entity"
        reg.async_remove.side_effect = RuntimeError("registry error")

        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {
            CalibrationMode.MPC_CALIBRATION: ["uid_1"],
        }

        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=reg,
        ):
            bt = _make_bt_climate()
            # Should not raise
            await _cleanup_stale_algorithm_entities(
                hass=MagicMock(), entry_id="entry_1", bt_climate=bt, current_algorithms=set()
            )


# ===========================================================================
# 11. _cleanup_preset_number_entities
# ===========================================================================

class TestCleanupPresetNumberEntities:
    """Tests for _cleanup_preset_number_entities."""

    @pytest.mark.asyncio
    async def test_removes_disabled_preset_entities(self):
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "number.preset_away"

        _ACTIVE_PRESET_NUMBERS["entry_1"] = {
            "uid_away": {"preset": "away"},
            "uid_home": {"preset": "home"},
        }
        bt = _make_bt_climate()
        # Only "home" is current → "away" should be removed
        await _cleanup_preset_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1",
            bt_climate=bt, current_presets={"home"},
        )

        reg.async_remove.assert_called_once_with("number.preset_away")

    @pytest.mark.asyncio
    async def test_none_unique_id_skipped(self):
        """Entries with None as unique_id should be skipped."""
        reg = _make_entity_registry()
        _ACTIVE_PRESET_NUMBERS["entry_1"] = {
            None: {"preset": "away"},
        }
        bt = _make_bt_climate()
        await _cleanup_preset_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1",
            bt_climate=bt, current_presets=set(),
        )
        reg.async_get_entity_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_merges_new_presets_into_tracking(self):
        reg = _make_entity_registry()
        bt = _make_bt_climate()
        await _cleanup_preset_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1",
            bt_climate=bt, current_presets={"comfort", "eco"},
        )
        tracked = _ACTIVE_PRESET_NUMBERS["entry_1"]
        assert f"{bt.unique_id}_preset_comfort" in tracked
        assert f"{bt.unique_id}_preset_eco" in tracked

    @pytest.mark.asyncio
    async def test_remove_failure_keeps_tracking(self):
        """On removal failure, tracking entry should remain for retry."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "number.preset_away"
        reg.async_remove.side_effect = RuntimeError("fail")

        _ACTIVE_PRESET_NUMBERS["entry_1"] = {
            "uid_away": {"preset": "away"},
        }
        bt = _make_bt_climate()
        await _cleanup_preset_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1",
            bt_climate=bt, current_presets=set(),
        )
        # Tracking should remain since removal failed
        assert "uid_away" in _ACTIVE_PRESET_NUMBERS["entry_1"]


# ===========================================================================
# 12. _cleanup_pid_number_entities
# ===========================================================================

class TestCleanupPidNumberEntities:
    """Tests for _cleanup_pid_number_entities."""

    @pytest.mark.asyncio
    async def test_removes_pid_entities_for_non_pid_trv(self):
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "number.pid_kp"

        _ACTIVE_PID_NUMBERS["entry_1"] = {
            "uid_kp": {"trv": "trv_1", "param": "kp"},
        }
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT}},
        })
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        reg.async_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_keeps_pid_entities_for_pid_trv(self):
        reg = _make_entity_registry()
        _ACTIVE_PID_NUMBERS["entry_1"] = {
            "uid_kp": {"trv": "trv_1", "param": "kp"},
        }
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION}},
        })
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        reg.async_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_merges_pid_tracking_for_current_trvs(self):
        reg = _make_entity_registry()
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION}},
        })
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        tracked = _ACTIVE_PID_NUMBERS["entry_1"]
        # Should have 3 entries for trv_1 (kp, ki, kd)
        trv_entries = [v for v in tracked.values() if v.get("trv") == "trv_1"]
        assert len(trv_entries) == 3

    @pytest.mark.asyncio
    async def test_no_real_trvs_returns_empty_pid_trvs(self):
        """If no real_trvs, no PID TRVs should be found."""
        reg = _make_entity_registry()
        _ACTIVE_PID_NUMBERS["entry_1"] = {
            "uid_kp": {"trv": "trv_1", "param": "kp"},
        }
        bt = _make_bt_climate(real_trvs={})
        reg.async_get_entity_id.return_value = "number.pid_kp"
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        reg.async_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_calibration_mode_trv_skipped(self):
        reg = _make_entity_registry()
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: "totally_bogus"}},
        })
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        # No PID TRVs found, nothing to merge
        tracked = _ACTIVE_PID_NUMBERS["entry_1"]
        assert not any(v.get("trv") == "trv_1" for v in tracked.values())


# ===========================================================================
# 13. _cleanup_pid_switch_entities
# ===========================================================================

class TestCleanupPidSwitchEntities:
    """Tests for _cleanup_pid_switch_entities."""

    @pytest.mark.asyncio
    async def test_removes_pid_autotune_for_non_pid_trv(self):
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "switch.pid_auto_tune"

        _ACTIVE_SWITCH_ENTITIES["entry_1"] = {
            "uid_autotune": {"trv": "trv_1", "type": "pid_auto_tune"},
        }
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT}},
        })
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        reg.async_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_removes_child_lock_for_removed_trv(self):
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "switch.child_lock"

        _ACTIVE_SWITCH_ENTITIES["entry_1"] = {
            "uid_lock": {"trv": "trv_removed", "type": "child_lock"},
        }
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT}},
        })
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        reg.async_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_keeps_child_lock_for_existing_trv(self):
        reg = _make_entity_registry()
        _ACTIVE_SWITCH_ENTITIES["entry_1"] = {
            "uid_lock": {"trv": "trv_1", "type": "child_lock"},
        }
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT}},
        })
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        reg.async_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_merges_switch_tracking(self):
        reg = _make_entity_registry()
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION}},
        })
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        tracked = _ACTIVE_SWITCH_ENTITIES["entry_1"]
        # Should have pid_auto_tune + child_lock for trv_1
        types = {v["type"] for v in tracked.values() if v.get("trv") == "trv_1"}
        assert types == {"pid_auto_tune", "child_lock"}

    @pytest.mark.asyncio
    async def test_child_lock_for_no_real_trvs(self):
        """When real_trvs missing, child lock for tracked TRV should be removed."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "switch.child_lock"

        _ACTIVE_SWITCH_ENTITIES["entry_1"] = {
            "uid_lock": {"trv": "trv_1", "type": "child_lock"},
        }
        bt = _make_bt_climate(real_trvs=None)
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt,
        )
        reg.async_remove.assert_called_once()


# ===========================================================================
# 14. Edge cases & potential bugs
# ===========================================================================

class TestEdgeCasesAndPotentialBugs:
    """Tests probing edge cases that might reveal bugs."""

    def test_mpc_sensor_real_trvs_none_does_not_crash(self):
        """If real_trvs is None instead of dict, _update_state should not crash."""
        bt = _make_bt_climate()
        bt.real_trvs = None
        sensor = BetterThermostatVirtualTempSensor(bt)
        # This might crash with TypeError: cannot unpack non-iterable NoneType
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_mpc_sensor_real_trvs_is_list_not_dict(self):
        """If real_trvs is a list instead of dict, .items() would fail."""
        bt = _make_bt_climate()
        bt.real_trvs = [{"calibration_balance": {"debug": {"mpc_virtual_temp": 22.0}}}]
        sensor = BetterThermostatVirtualTempSensor(bt)
        # list doesn't have .items() → should it crash?
        try:
            sensor._update_state()
        except AttributeError:
            pass  # documents the bug

    def test_solar_sensor_negative_intensity(self):
        """What happens if solar intensity returns a negative value?"""
        with patch(
            "custom_components.better_thermostat.sensor._get_current_solar_intensity"
        ) as mock_solar:
            mock_solar.return_value = -0.5
            bt = _make_bt_climate()
            sensor = BetterThermostatSolarIntensitySensor(bt)
            sensor._update_state()
            # Code does val * 100.0 → would show -50.0%
            # This might be unexpected behavior
            assert sensor._attr_native_value == -50.0

    def test_solar_sensor_above_one_intensity(self):
        """What happens if solar intensity returns > 1.0?"""
        with patch(
            "custom_components.better_thermostat.sensor._get_current_solar_intensity"
        ) as mock_solar:
            mock_solar.return_value = 1.5
            bt = _make_bt_climate()
            sensor = BetterThermostatSolarIntensitySensor(bt)
            sensor._update_state()
            assert sensor._attr_native_value == 150.0

    def test_1h_ema_negative_dt_clamped(self):
        """If monotonic() goes backward (shouldn't happen but defensive), dt is clamped to 0."""
        bt = _make_bt_climate(cur_temp_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_ema(20.0)
        # Set last_update to the future
        sensor._last_update_ts = monotonic() + 1000
        sensor._update_ema(25.0)
        # dt = max(0, now - future) = 0 → alpha = 0 → EMA stays at 20
        assert sensor._ema_value == 20.0

    def test_cleanup_stale_empty_entry_removed(self):
        """After all algorithms removed, the entry_id key should be deleted."""
        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {}
        # empty dict → should be cleaned up
        reg = _make_entity_registry()
        hass = MagicMock()
        bt = _make_bt_climate()
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            _cleanup_stale_algorithm_entities(hass, "entry_1", bt, set())
        )
        # The function checks `if not _ACTIVE_ALGORITHM_ENTITIES[entry_id]`
        # and deletes it → entry should be gone
        assert "entry_1" not in _ACTIVE_ALGORITHM_ENTITIES

    @pytest.mark.asyncio
    async def test_cleanup_preset_entities_with_empty_tracking(self):
        """Cleaning up when there are no tracked presets should not fail."""
        reg = _make_entity_registry()
        bt = _make_bt_climate()
        await _cleanup_preset_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1",
            bt_climate=bt, current_presets={"comfort"},
        )
        # Should just merge without error
        assert "entry_1" in _ACTIVE_PRESET_NUMBERS

    @pytest.mark.asyncio
    async def test_get_active_algorithms_with_empty_advanced(self):
        """TRV with empty advanced dict should return no algorithms."""
        bt = _make_bt_climate(real_trvs={
            "trv_1": {"advanced": {}},
        })
        result = _get_active_algorithms(bt)
        assert result == set()

    def test_external_temp_sensor_with_nan(self):
        """NaN as temperature value should be handled."""
        bt = _make_bt_climate(cur_temp_filtered=float("nan"))
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        # NaN is a valid float, so it will be set (but it's arguably a bug)
        assert sensor._attr_native_value is not None  # float("nan") is a float
        assert math.isnan(sensor._attr_native_value)

    def test_external_temp_sensor_with_inf(self):
        """Infinity as temperature should be handled."""
        bt = _make_bt_climate(cur_temp_filtered=float("inf"))
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        # inf is a valid float → will be set (potentially problematic)
        assert sensor._attr_native_value == float("inf")

    def test_1h_ema_with_nan_input(self):
        """NaN input to EMA should propagate NaN."""
        bt = _make_bt_climate(cur_temp_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_ema(20.0)
        sensor._last_update_ts = monotonic() - 60
        sensor._update_ema(float("nan"))
        # NaN math: 20 + alpha * (nan - 20) = nan
        assert math.isnan(sensor._ema_value)

    @pytest.mark.asyncio
    async def test_setup_entry_missing_domain_key_crashes(self):
        """If hass.data doesn't have the DOMAIN key, it should crash with KeyError."""
        hass = MagicMock()
        hass.data = {}  # no DOMAIN key
        entry = _make_entry()
        async_add_entities = MagicMock()

        with pytest.raises(KeyError):
            await async_setup_entry(hass, entry, async_add_entities)

    @pytest.mark.asyncio
    async def test_setup_entry_missing_entry_id_crashes(self):
        """If the entry_id is not in hass.data[DOMAIN], KeyError should occur."""
        hass = MagicMock()
        hass.data = {DOMAIN: {}}  # no entry_id
        entry = _make_entry()
        async_add_entities = MagicMock()

        with pytest.raises(KeyError):
            await async_setup_entry(hass, entry, async_add_entities)


# ===========================================================================
# 15. Base class tests
# ===========================================================================

class TestBtSensorBase:
    """Tests for _BtSensorBase.__init__ and shared behavior."""

    def test_init_sets_unique_id_from_suffix(self):
        bt = _make_bt_climate()
        sensor = BetterThermostatTempSlopeSensor(bt)
        assert sensor._attr_unique_id == "test_bt_123_temp_slope"

    def test_init_stores_bt_climate(self):
        bt = _make_bt_climate()
        sensor = BetterThermostatHeatingPowerSensor(bt)
        assert sensor._bt_climate is bt

    def test_init_sets_device_info(self):
        bt = _make_bt_climate()
        sensor = BetterThermostatHeatLossSensor(bt)
        assert sensor._attr_device_info == bt.device_info

    def test_all_sensors_inherit_from_base(self):
        """All concrete sensor classes should inherit from _BtSensorBase."""
        bt = _make_bt_climate()
        for cls in [
            BetterThermostatExternalTempSensor,
            BetterThermostatExternalTemp1hEMASensor,
            BetterThermostatTempSlopeSensor,
            BetterThermostatHeatingPowerSensor,
            BetterThermostatHeatLossSensor,
            BetterThermostatVirtualTempSensor,
            BetterThermostatMpcGainSensor,
            BetterThermostatMpcLossSensor,
            BetterThermostatMpcKaSensor,
            BetterThermostatSolarIntensitySensor,
        ]:
            sensor = cls(bt)
            assert isinstance(sensor, _BtSensorBase), f"{cls.__name__} should inherit from _BtSensorBase"

    def test_mpc_sensors_inherit_from_mpc_base(self):
        """All MPC sensors should inherit from _BtMpcSensorBase."""
        bt = _make_bt_climate()
        for cls in [
            BetterThermostatVirtualTempSensor,
            BetterThermostatMpcGainSensor,
            BetterThermostatMpcLossSensor,
            BetterThermostatMpcKaSensor,
        ]:
            sensor = cls(bt)
            assert isinstance(sensor, _BtMpcSensorBase), f"{cls.__name__} should inherit from _BtMpcSensorBase"

    def test_simple_sensors_inherit_from_simple_base(self):
        """Simple attribute sensors should inherit from _BtSimpleAttributeSensor."""
        bt = _make_bt_climate()
        for cls in [
            BetterThermostatTempSlopeSensor,
            BetterThermostatHeatingPowerSensor,
            BetterThermostatHeatLossSensor,
        ]:
            sensor = cls(bt)
            assert isinstance(sensor, _BtSimpleAttributeSensor), f"{cls.__name__} should inherit from _BtSimpleAttributeSensor"


class TestGetFilteredTemp:
    """Tests for _get_filtered_temp helper."""

    def test_prefers_cur_temp_filtered(self):
        bt = _make_bt_climate(cur_temp_filtered=21.5, external_temp_ema=22.0)
        assert _get_filtered_temp(bt) == 21.5

    def test_falls_back_to_external_temp_ema(self):
        bt = _make_bt_climate(cur_temp_filtered=None, external_temp_ema=22.0)
        assert _get_filtered_temp(bt) == 22.0

    def test_returns_none_when_both_missing(self):
        bt = _make_bt_climate(cur_temp_filtered=None, external_temp_ema=None)
        assert _get_filtered_temp(bt) is None

    def test_zero_value_not_treated_as_none(self):
        bt = _make_bt_climate(cur_temp_filtered=0.0, external_temp_ema=22.0)
        assert _get_filtered_temp(bt) == 0.0


class TestBtSimpleAttributeSensor:
    """Tests for _BtSimpleAttributeSensor base behavior."""

    def test_rounding_applied_when_set(self):
        bt = _make_bt_climate(temp_slope=0.01236789)
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.0124

    def test_no_rounding_when_none(self):
        bt = _make_bt_climate(heating_power=0.05123456)
        sensor = BetterThermostatHeatingPowerSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.05123456

    def test_none_attribute_gives_none(self):
        bt = _make_bt_climate(heating_power=None)
        sensor = BetterThermostatHeatingPowerSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_string_gives_none(self):
        bt = _make_bt_climate(temp_slope="not_a_number")
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None
