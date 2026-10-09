"""Tests for Better Thermostat sensor platform (sensor.py).

Covers:
  - Sensor entity classes (state updates, availability, EMA math)
  - Setup & algorithm sensor creation
  - Active algorithm detection
  - Entity cleanup functions (preset, PID, switch)
  - Unload cleanup
"""

from collections.abc import Iterable
import logging
import math
from time import monotonic
from unittest.mock import MagicMock, patch

from homeassistant.components.sensor import SensorEntity
import pytest

from custom_components.better_thermostat import BetterThermostatData
from custom_components.better_thermostat.sensor import (
    _ACTIVE_ALGORITHM_ENTITIES,
    _ACTIVE_PID_NUMBERS,
    _ACTIVE_PRESET_NUMBERS,
    _ACTIVE_SWITCH_ENTITIES,
    _DISPATCHER_UNSUBSCRIBES,
    _ENTITY_CLEANUP_CALLBACKS,
    BetterThermostatExternalTemp1hEMASensor,
    BetterThermostatExternalTempSensor,
    BetterThermostatHeatingPowerSensor,
    BetterThermostatHeatLossSensor,
    BetterThermostatMpcGainSensor,
    BetterThermostatMpcKaSensor,
    BetterThermostatMpcLossSensor,
    BetterThermostatMpcV2CouplingSensor,
    BetterThermostatMpcV2DisturbanceSensor,
    BetterThermostatMpcV2RoomTimeConstantSensor,
    BetterThermostatMpcV2VirtualTempSensor,
    BetterThermostatPidErrorSensor,
    BetterThermostatPidOutputSensor,
    BetterThermostatTempSlopeSensor,
    BetterThermostatVirtualTempSensor,
    _BtMpcSensorBase,
    _BtSensorBase,
    _BtSimpleAttributeSensor,
    _cleanup_pid_number_entities,
    _cleanup_pid_switch_entities,
    _cleanup_preset_number_entities,
    _cleanup_stale_algorithm_entities,
    _debug_number,
    _get_active_algorithms,
    _get_filtered_temperature,
    _handle_dynamic_entity_update,
    _release_entry,
    _setup_algorithm_sensors,
    async_setup_entry,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION_MODE,
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
)
from custom_components.better_thermostat.utils.entry_schema import TrvSettings
from tests.factories import (
    ThermostatStandIn,
    make_calibration_balance,
    make_entity_registry,
    make_registry_entry,
)

DOMAIN = "better_thermostat"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _native_number(sensor: SensorEntity) -> float:
    """Return the sensor's native value, which these sensors publish as a number."""
    value = sensor._attr_native_value
    assert isinstance(value, int | float)
    return value


def _unique_ids(sensors: Iterable[SensorEntity]) -> list[str]:
    """Return the unique ids of sensors, every one of which carries one."""
    unique_ids: list[str] = []
    for sensor in sensors:
        assert sensor.unique_id is not None
        unique_ids.append(sensor.unique_id)
    return unique_ids


def _make_bt_climate(**overrides):
    """Create a mock BT climate entity with sensible defaults."""
    bt = ThermostatStandIn()
    bt.unique_id = "test_bt_123"
    bt.device_name = "Test BT"
    bt.entity_id = "climate.test_bt"
    bt.device_info = {"identifiers": {(DOMAIN, "test_bt_123")}}
    bt._available = True
    bt.window_open = False
    bt.hvac_mode = "heat"
    bt.room_temperature_filtered = None
    bt.room_temperature_ema = None
    bt.temperature_slope = None
    bt.heating_power = None
    bt.heat_loss_rate = None
    bt.real_trvs = dict[str, Trv]()
    bt.all_trvs = list[TrvSettings]()
    bt.preset_modes = list[str]()
    bt.door_open = False
    for k, v in overrides.items():
        setattr(bt, k, v)
    bt.contact_open = bool(bt.window_open) or bool(bt.door_open)
    return bt


