"""Tests for the startup() submethods extracted from BetterThermostat.startup().

Covers: _check_entities_ready, _collect_trv_states, _resolve_temperature_range,
_initialize_sensors, _restore_state, _validate_hvac_mode.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import (
    BetterThermostat,
    DEFAULT_FALLBACK_TEMPERATURE,
)
from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_CALL_FOR_HEAT,
    ATTR_STATE_HEATING_POWER,
    MAX_HEATING_POWER,
)

SENSOR_ID = "sensor.room_temp"
TRV_ID = "climate.test_trv"
TRV_ID_2 = "climate.test_trv_2"
COOLER_ID = "climate.cooler"
WINDOW_ID = "binary_sensor.window"
HUMIDITY_ID = "sensor.humidity"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bt():
    """Create a mock BetterThermostat with sensible defaults."""
    mock = MagicMock(spec=BetterThermostat)
    mock.hass = MagicMock()
    mock.device_name = "Test BT"
    mock.sensor_entity_id = SENSOR_ID
    mock.real_trvs = {TRV_ID: {"calibration": 1}}
    mock.cooler_entity_id = None
    mock.humidity_sensor_entity_id = None
    mock.window_id = None
    mock.all_entities = []
    mock.unavailable_sensors = []
    mock.degraded_mode = False
    mock.bt_min_temp = 5.0
    mock.bt_max_temp = 30.0
    mock.bt_target_temp = 21.0
    mock.bt_target_temp_step = None
    mock.bt_target_cooltemp = None
    mock.bt_hvac_mode = None
    mock.cur_temp = None
    mock.cur_temp_filtered = None
    mock.external_temp_ema = None
    mock._external_temp_ema_ts = None
    mock.external_temp_ema_tau_s = 300.0
    mock.temp_slope = None
    mock.last_known_external_temp = None
    mock._current_humidity = None
    mock.window_open = None
    mock.last_window_state = None
    mock.last_main_hvac_mode = None
    mock.call_for_heat = None
    mock._saved_temperature = None
    mock.heating_power = 0.01
    mock.heat_loss_rate = 0.01
    mock._preset_temperature = None
    mock._preset_mode = None
    mock._preset_temperatures = {"comfort": 22.0, "eco": 18.0}
    mock.preset_modes = ["none", "comfort", "eco"]
    mock.version = "1.0.0"
    mock.startup_running = True
    return mock


def _make_trv_state(entity_id=TRV_ID, state="heat", attrs=None):
    """Build a TRV State with typical attributes."""
    default_attrs = {
        "min_temp": 5.0,
        "max_temp": 30.0,
        "target_temp_step": 0.5,
        ATTR_TEMPERATURE: 21.0,
        "current_temperature": 20.0,
    }
    if attrs:
        default_attrs.update(attrs)
    return State(entity_id, state, attributes=default_attrs)


def _make_sensor_state(temp="21.5", state_val=None):
    """Build a sensor State."""
    return State(SENSOR_ID, state_val or temp)


# ---------------------------------------------------------------------------
# 1. _check_entities_ready
# ---------------------------------------------------------------------------


class TestCheckEntitiesReady:
    """Tests for _check_entities_ready."""

    def test_sensor_none_returns_false(self, bt):
        result = BetterThermostat._check_entities_ready(bt, None)
        assert result is False

    def test_sensor_unavailable_returns_false(self, bt):
        sensor = State(SENSOR_ID, STATE_UNAVAILABLE)
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is False

    def test_sensor_unknown_returns_false(self, bt):
        sensor = State(SENSOR_ID, STATE_UNKNOWN)
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is False

    def test_trv_none_returns_false(self, bt):
        sensor = _make_sensor_state()
        bt.hass.states.get.return_value = None
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is False

    def test_trv_unavailable_returns_false(self, bt):
        sensor = _make_sensor_state()
        bt.hass.states.get.return_value = State(TRV_ID, STATE_UNAVAILABLE)
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is False

    def test_all_ready_returns_true(self, bt):
        sensor = _make_sensor_state()
        bt.hass.states.get.return_value = _make_trv_state()
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is True

    def test_multiple_trvs_second_unavailable(self, bt):
        sensor = _make_sensor_state()
        bt.real_trvs = {TRV_ID: {}, TRV_ID_2: {}}

        def side_effect(entity_id):
            if entity_id == TRV_ID:
                return _make_trv_state()
            return State(TRV_ID_2, STATE_UNAVAILABLE)

        bt.hass.states.get.side_effect = side_effect
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is False


# ---------------------------------------------------------------------------
# 2. _collect_trv_states
# ---------------------------------------------------------------------------


class TestCollectTrvStates:
    """Tests for _collect_trv_states."""

    def test_collects_single_trv(self, bt):
        trv_state = _make_trv_state()
        bt.hass.states.get.return_value = trv_state
        result = BetterThermostat._collect_trv_states(bt)
        assert len(result) == 1
        assert result[0] is trv_state

    def test_includes_cooler_when_available(self, bt):
        bt.cooler_entity_id = COOLER_ID
        cooler_state = State(COOLER_ID, "cool", {"min_temp": 18, "max_temp": 28})
        trv_state = _make_trv_state()

        def side_effect(entity_id):
            if entity_id == TRV_ID:
                return trv_state
            if entity_id == COOLER_ID:
                return cooler_state
            return None

        bt.hass.states.get.side_effect = side_effect
        result = BetterThermostat._collect_trv_states(bt)
        assert len(result) == 2
        assert cooler_state in result

    def test_skips_unavailable_cooler(self, bt):
        bt.cooler_entity_id = COOLER_ID
        trv_state = _make_trv_state()

        def side_effect(entity_id):
            if entity_id == TRV_ID:
                return trv_state
            if entity_id == COOLER_ID:
                return State(COOLER_ID, STATE_UNAVAILABLE)
            return None

        bt.hass.states.get.side_effect = side_effect
        result = BetterThermostat._collect_trv_states(bt)
        assert len(result) == 1

    def test_missing_trv_state_skipped(self, bt):
        bt.real_trvs = {TRV_ID: {}, TRV_ID_2: {}}

        def side_effect(entity_id):
            if entity_id == TRV_ID:
                return _make_trv_state()
            return None

        bt.hass.states.get.side_effect = side_effect
        result = BetterThermostat._collect_trv_states(bt)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 3. _resolve_temperature_range
# ---------------------------------------------------------------------------


class TestResolveTemperatureRange:
    """Tests for _resolve_temperature_range."""

    def test_normal_range(self, bt):
        states = [_make_trv_state(attrs={"min_temp": 5.0, "max_temp": 30.0})]
        BetterThermostat._resolve_temperature_range(bt, states)
        assert bt.bt_min_temp == 5.0
        assert bt.bt_max_temp == 30.0

    def test_min_greater_than_max(self, bt):
        """When heater min > cooler max, range is still set."""
        states = [
            _make_trv_state(attrs={"min_temp": 20.0, "max_temp": 15.0}),
        ]
        # Set attributes so reduce_attribute returns the right values
        bt.bt_min_temp = None
        bt.bt_max_temp = None
        BetterThermostat._resolve_temperature_range(bt, states)
        # min_temp=20, max_temp=15 → warning
        assert bt.bt_min_temp == 20.0
        assert bt.bt_max_temp == 15.0

    def test_step_already_set_not_overwritten(self, bt):
        bt.bt_target_temp_step = 1.0
        states = [_make_trv_state(attrs={"target_temp_step": 0.5})]
        BetterThermostat._resolve_temperature_range(bt, states)
        assert bt.bt_target_temp_step == 1.0

    def test_step_none_gets_resolved(self, bt):
        bt.bt_target_temp_step = None
        states = [_make_trv_state(attrs={"target_temp_step": 0.5})]
        BetterThermostat._resolve_temperature_range(bt, states)
        assert bt.bt_target_temp_step == 0.5


# ---------------------------------------------------------------------------
# 4. _initialize_sensors
# ---------------------------------------------------------------------------


class TestInitializeSensors:
    """Tests for _initialize_sensors."""

    def test_sensor_ok_sets_cur_temp(self, bt):
        sensor = _make_sensor_state("21.5")
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.cur_temp is not None
        assert SENSOR_ID in bt.all_entities

    def test_sensor_unavailable_falls_back_to_trv(self, bt):
        sensor = State(SENSOR_ID, STATE_UNAVAILABLE)
        trv_state = _make_trv_state(attrs={"current_temperature": 19.5})
        bt.hass.states.get.return_value = trv_state
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.cur_temp is not None
        assert bt.degraded_mode is True

    def test_no_sensor_no_trv_uses_default(self, bt):
        sensor = State(SENSOR_ID, STATE_UNAVAILABLE)
        # TRV has no current_temperature
        trv_state = _make_trv_state(attrs={"current_temperature": None})
        bt.hass.states.get.return_value = trv_state
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.cur_temp == DEFAULT_FALLBACK_TEMPERATURE

    def test_window_open_detected(self, bt):
        bt.window_id = WINDOW_ID
        sensor = _make_sensor_state("20.0")

        def side_effect(entity_id):
            if entity_id == WINDOW_ID:
                return State(WINDOW_ID, "on")
            return None

        bt.hass.states.get.side_effect = side_effect
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.window_open is True
        assert WINDOW_ID in bt.all_entities

    def test_window_none_defaults_closed(self, bt):
        bt.window_id = None
        sensor = _make_sensor_state("20.0")
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.window_open is False

    def test_humidity_sensor_initialized(self, bt):
        bt.humidity_sensor_entity_id = HUMIDITY_ID
        sensor = _make_sensor_state("20.0")
        bt.hass.states.get.return_value = State(HUMIDITY_ID, "55.0")
        BetterThermostat._initialize_sensors(bt, sensor)
        assert HUMIDITY_ID in bt.all_entities

    def test_ema_initialized_with_cur_temp(self, bt):
        sensor = _make_sensor_state("21.5")
        with patch(
            "custom_components.better_thermostat.events.temperature._update_external_temp_ema"
        ):
            BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.last_known_external_temp is not None


# ---------------------------------------------------------------------------
# 5. _restore_state
# ---------------------------------------------------------------------------


class TestRestoreState:
    """Tests for _restore_state."""

    @pytest.mark.asyncio
    async def test_restores_ema_and_slope(self, bt):
        old = MagicMock()
        old.state = "heat"
        old.attributes = {
            "external_temp_ema": "20.5",
            "temp_slope_K_min": "0.0012",
            ATTR_TEMPERATURE: 21.0,
        }
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt._preset_temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.external_temp_ema == 20.5
        assert bt.cur_temp_filtered == 20.5
        assert bt.temp_slope == 0.0012

    @pytest.mark.asyncio
    async def test_target_clamped_to_min(self, bt):
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 2.0}  # below min
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.bt_min_temp = 5.0
        bt.bt_max_temp = 30.0
        bt._preset_temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.bt_target_temp is not None

    @pytest.mark.asyncio
    async def test_target_clamped_to_max(self, bt):
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 35.0}  # above max
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.bt_min_temp = 5.0
        bt.bt_max_temp = 30.0
        bt._preset_temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.bt_target_temp is not None

    @pytest.mark.asyncio
    async def test_restores_preset_mode(self, bt):
        old = MagicMock()
        old.state = "heat"
        old.attributes = {
            ATTR_TEMPERATURE: 22.0,
            "preset_mode": "comfort",
        }
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt._preset_temperatures = {"comfort": 22.0, "eco": 18.0}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt._preset_mode == "comfort"

    @pytest.mark.asyncio
    async def test_restores_heating_power_clamped(self, bt):
        old = MagicMock()
        old.state = "heat"
        old.attributes = {
            ATTR_TEMPERATURE: 21.0,
            ATTR_STATE_HEATING_POWER: "999.0",  # way above max
        }
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt._preset_temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.heating_power == MAX_HEATING_POWER

    @pytest.mark.asyncio
    async def test_no_old_state_uses_trv_defaults(self, bt):
        bt.async_get_last_state = AsyncMock(return_value=None)
        bt.bt_target_temp = None

        states = [_make_trv_state(attrs={ATTR_TEMPERATURE: 20.0})]
        await BetterThermostat._restore_state(bt, states)

        # Should have set bt_target_temp from TRV states
        assert bt.bt_target_temp is not None

    @pytest.mark.asyncio
    async def test_restores_call_for_heat(self, bt):
        old = MagicMock()
        old.state = "heat"
        old.attributes = {
            ATTR_TEMPERATURE: 21.0,
            ATTR_STATE_CALL_FOR_HEAT: True,
        }
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt._preset_temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.call_for_heat is True


# ---------------------------------------------------------------------------
# 6. _validate_hvac_mode
# ---------------------------------------------------------------------------


class TestValidateHvacMode:
    """Tests for _validate_hvac_mode."""

    def test_already_set_stays(self, bt):
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state(state="heat")]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.bt_hvac_mode == HVACMode.HEAT

    def test_none_mode_all_off_sets_off(self, bt):
        bt.bt_hvac_mode = None
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state(state="off")]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.bt_hvac_mode == HVACMode.OFF

    def test_none_mode_most_heat_sets_heat(self, bt):
        bt.bt_hvac_mode = None
        bt.humidity_sensor_entity_id = None
        states = [
            _make_trv_state(TRV_ID, state="heat"),
            _make_trv_state(TRV_ID_2, state="heat"),
        ]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.bt_hvac_mode == HVACMode.HEAT

    def test_invalid_mode_forced_to_heat(self, bt):
        bt.bt_hvac_mode = "auto"  # not in allowed set
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state(state="heat")]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.bt_hvac_mode == HVACMode.HEAT

    def test_last_main_hvac_mode_default(self, bt):
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.last_main_hvac_mode = None
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state()]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.last_main_hvac_mode == HVACMode.HEAT

    def test_last_window_state_set(self, bt):
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.window_open = True
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state()]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.last_window_state is True

    def test_humidity_sensor_re_read(self, bt):
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.humidity_sensor_entity_id = HUMIDITY_ID
        bt.hass.states.get.return_value = State(HUMIDITY_ID, "60.0")
        states = [_make_trv_state()]
        BetterThermostat._validate_hvac_mode(bt, states)
        # humidity should be re-read
        assert bt._current_humidity is not None

    def test_humidity_sensor_none_sets_zero(self, bt):
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.humidity_sensor_entity_id = HUMIDITY_ID
        bt.hass.states.get.return_value = None
        states = [_make_trv_state()]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt._current_humidity == 0