def _make_entry(entry_id="entry_1", climate=None):
    """Create a mock ConfigEntry loaded with ``climate`` as its climate entity."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = BetterThermostatData(climate=climate)
    return entry


def _make_entity_registry():
    """Create a mock EntityRegistry that holds none of the looked-up ids."""
    reg = make_entity_registry()
    reg.async_get_entity_id = MagicMock(return_value=None)
    reg.async_remove = MagicMock()
    return reg


def _trvs_in_modes(*modes):
    """Build one real Trv per calibration mode, keyed trv_1, trv_2, ..."""
    return {
        f"trv_{index}": Trv(
            entity_id=f"trv_{index}", advanced={CONF_CALIBRATION_MODE: mode}
        )
        for index, mode in enumerate(modes, start=1)
    }


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
        """Unique id."""
        bt = _make_bt_climate()
        sensor = BetterThermostatExternalTempSensor(bt)
        assert sensor._attr_unique_id == "test_bt_123_external_temp_ema"

    def test_update_from_room_temperature_filtered(self):
        """Update from the filtered room temperature."""
        bt = _make_bt_climate(room_temperature_filtered=21.5)
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 21.5

    def test_fallback_to_room_temperature_ema(self):
        """Fallback to external temperature ema."""
        bt = _make_bt_climate(room_temperature_filtered=None, room_temperature_ema=22.3)
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 22.3

    def test_none_when_both_missing(self):
        """None when both missing."""
        bt = _make_bt_climate(room_temperature_filtered=None, room_temperature_ema=None)
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_float_returns_none(self):
        """Invalid float returns none."""
        bt = _make_bt_climate(room_temperature_filtered="not_a_number")
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_string_number_converted(self):
        """A string like '20.5' should be converted to float."""
        bt = _make_bt_climate(room_temperature_filtered="20.5")
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 20.5


# ===========================================================================
# 2. External Temp 1h EMA Sensor
# ===========================================================================


class TestExternalTemp1hEMASensor:
    """Tests for BetterThermostatExternalTemp1hEMASensor."""

    def test_unique_id(self):
        """Unique id."""
        bt = _make_bt_climate()
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        assert sensor._attr_unique_id == "test_bt_123_external_temp_ema_1h"

    def test_first_update_sets_ema_directly(self):
        """First update sets ema directly."""
        bt = _make_bt_climate(room_temperature_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 20.0
        assert sensor._ema_value == 20.0

    def test_subsequent_update_applies_ema(self):
        """Subsequent update applies ema."""
        bt = _make_bt_climate(room_temperature_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()  # first
        # Simulate time passing
        sensor._last_update_ts = monotonic() - 60  # 1 minute ago
        bt.room_temperature_filtered = 25.0
        sensor._update_state()  # second
        # EMA should be between 20 and 25, closer to 20
        assert 20.0 < _native_number(sensor) < 25.0

    def test_ema_converges_over_time(self):
        """After many tau periods, EMA should be very close to new value."""
        bt = _make_bt_climate(room_temperature_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()
        # Simulate 5 tau (5 hours) passing in one step
        sensor._last_update_ts = monotonic() - (5 * 3600)
        bt.room_temperature_filtered = 25.0
        sensor._update_state()
        # After 5 tau, alpha ≈ 1 - e^(-5) ≈ 0.993
        assert abs(_native_number(sensor) - 25.0) < 0.1

    def test_zero_dt_does_not_change_ema(self):
        """When dt=0, alpha=0, EMA should not change."""
        bt = _make_bt_climate(room_temperature_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()  # first → EMA = 20.0
        # Set last_update_ts to now so dt ≈ 0
        sensor._last_update_ts = monotonic()
        bt.room_temperature_filtered = 30.0
        sensor._update_state()
        # dt ≈ 0 → alpha ≈ 0 → EMA stays at 20.0
        assert sensor._attr_native_value == 20.0

    def test_none_value_gives_none(self):
        """None value gives none."""
        bt = _make_bt_climate(room_temperature_filtered=None, room_temperature_ema=None)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_float_gives_none(self):
        """Invalid float gives none."""
        bt = _make_bt_climate(room_temperature_filtered="invalid")
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_ema_math_correctness(self):
        """Verify the EMA formula matches expected math."""
        bt = _make_bt_climate(room_temperature_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_ema(20.0)  # first
        dt_seconds = 600.0  # 10 minutes
        sensor._last_update_ts = monotonic() - dt_seconds
        sensor._update_ema(25.0)
        expected_alpha = 1.0 - math.exp(-dt_seconds / 3600.0)
        expected_ema = 20.0 + expected_alpha * (25.0 - 20.0)
        assert sensor._ema_value is not None
        assert abs(sensor._ema_value - expected_ema) < 0.001


# ===========================================================================
# 3. Simple attribute sensors (TempSlope, HeatingPower, HeatLoss)
# ===========================================================================


class TestSimpleAttributeSensors:
    """Tests for sensors that read a single attribute."""

    def test_temperature_slope_with_value(self):
        """Temp slope with value."""
        bt = _make_bt_climate(temperature_slope=0.0123)
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.0123

    def test_temperature_slope_rounds_to_4_decimals(self):
        """Temp slope rounds to 4 decimals."""
        bt = _make_bt_climate(temperature_slope=0.01236789)
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.0124

    def test_temperature_slope_none(self):
        """Temp slope none."""
        bt = _make_bt_climate(temperature_slope=None)
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_heating_power_with_value(self):
        """Heating power with value."""
        bt = _make_bt_climate(heating_power=0.05)
        sensor = BetterThermostatHeatingPowerSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.05

    def test_heating_power_none(self):
        """Heating power none."""
        bt = _make_bt_climate(heating_power=None)
        sensor = BetterThermostatHeatingPowerSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_heat_loss_with_value(self):
        """Heat loss with value."""
        bt = _make_bt_climate(heat_loss_rate=0.03)
        sensor = BetterThermostatHeatLossSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.03

    def test_heat_loss_none(self):
        """Heat loss none."""
        bt = _make_bt_climate(heat_loss_rate=None)
        sensor = BetterThermostatHeatLossSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_string_returns_none(self):
        """Invalid string returns none."""
        bt = _make_bt_climate(temperature_slope="not_a_number")
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None


# ===========================================================================
# 4. MPC Sensors (VirtualTemp, Gain, Loss, Ka) – availability & state
# ===========================================================================


class TestMpcSensorAvailability:
    """Tests for the shared availability logic of MPC sensors."""

    @pytest.mark.parametrize(
        "sensor_class",
        [
            BetterThermostatVirtualTempSensor,
            BetterThermostatMpcGainSensor,
            BetterThermostatMpcLossSensor,
            BetterThermostatMpcKaSensor,
        ],
    )
    def test_available_when_all_ok(self, sensor_class):
        """Available when all ok."""
        bt = _make_bt_climate(_available=True, window_open=False, hvac_mode="heat")
        sensor = sensor_class(bt)
        assert sensor.available is True

    @pytest.mark.parametrize(
        "sensor_class",
        [
            BetterThermostatVirtualTempSensor,
            BetterThermostatMpcGainSensor,
            BetterThermostatMpcLossSensor,
            BetterThermostatMpcKaSensor,
        ],
    )
    def test_unavailable_when_climate_unavailable(self, sensor_class):
        """Unavailable when climate unavailable."""
        bt = _make_bt_climate(_available=False)
        sensor = sensor_class(bt)
        assert sensor.available is False

    @pytest.mark.parametrize(
        "sensor_class",
        [
            BetterThermostatVirtualTempSensor,
            BetterThermostatMpcGainSensor,
            BetterThermostatMpcLossSensor,
            BetterThermostatMpcKaSensor,
        ],
    )
    def test_unavailable_when_window_open(self, sensor_class):
        """Unavailable when window open."""
        bt = _make_bt_climate(window_open=True)
        sensor = sensor_class(bt)
        assert sensor.available is False

    @pytest.mark.parametrize(
        "sensor_class",
        [
            BetterThermostatVirtualTempSensor,
            BetterThermostatMpcGainSensor,
            BetterThermostatMpcLossSensor,
            BetterThermostatMpcKaSensor,
        ],
    )
    def test_unavailable_when_hvac_off(self, sensor_class):
        """Unavailable when hvac off."""
        bt = _make_bt_climate(hvac_mode="off")
        sensor = sensor_class(bt)
        assert sensor.available is False

    @pytest.mark.parametrize(
        "sensor_class",
        [
            BetterThermostatVirtualTempSensor,
            BetterThermostatMpcGainSensor,
            BetterThermostatMpcLossSensor,
            BetterThermostatMpcKaSensor,
        ],
    )
    def test_available_false_when_not_available(self, sensor_class):
        """If _available is False, sensor should be unavailable."""
        bt = _make_bt_climate()
        bt._available = False
        sensor = sensor_class(bt)
        assert sensor.available is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (21.5, 21.5),
        (3, 3.0),
        ("0.25", 0.25),
        ("bad", None),
        (None, None),
        ({"nested": 1.0}, None),
    ],
)
def test_debug_number_keeps_numbers_and_drops_everything_else(value, expected):
    """A debug value becomes a float when numeric and None otherwise."""
    assert _debug_number(value) == expected


class TestMpcSensorState:
    """Tests for MPC sensor state retrieval from calibration_balance debug."""

    def _make_trv_with_debug(self, **debug_values):
        return {
            "trv_1": Trv(
                entity_id="trv_1",
                calibration_balance=make_calibration_balance(
                    CalibrationMode.MPC_CALIBRATION, debug_values
                ),
            )
        }

    def test_virtual_temperature_reads_from_debug(self):
        """Virtual temperature reads from debug."""
        bt = _make_bt_climate(
            real_trvs=self._make_trv_with_debug(**{"mpc_virtual_temp": "22.500"})
        )
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 22.5

    def test_mpc_gain_reads_from_debug(self):
        """Mpc gain reads from debug."""
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(mpc_gain=0.05))
        sensor = BetterThermostatMpcGainSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.05

    def test_mpc_loss_reads_from_debug(self):
        """Mpc loss reads from debug."""
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(mpc_loss=0.03))
        sensor = BetterThermostatMpcLossSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.03

    def test_mpc_ka_reads_from_debug(self):
        """Mpc ka reads from debug."""
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(mpc_ka=0.001))
        sensor = BetterThermostatMpcKaSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.001

    def test_no_calibration_balance_returns_none(self):
        """No calibration balance returns none."""
        bt = _make_bt_climate(real_trvs={"trv_1": Trv(entity_id="trv_1")})
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_debug_without_the_reading_returns_none(self):
        """A debug payload without the virtual temperature returns none."""
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(mpc_gain=0.05))
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_a_payload_of_another_controller_is_not_read(self):
        """A PID payload under the same key leaves the sensor empty."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    calibration_balance=make_calibration_balance(
                        CalibrationMode.PID_CALIBRATION, {"mpc_gain": 0.05}
                    ),
                )
            }
        )
        sensor = BetterThermostatMpcGainSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_real_trvs_none_returns_none(self):
        """Real trvs none returns none."""
        bt = _make_bt_climate(real_trvs=None)
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_empty_real_trvs_returns_none(self):
        """Empty real trvs returns none."""
        bt = _make_bt_climate(real_trvs={})
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_debug_value_returns_none(self):
        """Invalid debug value returns none."""
        bt = _make_bt_climate(
            real_trvs=self._make_trv_with_debug(**{"mpc_virtual_temp": "bad"})
        )
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_first_trv_with_debug_wins(self):
        """When multiple TRVs exist, the first with debug data should be used."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(entity_id="trv_1"),
                "trv_2": Trv(
                    entity_id="trv_2",
                    calibration_balance=make_calibration_balance(
                        CalibrationMode.MPC_CALIBRATION, {"mpc_virtual_temp": "23.000"}
                    ),
                ),
            }
        )
        sensor = BetterThermostatVirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 23.0


class TestMpcV2SensorState:
    """The MPC v2 sensors read the payload MPC v2 publishes, and only that one."""

    @staticmethod
    def _trv(name, controller, **debug):
        return Trv(
            entity_id=name,
            calibration_balance=make_calibration_balance(controller, debug),
        )

    @classmethod
    def _v2_trv(cls, name, **overrides):
        debug = {
            "T_room_hat": 21.0,
            "T_rad_hat": 30.0,
            "D_hat_K_per_min": 0.0,
            "tau_room_min": 120.0,
            "coupling_rad_room": 0.5,
            "group_valve_pct": 40.0,
            "distributed_valve_pct": 40.0,
            "controller_version": "v2",
            "reid_tau_room": None,
            "reid_gain": None,
        }
        return cls._trv(name, CalibrationMode.MPC_V2_CALIBRATION, **(debug | overrides))

    @pytest.mark.parametrize(
        ("sensor_class", "debug_key", "value"),
        [
            (BetterThermostatMpcV2VirtualTempSensor, "T_room_hat", 20.75),
            (BetterThermostatMpcV2CouplingSensor, "coupling_rad_room", 0.42),
            (BetterThermostatMpcV2DisturbanceSensor, "D_hat_K_per_min", -0.012),
            (BetterThermostatMpcV2RoomTimeConstantSensor, "tau_room_min", 185.0),
        ],
    )
    def test_each_sensor_shows_its_value_of_the_v2_payload(
        self, sensor_class, debug_key, value
    ):
        bt = _make_bt_climate(
            real_trvs={"trv_1": self._v2_trv("trv_1", **{debug_key: value})}
        )
        sensor = sensor_class(bt)
        sensor._update_state()
        assert sensor._attr_native_value == value

    def test_a_payload_of_another_controller_is_not_read(self):
        """An MPC v1 payload under the same key leaves the sensor empty."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": self._trv(
                    "trv_1", CalibrationMode.MPC_CALIBRATION, T_room_hat=21.0
                )
            }
        )
        sensor = BetterThermostatMpcV2VirtualTempSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_the_first_head_with_a_v2_value_is_shown(self):
        """Heads without a balance, or on another controller, are passed over."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(entity_id="trv_1"),
                "trv_2": self._trv(
                    "trv_2", CalibrationMode.MPC_CALIBRATION, tau_room_min=1.0
                ),
                "trv_3": self._trv("trv_3", CalibrationMode.PID_CALIBRATION, u=1.0),
                "trv_4": self._v2_trv("trv_4", tau_room_min=90.0),
                "trv_5": self._v2_trv("trv_5", tau_room_min=30.0),
            }
        )
        sensor = BetterThermostatMpcV2RoomTimeConstantSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 90.0

    def test_no_heads_leave_the_sensor_empty(self):
        bt = _make_bt_climate(real_trvs={})
        sensor = BetterThermostatMpcV2CouplingSensor(bt)
        sensor._attr_native_value = 0.5
        sensor._update_state()
        assert sensor._attr_native_value is None


class TestPidSensorState:
    """Tests for PID sensor state retrieval from calibration_balance debug."""

    def _make_trv_with_debug(self, **debug_values):
        return {
            "trv_1": Trv(
                entity_id="trv_1",
                calibration_balance=make_calibration_balance(
                    CalibrationMode.PID_CALIBRATION, {"mode": "pid", **debug_values}
                ),
            )
        }

    @pytest.mark.parametrize(
        ("sensor_class", "debug_key", "value"),
        [
            (BetterThermostatPidOutputSensor, "u", 42.5),
            (BetterThermostatPidErrorSensor, "e_K", -0.3),
        ],
    )
    def test_reads_value_from_debug(self, sensor_class, debug_key, value):
        """Each PID sensor reads its debug key from calibration_balance."""
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(**{debug_key: value}))
        sensor = sensor_class(bt)
        sensor._update_state()
        assert sensor._attr_native_value == value

    def test_missing_debug_key_returns_none(self):
        """A PID sensor whose key is absent from debug reports None."""
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(u=42.5))
        sensor = BetterThermostatPidErrorSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_debug_value_returns_none(self):
        """A non-numeric debug value is coerced to None."""
        bt = _make_bt_climate(real_trvs=self._make_trv_with_debug(u="bad"))
        sensor = BetterThermostatPidOutputSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    @pytest.mark.parametrize(
        "sensor_class",
        [BetterThermostatPidOutputSensor, BetterThermostatPidErrorSensor],
    )
    def test_unavailable_when_hvac_off(self, sensor_class):
        """PID sensors are unavailable when the thermostat is off."""
        bt = _make_bt_climate(hvac_mode="off")
        sensor = sensor_class(bt)
        assert sensor.available is False


# ===========================================================================
# ===========================================================================
# 6. _get_active_algorithms
# ===========================================================================


class TestGetActiveAlgorithms:
    """Tests for _get_active_algorithms helper."""

    def test_no_real_trvs_returns_empty(self):
        """No real trvs returns empty."""
        bt = _make_bt_climate(real_trvs={})
        assert _get_active_algorithms(bt) == set()

    def test_real_trvs_none_returns_empty(self):
        """Real trvs none returns empty."""
        bt = _make_bt_climate(real_trvs=None)
        assert _get_active_algorithms(bt) == set()

    def test_mpc_calibration_detected(self):
        """Mpc calibration detected."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION},
                )
            }
        )
        result = _get_active_algorithms(bt)
        assert CalibrationMode.MPC_CALIBRATION in result

    def test_string_calibration_mode_converted(self):
        """String values should be auto-converted to CalibrationMode enum."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: "mpc_calibration"},
                )
            }
        )
        result = _get_active_algorithms(bt)
        assert CalibrationMode.MPC_CALIBRATION in result

    def test_invalid_calibration_mode_skipped(self):
        """Invalid calibration mode skipped."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: "totally_invalid_mode"},
                )
            }
        )
        result = _get_active_algorithms(bt)
        assert result == set()

    def test_multiple_trvs_different_modes(self):
        """Multiple trvs different modes."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION},
                ),
                "trv_2": Trv(
                    entity_id="trv_2",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION},
                ),
            }
        )
        result = _get_active_algorithms(bt)
        assert result == {
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        }

    def test_none_calibration_mode_reports_the_default_mode(self):
        """A stored ``None`` runs the default mode, so that mode is active."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(entity_id="trv_1", advanced={CONF_CALIBRATION_MODE: None})
            }
        )
        result = _get_active_algorithms(bt)
        assert result == {DEFAULT_CALIBRATION_MODE}

    def test_missing_advanced_key_reports_the_default_mode(self):
        """A TRV without advanced settings runs the default mode."""
        bt = _make_bt_climate(real_trvs={"trv_1": Trv(entity_id="trv_1")})
        result = _get_active_algorithms(bt)
        assert result == {DEFAULT_CALIBRATION_MODE}

    def test_a_mis_cased_mode_is_the_mode_the_calibration_runs(self):
        """A mis-cased mode name brings the sensors of the mode the calibration runs."""
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: "MPC_Calibration"},
                )
            }
        )
        result = _get_active_algorithms(bt)
        assert result == {CalibrationMode.MPC_CALIBRATION}

    def test_real_trvs_none_returns_empty_and_returns_set(self):
        """Real trvs none returns empty and returns set."""
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
        """Mpc creates four sensors."""
        hass = MagicMock()
        entry = _make_entry(climate=None)
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION},
                )
            }
        )
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            sensors = await _setup_algorithm_sensors(hass, entry, bt)
        assert len(sensors) == 4
        assert isinstance(sensors[0], BetterThermostatVirtualTempSensor)

    @pytest.mark.asyncio
    async def test_no_algorithms_returns_empty(self):
        """No algorithms returns empty."""
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
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION},
                )
            }
        )
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
    async def test_mpc_tracking_names_exactly_the_created_sensors(self):
        """The tracked MPC unique_ids are the ones of the sensors just created.

        Cleanup removes what it tracks; an id without a sensor can never be
        removed, and a sensor without an id is never cleaned up.
        """
        bt = _make_bt_climate(real_trvs=_trvs_in_modes(CalibrationMode.MPC_CALIBRATION))
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            sensors = await _setup_algorithm_sensors(MagicMock(), _make_entry(), bt)

        tracked_ids = _ACTIVE_ALGORITHM_ENTITIES["entry_1"][
            CalibrationMode.MPC_CALIBRATION
        ]
        assert sorted(tracked_ids) == sorted(_unique_ids(sensors))

    @pytest.mark.asyncio
    async def test_dropping_mpc_removes_it_from_tracking(self):
        """Once MPC is no longer configured and its sensors are gone, it is untracked.

        The registry knows exactly the sensors setup created; removing all of
        them is a complete cleanup.
        """
        bt = _make_bt_climate(real_trvs=_trvs_in_modes(CalibrationMode.MPC_CALIBRATION))
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            sensors = await _setup_algorithm_sensors(MagicMock(), _make_entry(), bt)

        registered = {s.unique_id: f"sensor.{s.unique_id}" for s in sensors}
        reg = _make_entity_registry()
        reg.async_get_entity_id.side_effect = lambda _domain, _platform, unique_id: (
            registered.get(unique_id)
        )
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=reg,
        ):
            await _cleanup_stale_algorithm_entities(
                hass=MagicMock(),
                entry_id="entry_1",
                bt_climate=bt,
                current_algorithms=set(),
            )

        assert reg.async_remove.call_count == len(registered)
        assert "entry_1" not in _ACTIVE_ALGORITHM_ENTITIES

    @pytest.mark.asyncio
    async def test_mpc_and_mpc_v2_sensors_have_distinct_unique_ids(self):
        """Heads calibrated by MPC v1 and v2 in one room get separate sensors.

        Home Assistant rejects a second entity with a unique_id it already
        knows, so a shared id silently drops one of the two sets.
        """
        bt = _make_bt_climate(
            real_trvs=_trvs_in_modes(
                CalibrationMode.MPC_CALIBRATION, CalibrationMode.MPC_V2_CALIBRATION
            )
        )
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            sensors = await _setup_algorithm_sensors(MagicMock(), _make_entry(), bt)

        unique_ids = [s.unique_id for s in sensors]
        assert len(unique_ids) == 8
        assert len(set(unique_ids)) == len(unique_ids), unique_ids

    @pytest.mark.parametrize(
        ("translation_key", "moved"),
        [("mpc_v2_virtual_temp", True), ("virtual_temp", False)],
        ids=["mpc_v2_entry", "mpc_v1_entry"],
    )
    @pytest.mark.asyncio
    async def test_mpc_v2_takes_over_its_entry_from_the_mpc_v1_unique_id(
        self, translation_key, moved
    ):
        """An MPC v2 sensor keeps the registry entry it was registered under.

        An entry an MPC v2 sensor registered under the MPC v1 unique_id moves
        to the MPC v2 unique_id, keeping its entity_id; an MPC v1 sensor's
        entry stays with MPC v1.
        """
        shared = make_registry_entry(
            "sensor.test_bt_virtual_temperature",
            unique_id="test_bt_123_virtual_temp",
            platform=DOMAIN,
            translation_key=translation_key,
        )
        reg = make_entity_registry(shared)
        reg.async_get_entity_id.side_effect = lambda domain, platform, unique_id: (
            reg.entities.get_entity_id((domain, platform, unique_id))
        )
        bt = _make_bt_climate(
            real_trvs=_trvs_in_modes(CalibrationMode.MPC_V2_CALIBRATION)
        )
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=reg,
        ):
            await _setup_algorithm_sensors(MagicMock(), _make_entry(), bt)

        if moved:
            reg.async_update_entity.assert_called_once_with(
                shared.entity_id, new_unique_id="test_bt_123_mpc_v2_virtual_temp"
            )
        else:
            reg.async_update_entity.assert_not_called()


# ===========================================================================
# 8. async_setup_entry
# ===========================================================================


class TestAsyncSetupEntry:
    """Tests for async_setup_entry."""

    @pytest.mark.asyncio
    async def test_no_climate_returns_early(self):
        """If climate entity not found, no sensors should be added."""
        hass = MagicMock()
        entry = _make_entry(climate=None)
        async_add_entities = MagicMock()

        await async_setup_entry(hass, entry, async_add_entities)
        async_add_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_five_core_sensors(self):
        """Should create 5 core sensors when climate exists."""
        bt = _make_bt_climate()
        hass = MagicMock()
        entry = _make_entry(climate=bt)
        async_add_entities = MagicMock()

        with (
            patch(
                "custom_components.better_thermostat.sensor._setup_algorithm_sensors",
                return_value=[],
            ),
            patch(
                "custom_components.better_thermostat.sensor._register_dynamic_entity_callback"
            ),
        ):
            await async_setup_entry(hass, entry, async_add_entities)

        async_add_entities.assert_called_once()
        sensors = async_add_entities.call_args[0][0]
        assert len(sensors) == 5

    @pytest.mark.asyncio
    async def test_a_setup_retried_after_a_failure_creates_the_algorithm_sensors(self):
        """A record left by a setup that failed part way does not hide sensors.

        The first setup tracks the MPC sensors and then fails before the
        entry registers its unload cleanup. The retry creates them again.
        """
        bt = _make_bt_climate(real_trvs=_trvs_in_modes(CalibrationMode.MPC_CALIBRATION))
        hass = MagicMock()
        entry = _make_entry(climate=bt)
        async_add_entities = MagicMock()

        with (
            patch(
                "custom_components.better_thermostat.sensor.async_get_entity_registry",
                return_value=_make_entity_registry(),
            ),
            patch(
                "custom_components.better_thermostat.sensor._register_dynamic_entity_callback"
            ),
            patch(
                "custom_components.better_thermostat.sensor.async_normalize_bt_entity_ids",
                side_effect=[RuntimeError("registry busy"), None],
            ),
        ):
            with pytest.raises(RuntimeError):
                await async_setup_entry(hass, entry, async_add_entities)
            await async_setup_entry(hass, entry, async_add_entities)

        sensors = async_add_entities.call_args[0][0]
        assert sum(isinstance(s, BetterThermostatMpcGainSensor) for s in sensors) == 1


# ===========================================================================
# 9. _release_entry
# ===========================================================================


class TestReleaseEntry:
    """Tests for _release_entry, which the entry's unload runs."""

    def test_unsubscribes_dispatcher(self):
        """Unsubscribes dispatcher."""
        entry = _make_entry()
        unsub = MagicMock()
        _DISPATCHER_UNSUBSCRIBES["entry_1"] = unsub

        _release_entry(entry.entry_id)
        unsub.assert_called_once()
        assert "entry_1" not in _DISPATCHER_UNSUBSCRIBES

    def test_cleans_all_tracking_dicts(self):
        """Cleans all tracking dicts."""
        entry = _make_entry()
        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {
            CalibrationMode.MPC_CALIBRATION: ["id1"]
        }
        _ENTITY_CLEANUP_CALLBACKS["entry_1"] = MagicMock()
        _ACTIVE_PRESET_NUMBERS["entry_1"] = {"uid": dict[str, str | bool]()}
        _ACTIVE_PID_NUMBERS["entry_1"] = {"uid": dict[str, str]()}
        _ACTIVE_SWITCH_ENTITIES["entry_1"] = {"uid": dict[str, str]()}

        _release_entry(entry.entry_id)

        assert "entry_1" not in _ACTIVE_ALGORITHM_ENTITIES
        assert "entry_1" not in _ENTITY_CLEANUP_CALLBACKS
        assert "entry_1" not in _ACTIVE_PRESET_NUMBERS
        assert "entry_1" not in _ACTIVE_PID_NUMBERS
        assert "entry_1" not in _ACTIVE_SWITCH_ENTITIES

    def test_no_dispatcher_no_error(self):
        """Unloading an entry without registered dispatcher should not fail."""
        _release_entry("entry_1")
        assert "entry_1" not in _DISPATCHER_UNSUBSCRIBES


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
            CalibrationMode.MPC_CALIBRATION: ["uid_1", "uid_2"]
        }

        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=reg,
        ):
            bt = _make_bt_climate()
            # current_algorithms is empty → MPC should be cleaned up
            await _cleanup_stale_algorithm_entities(
                hass=MagicMock(),
                entry_id="entry_1",
                bt_climate=bt,
                current_algorithms=set(),
            )

        assert reg.async_remove.call_count == 2
        # Tracking should be cleaned up
        assert "entry_1" not in _ACTIVE_ALGORITHM_ENTITIES

    @pytest.mark.asyncio
    async def test_keeps_active_algorithms(self):
        """Entities for still-active algorithms should NOT be removed."""
        reg = _make_entity_registry()
        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {
            CalibrationMode.MPC_CALIBRATION: ["uid_1"]
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
        """A removal that failed keeps the algorithm tracked for the next cleanup."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.side_effect = ["sensor.first", "sensor.second"]
        reg.async_remove.side_effect = [None, RuntimeError("registry error")]

        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {
            CalibrationMode.MPC_CALIBRATION: ["uid_1", "uid_2"]
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
                current_algorithms=set(),
            )

        assert reg.async_remove.call_count == 2
        # Only 1 of 2 was removed, so the algorithm stays tracked
        assert CalibrationMode.MPC_CALIBRATION in _ACTIVE_ALGORITHM_ENTITIES.get(
            "entry_1", {}
        )

    @pytest.mark.asyncio
    async def test_retry_after_partial_removal_clears_tracking(self):
        """A later cleanup finishes what a partial one started.

        The entity removed the first time is gone from the registry; once the
        remaining one is removed too, nothing of the algorithm is left to track.
        """
        registered = {"uid_1": "sensor.first", "uid_2": "sensor.second"}
        reg = _make_entity_registry()
        reg.async_get_entity_id.side_effect = lambda _domain, _platform, unique_id: (
            registered.get(unique_id)
        )
        failures = iter([None, RuntimeError("registry error")])

        def remove(entity_id):
            failure = next(failures, None)
            if failure is not None:
                raise failure
            registered.pop(
                next(uid for uid, eid in registered.items() if eid == entity_id)
            )

        reg.async_remove.side_effect = remove

        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {
            CalibrationMode.MPC_CALIBRATION: ["uid_1", "uid_2"]
        }

        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=reg,
        ):
            bt = _make_bt_climate()
            for _ in range(2):
                await _cleanup_stale_algorithm_entities(
                    hass=MagicMock(),
                    entry_id="entry_1",
                    bt_climate=bt,
                    current_algorithms=set(),
                )

        assert registered == {}
        assert "entry_1" not in _ACTIVE_ALGORITHM_ENTITIES

    @pytest.mark.asyncio
    async def test_remove_exception_handled_gracefully(self, caplog):
        """A registry error during removal is logged with the entity, not raised."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "sensor.entity"
        reg.async_remove.side_effect = RuntimeError("registry error")

        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = {
            CalibrationMode.MPC_CALIBRATION: ["uid_1"]
        }

        with (
            patch(
                "custom_components.better_thermostat.sensor.async_get_entity_registry",
                return_value=reg,
            ),
            caplog.at_level(
                logging.WARNING, logger="custom_components.better_thermostat.sensor"
            ),
        ):
            bt = _make_bt_climate()
            await _cleanup_stale_algorithm_entities(
                hass=MagicMock(),
                entry_id="entry_1",
                bt_climate=bt,
                current_algorithms=set(),
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "sensor.entity" in warnings[0].getMessage()
        assert "registry error" in warnings[0].getMessage()


class TestDynamicAlgorithmSensors:
    """A configuration change adds and removes algorithm sensors as TRVs use them."""

    @staticmethod
    def _registry_of(registered, failing=()):
        """Build a registry that holds `registered` and refuses to remove `failing`."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.side_effect = lambda _domain, _platform, unique_id: (
            registered.get(unique_id)
        )

        def remove(entity_id):
            if entity_id in failing:
                raise RuntimeError("registry error")
            registered.pop(
                next(uid for uid, eid in registered.items() if eid == entity_id)
            )

        reg.async_remove.side_effect = remove
        return reg

    @staticmethod
    async def _config_change(bt, reg) -> list[SensorEntity]:
        """Run one configuration change and return the entities it added."""
        async_add_entities = MagicMock()
        with (
            patch(
                "custom_components.better_thermostat.sensor.async_get_entity_registry",
                return_value=reg,
            ),
            patch(
                "custom_components.better_thermostat.sensor._cleanup_unused_number_entities"
            ),
        ):
            await _handle_dynamic_entity_update(
                MagicMock(), _make_entry(), bt, async_add_entities
            )
        if not async_add_entities.call_count:
            return []
        return list(async_add_entities.call_args.args[0])

    @pytest.mark.asyncio
    async def test_an_unchanged_mode_without_sensors_reports_no_change(self, caplog):
        """A configuration change that keeps the algorithms logs no algorithm change.

        Heating power brings no sensors of its own, so it is never tracked;
        it is still the algorithm the thermostat ran before the change.
        """
        bt = _make_bt_climate(
            real_trvs=_trvs_in_modes(CalibrationMode.HEATING_POWER_CALIBRATION)
        )
        reg = self._registry_of({})

        with caplog.at_level(
            logging.INFO, logger="custom_components.better_thermostat"
        ):
            await self._config_change(bt, reg)
            await self._config_change(bt, reg)

        assert "Algorithm configuration changed" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_second_algorithm_leaves_the_first_ones_sensors_in_place(self):
        """A TRV switching to PID beside one on MPC adds PID and keeps MPC.

        The MPC sensors stay registered and tracked; only the PID sensors are
        created.
        """
        bt = _make_bt_climate(real_trvs=_trvs_in_modes(CalibrationMode.MPC_CALIBRATION))
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            mpc_sensors = await _setup_algorithm_sensors(MagicMock(), _make_entry(), bt)
        registered = {s.unique_id: f"sensor.{s.unique_id}" for s in mpc_sensors}
        reg = self._registry_of(registered)

        bt.real_trvs = _trvs_in_modes(
            CalibrationMode.MPC_CALIBRATION, CalibrationMode.PID_CALIBRATION
        )
        added = await self._config_change(bt, reg)

        reg.async_remove.assert_not_called()
        assert len(registered) == len(mpc_sensors)
        assert {type(s) for s in added} == {
            BetterThermostatPidOutputSensor,
            BetterThermostatPidErrorSensor,
        }
        assert set(_ACTIVE_ALGORITHM_ENTITIES["entry_1"]) == {
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        }

    @pytest.mark.asyncio
    async def test_an_algorithm_used_again_after_a_partial_cleanup_gets_its_sensors_back(
        self,
    ):
        """Only the sensors a partial cleanup removed are created again.

        Dropping MPC removes three of its four sensors; the registry refuses
        the fourth, which stays live. When a TRV uses MPC again, the three
        removed ones are created and the live one is not added a second time.
        """
        bt = _make_bt_climate(real_trvs=_trvs_in_modes(CalibrationMode.MPC_CALIBRATION))
        with patch(
            "custom_components.better_thermostat.sensor.async_get_entity_registry",
            return_value=_make_entity_registry(),
        ):
            mpc_sensors = await _setup_algorithm_sensors(MagicMock(), _make_entry(), bt)
        mpc_ids = {s.unique_id for s in mpc_sensors}
        registered = {uid: f"sensor.{uid}" for uid in mpc_ids}
        refused_id = mpc_sensors[1].unique_id
        reg = self._registry_of(registered, failing={f"sensor.{refused_id}"})

        bt.real_trvs = dict[str, Trv]()
        assert await self._config_change(bt, reg) == []
        assert set(registered) == {refused_id}

        bt.real_trvs = _trvs_in_modes(CalibrationMode.MPC_CALIBRATION)
        added = await self._config_change(bt, reg)

        assert {s.unique_id for s in added} == mpc_ids - {refused_id}
        assert (
            set(_ACTIVE_ALGORITHM_ENTITIES["entry_1"][CalibrationMode.MPC_CALIBRATION])
            == mpc_ids
        )


# ===========================================================================
# 11. _cleanup_preset_number_entities
# ===========================================================================


class TestCleanupPresetNumberEntities:
    """Tests for _cleanup_preset_number_entities."""

    @pytest.mark.asyncio
    async def test_removes_disabled_preset_entities(self):
        """Removes disabled preset entities."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "number.preset_away"

        _ACTIVE_PRESET_NUMBERS["entry_1"] = {
            "uid_away": {"preset": "away"},
            "uid_home": {"preset": "home"},
        }
        bt = _make_bt_climate()
        # Only "home" is current → "away" should be removed
        await _cleanup_preset_number_entities(
            hass=MagicMock(),
            entity_registry=reg,
            entry_id="entry_1",
            bt_climate=bt,
            current_presets={"home"},
        )

        reg.async_remove.assert_called_once_with("number.preset_away")

    @pytest.mark.asyncio
    async def test_none_unique_id_skipped(self):
        """Entries with None as unique_id should be skipped."""
        reg = _make_entity_registry()
        _ACTIVE_PRESET_NUMBERS["entry_1"] = {None: {"preset": "away"}}
        bt = _make_bt_climate()
        await _cleanup_preset_number_entities(
            hass=MagicMock(),
            entity_registry=reg,
            entry_id="entry_1",
            bt_climate=bt,
            current_presets=set(),
        )
        reg.async_get_entity_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_merges_new_presets_into_tracking(self):
        """Merges new presets into tracking."""
        reg = _make_entity_registry()
        bt = _make_bt_climate()
        await _cleanup_preset_number_entities(
            hass=MagicMock(),
            entity_registry=reg,
            entry_id="entry_1",
            bt_climate=bt,
            current_presets={"comfort", "eco"},
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

        _ACTIVE_PRESET_NUMBERS["entry_1"] = {"uid_away": {"preset": "away"}}
        bt = _make_bt_climate()
        await _cleanup_preset_number_entities(
            hass=MagicMock(),
            entity_registry=reg,
            entry_id="entry_1",
            bt_climate=bt,
            current_presets=set(),
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
        """Removes pid entities for non pid trv."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "number.pid_kp"

        _ACTIVE_PID_NUMBERS["entry_1"] = {"uid_kp": {"trv": "trv_1", "param": "kp"}}
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT},
                )
            }
        )
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
        )
        reg.async_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_keeps_pid_entities_for_pid_trv(self):
        """Keeps pid entities for pid trv."""
        reg = _make_entity_registry()
        _ACTIVE_PID_NUMBERS["entry_1"] = {"uid_kp": {"trv": "trv_1", "param": "kp"}}
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION},
                )
            }
        )
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
        )
        reg.async_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_merges_pid_tracking_for_current_trvs(self):
        """Merges pid tracking for current trvs."""
        reg = _make_entity_registry()
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION},
                )
            }
        )
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
        )
        tracked = _ACTIVE_PID_NUMBERS["entry_1"]
        # Should have 3 entries for trv_1 (kp, ki, kd)
        trv_entries = [v for v in tracked.values() if v.get("trv") == "trv_1"]
        assert len(trv_entries) == 3

    @pytest.mark.asyncio
    async def test_no_real_trvs_returns_empty_pid_trvs(self):
        """If no real_trvs, no PID TRVs should be found."""
        reg = _make_entity_registry()
        _ACTIVE_PID_NUMBERS["entry_1"] = {"uid_kp": {"trv": "trv_1", "param": "kp"}}
        bt = _make_bt_climate(real_trvs={})
        reg.async_get_entity_id.return_value = "number.pid_kp"
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
        )
        reg.async_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_calibration_mode_trv_skipped(self):
        """Invalid calibration mode trv skipped."""
        reg = _make_entity_registry()
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1", advanced={CONF_CALIBRATION_MODE: "totally_bogus"}
                )
            }
        )
        await _cleanup_pid_number_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
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
        """Removes pid autotune for non pid trv."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "switch.pid_auto_tune"

        _ACTIVE_SWITCH_ENTITIES["entry_1"] = {
            "uid_autotune": {"trv": "trv_1", "type": "pid_auto_tune"}
        }
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT},
                )
            }
        )
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
        )
        reg.async_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_removes_child_lock_for_removed_trv(self):
        """Removes child lock for removed trv."""
        reg = _make_entity_registry()
        reg.async_get_entity_id.return_value = "switch.child_lock"

        _ACTIVE_SWITCH_ENTITIES["entry_1"] = {
            "uid_lock": {"trv": "trv_removed", "type": "child_lock"}
        }
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT},
                )
            }
        )
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
        )
        reg.async_remove.assert_called_once()

    @pytest.mark.asyncio
    async def test_keeps_child_lock_for_existing_trv(self):
        """Keeps child lock for existing trv."""
        reg = _make_entity_registry()
        _ACTIVE_SWITCH_ENTITIES["entry_1"] = {
            "uid_lock": {"trv": "trv_1", "type": "child_lock"}
        }
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT},
                )
            }
        )
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
        )
        reg.async_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_merges_switch_tracking(self):
        """Merges switch tracking."""
        reg = _make_entity_registry()
        bt = _make_bt_climate(
            real_trvs={
                "trv_1": Trv(
                    entity_id="trv_1",
                    advanced={CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION},
                )
            }
        )
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
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
            "uid_lock": {"trv": "trv_1", "type": "child_lock"}
        }
        bt = _make_bt_climate(real_trvs=None)
        await _cleanup_pid_switch_entities(
            hass=MagicMock(), entity_registry=reg, entry_id="entry_1", bt_climate=bt
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

    def test_1h_ema_negative_dt_clamped(self):
        """If monotonic() goes backward (shouldn't happen but defensive), dt is clamped to 0."""
        bt = _make_bt_climate(room_temperature_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_ema(20.0)
        # Set last_update to the future
        sensor._last_update_ts = monotonic() + 1000
        sensor._update_ema(25.0)
        # dt = max(0, now - future) = 0 → alpha = 0 → EMA stays at 20
        assert sensor._ema_value == 20.0

    @pytest.mark.asyncio
    async def test_cleanup_stale_empty_entry_removed(self):
        """After all algorithms removed, the entry_id key should be deleted."""
        _ACTIVE_ALGORITHM_ENTITIES["entry_1"] = dict[CalibrationMode, list[str]]()
        # empty dict → should be cleaned up
        hass = MagicMock()
        bt = _make_bt_climate()
        await _cleanup_stale_algorithm_entities(hass, "entry_1", bt, set())
        # The function checks `if not _ACTIVE_ALGORITHM_ENTITIES[entry_id]`
        # and deletes it → entry should be gone
        assert "entry_1" not in _ACTIVE_ALGORITHM_ENTITIES

    @pytest.mark.asyncio
    async def test_cleanup_preset_entities_with_empty_tracking(self):
        """Cleaning up when there are no tracked presets should not fail."""
        reg = _make_entity_registry()
        bt = _make_bt_climate()
        await _cleanup_preset_number_entities(
            hass=MagicMock(),
            entity_registry=reg,
            entry_id="entry_1",
            bt_climate=bt,
            current_presets={"comfort"},
        )
        # Should just merge without error
        assert "entry_1" in _ACTIVE_PRESET_NUMBERS

    @pytest.mark.asyncio
    async def test_get_active_algorithms_with_empty_advanced(self):
        """A TRV with empty advanced settings runs the default mode."""
        bt = _make_bt_climate(real_trvs={"trv_1": Trv(entity_id="trv_1", advanced={})})
        result = _get_active_algorithms(bt)
        assert result == {DEFAULT_CALIBRATION_MODE}

    def test_external_temperature_sensor_with_nan(self):
        """NaN as temperature value should be handled."""
        bt = _make_bt_climate(room_temperature_filtered=float("nan"))
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        # NaN is a valid float, so it will be set (but it's arguably a bug)
        assert sensor._attr_native_value is not None  # float("nan") is a float
        assert math.isnan(_native_number(sensor))

    def test_external_temperature_sensor_with_inf(self):
        """Infinity as temperature should be handled."""
        bt = _make_bt_climate(room_temperature_filtered=float("inf"))
        sensor = BetterThermostatExternalTempSensor(bt)
        sensor._update_state()
        # inf is a valid float → will be set (potentially problematic)
        assert sensor._attr_native_value == float("inf")

    def test_1h_ema_with_nan_input(self):
        """NaN input to EMA should propagate NaN."""
        bt = _make_bt_climate(room_temperature_filtered=20.0)
        sensor = BetterThermostatExternalTemp1hEMASensor(bt)
        sensor._update_ema(20.0)
        sensor._last_update_ts = monotonic() - 60
        sensor._update_ema(float("nan"))
        # NaN math: 20 + alpha * (nan - 20) = nan
        assert sensor._ema_value is not None
        assert math.isnan(sensor._ema_value)


# ===========================================================================
# 15. Base class tests
# ===========================================================================


class TestBtSensorBase:
    """Tests for _BtSensorBase.__init__ and shared behavior."""

    def test_init_sets_unique_id_from_suffix(self):
        """Init sets unique id from suffix."""
        bt = _make_bt_climate()
        sensor = BetterThermostatTempSlopeSensor(bt)
        assert sensor._attr_unique_id == "test_bt_123_temp_slope"

    def test_init_stores_bt_climate(self):
        """Init stores bt climate."""
        bt = _make_bt_climate()
        sensor = BetterThermostatHeatingPowerSensor(bt)
        assert sensor._bt_climate is bt

    def test_init_sets_device_info(self):
        """Init sets device info."""
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
        ]:
            sensor = cls(bt)
            assert isinstance(sensor, _BtSensorBase), (
                f"{cls.__name__} should inherit from _BtSensorBase"
            )

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
            assert isinstance(sensor, _BtMpcSensorBase), (
                f"{cls.__name__} should inherit from _BtMpcSensorBase"
            )

    def test_simple_sensors_inherit_from_simple_base(self):
        """Simple attribute sensors should inherit from _BtSimpleAttributeSensor."""
        bt = _make_bt_climate()
        for cls in [
            BetterThermostatTempSlopeSensor,
            BetterThermostatHeatingPowerSensor,
            BetterThermostatHeatLossSensor,
        ]:
            sensor = cls(bt)
            assert isinstance(sensor, _BtSimpleAttributeSensor), (
                f"{cls.__name__} should inherit from _BtSimpleAttributeSensor"
            )


class TestGetFilteredTemp:
    """Tests for _get_filtered_temperature helper."""

    def test_prefers_room_temperature_filtered(self):
        """Prefers the filtered room temperature."""
        bt = _make_bt_climate(room_temperature_filtered=21.5, room_temperature_ema=22.0)
        assert _get_filtered_temperature(bt) == 21.5

    def test_falls_back_to_room_temperature_ema(self):
        """Falls back to external temperature ema."""
        bt = _make_bt_climate(room_temperature_filtered=None, room_temperature_ema=22.0)
        assert _get_filtered_temperature(bt) == 22.0

    def test_returns_none_when_both_missing(self):
        """Returns none when both missing."""
        bt = _make_bt_climate(room_temperature_filtered=None, room_temperature_ema=None)
        assert _get_filtered_temperature(bt) is None

    def test_zero_value_not_treated_as_none(self):
        """Zero value not treated as none."""
        bt = _make_bt_climate(room_temperature_filtered=0.0, room_temperature_ema=22.0)
        assert _get_filtered_temperature(bt) == 0.0


class TestBtSimpleAttributeSensor:
    """Tests for _BtSimpleAttributeSensor base behavior."""

    def test_rounding_applied_when_set(self):
        """Rounding applied when set."""
        bt = _make_bt_climate(temperature_slope=0.01236789)
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.0124

    def test_no_rounding_when_none(self):
        """No rounding when none."""

        class _UnroundedHeatingPowerSensor(BetterThermostatHeatingPowerSensor):
            _rounding = None

        bt = _make_bt_climate(heating_power=0.05123456)
        sensor = _UnroundedHeatingPowerSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value == 0.05123456

    def test_learned_rates_are_published_rounded(self):
        """The learned rates reach the sensors rounded to their published grid."""
        bt = _make_bt_climate(heating_power=0.05123456, heat_loss_rate=0.01234567)
        power = BetterThermostatHeatingPowerSensor(bt)
        loss = BetterThermostatHeatLossSensor(bt)
        power._update_state()
        loss._update_state()
        assert (power._attr_native_value, loss._attr_native_value) == (0.0512, 0.01235)

    def test_none_attribute_gives_none(self):
        """None attribute gives none."""
        bt = _make_bt_climate(heating_power=None)
        sensor = BetterThermostatHeatingPowerSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None

    def test_invalid_string_gives_none(self):
        """Invalid string gives none."""
        bt = _make_bt_climate(temperature_slope="not_a_number")
        sensor = BetterThermostatTempSlopeSensor(bt)
        sensor._update_state()
        assert sensor._attr_native_value is None


class TestDynamicUpdateBelongsToTheEntry:
    """An entity update started by a configuration change ends with its entry."""

    @pytest.mark.asyncio
    async def test_the_update_task_is_owned_by_the_config_entry(self):
        """The update runs as a task of the entry, which an unload cancels.

        A task owned by Home Assistant alone would outlive the unload and add
        entities to an entry that no longer exists.
        """
        from custom_components.better_thermostat import sensor as sensor_module

        hass = MagicMock()
        entry = _make_entry("entry_owned")
        entry.async_create_background_task = MagicMock(
            side_effect=lambda _hass, coro, name: coro.close()
        )
        with patch.object(sensor_module, "async_dispatcher_connect", MagicMock()):
            await sensor_module._register_dynamic_entity_callback(
                hass, entry, _make_bt_climate(), MagicMock()
            )
        sensor_module._ENTITY_CLEANUP_CALLBACKS["entry_owned"](None)

        entry.async_create_background_task.assert_called_once()
        assert entry.async_create_background_task.call_args.args[0] is hass
        hass.async_create_background_task.assert_not_called()
