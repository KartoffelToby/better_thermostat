"""Tests for the startup() submethods extracted from BetterThermostat.startup().

Covers: _check_entities_ready, _collect_trv_states, _resolve_temperature_range,
_initialize_sensors, _restore_state, _validate_hvac_mode.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import (
    DEFAULT_FALLBACK_TEMPERATURE,
    BetterThermostat,
)
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_CALL_FOR_HEAT,
    ATTR_STATE_HEAT_LOSS,
    ATTR_STATE_HEATING_POWER,
    ATTR_STATE_PRESET_COOL_TEMPERATURES,
    DEFAULT_TARGET_TEMP,
    MAX_HEAT_LOSS,
    MAX_HEATING_POWER,
)
from custom_components.better_thermostat.utils.helpers import resolve_inbound_setpoint

SENSOR_ID = "sensor.room_temp"
TRV_ID = "climate.test_trv"
TRV_ID_2 = "climate.test_trv_2"
COOLER_ID = "climate.cooler"
WINDOW_ID = "binary_sensor.window"
DOOR_ID = "binary_sensor.door"
HUMIDITY_ID = "sensor.humidity"
OUTDOOR_ID = "sensor.outdoor_temp"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bt():
    """Create a mock BetterThermostat with sensible defaults."""
    mock = MagicMock(spec=BetterThermostat)
    mock.clock = MagicMock()
    mock.kernel_state = KernelState()
    mock._degraded_grace_until = None
    mock.state_mgr = None
    mock.hass = MagicMock()
    # climate entities publish no unit attribute, so every temperature read off
    # a child state resolves through the system unit.
    mock.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    mock.device_name = "Test BT"
    mock.sensor_entity_id = SENSOR_ID
    mock.real_trvs = {TRV_ID: {"calibration": 1}}
    mock.cooler_entity_id = None
    mock.outdoor_sensor = None
    mock.humidity_sensor_entity_id = None
    mock.window_id = None
    mock.door_id = None
    mock.all_entities = []
    mock.unavailable_sensors = []
    mock.degraded_mode = False
    mock.bt_min_temp = 5.0
    mock.bt_max_temp = 30.0
    mock.bt_target_temp = 21.0
    mock.bt_target_temp_step = None
    mock._configured_target_temp_step = None
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
    mock.contact_open = None
    mock.last_window_state = None
    mock.last_main_hvac_mode = None
    mock.call_for_heat = None
    mock._saved_temperature = None
    mock.heating_power = 0.01
    mock.heat_loss_rate = 0.01
    from custom_components.better_thermostat.utils.preset_manager import PresetManager

    mock.preset_mgr = PresetManager(
        mode="none",
        temperatures={"none": 20.0, "comfort": 22.0, "eco": 18.0},
        enabled_presets=["comfort", "eco"],
    )
    mock.preset_modes = ["none", "comfort", "eco"]
    mock.version = "1.0.0"
    mock.startup_running = True
    # The seed is the one collaborator whose effect on the instance the cooler
    # assertions read back, so it runs for real while staying observable, and so
    # do the two rules it delegates to.
    mock._seed_cool_target_from_cooler.side_effect = lambda log_source: (
        BetterThermostat._seed_cool_target_from_cooler(mock, log_source)
    )
    mock._seed_cool_target.side_effect = lambda setpoint, entity_id: (
        BetterThermostat._seed_cool_target(mock, setpoint, entity_id)
    )
    mock._enforce_cool_above_heat.side_effect = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(mock, **kwargs)
    )
    mock._bound_target_to_range = lambda value: BetterThermostat._bound_target_to_range(
        mock, value
    )
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


def _make_cooler_state(attrs, state="cool"):
    """Build a cooler State with the given setpoint attributes."""
    return State(COOLER_ID, state, attributes=attrs)


def _install_states(bt, states):
    """Route ``bt.hass.states.get`` to a per-entity mapping."""
    bt.hass.states.get.side_effect = states.get


# ---------------------------------------------------------------------------
# 0. startup() retry loop unload behavior
# ---------------------------------------------------------------------------


class TestStartupUnloadBailout:
    """The startup retry loop exits when the entity is removed."""

    @pytest.mark.asyncio
    async def test_returns_immediately_when_already_removed(self, bt):
        """Return before any readiness check when the entity is removed."""
        bt.is_removed = True
        bt.startup_running = True

        await asyncio.wait_for(BetterThermostat.startup(bt), timeout=1)

        bt._check_entities_ready.assert_not_called()
        bt._collect_trv_states.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_when_removed_during_retry_sleep(self, bt):
        """Exit the retry loop when removal happens while sleeping."""
        bt.is_removed = False
        bt.startup_running = True
        bt._check_entities_ready.return_value = False

        async def fake_sleep(_seconds):
            bt.is_removed = True

        with patch(
            "custom_components.better_thermostat.climate.asyncio.sleep",
            side_effect=fake_sleep,
        ) as mock_sleep:
            await asyncio.wait_for(BetterThermostat.startup(bt), timeout=1)

        mock_sleep.assert_awaited_once()
        bt._collect_trv_states.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_when_removed_before_trv_initialization(self, bt):
        """Stop before writing to TRVs when removal happens during restore."""
        bt.is_removed = False
        bt.startup_running = True
        bt._check_entities_ready.return_value = True

        async def fake_restore_state(_states):
            bt.is_removed = True

        bt._restore_state.side_effect = fake_restore_state

        with patch(
            "custom_components.better_thermostat.climate.check_and_update_degraded_mode",
            new=AsyncMock(),
        ):
            await asyncio.wait_for(BetterThermostat.startup(bt), timeout=1)

        bt._initialize_trvs.assert_not_called()
        bt._finalize_startup.assert_not_called()

    @pytest.mark.asyncio
    async def test_will_remove_from_hass_stops_startup_loop(self, bt):
        """Unload stops the lifecycle so the loop condition terminates."""
        bt._control_task = None
        bt._window_task = None
        bt._door_task = None

        await BetterThermostat.async_will_remove_from_hass(bt)

        assert bt.kernel_state.lifecycle.startup_running is False


# ---------------------------------------------------------------------------
# 1. _check_entities_ready
# ---------------------------------------------------------------------------


class TestCheckEntitiesReady:
    """Tests for _check_entities_ready."""

    def test_sensor_none_returns_false(self, bt):
        """Return False when sensor state is None."""
        result = BetterThermostat._check_entities_ready(bt, None)
        assert result is False

    def test_sensor_unavailable_returns_false(self, bt):
        """Return False when sensor is unavailable."""
        sensor = State(SENSOR_ID, STATE_UNAVAILABLE)
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is False

    def test_sensor_unknown_returns_false(self, bt):
        """Return False when sensor state is unknown."""
        sensor = State(SENSOR_ID, STATE_UNKNOWN)
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is False

    def test_trv_none_returns_false(self, bt):
        """Return False when TRV state is None."""
        sensor = _make_sensor_state()
        bt.hass.states.get.return_value = None
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is False

    def test_trv_unavailable_returns_false(self, bt):
        """Return False when TRV is unavailable."""
        sensor = _make_sensor_state()
        bt.hass.states.get.return_value = State(TRV_ID, STATE_UNAVAILABLE)
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is False

    def test_all_ready_returns_true(self, bt):
        """Return True when all entities are ready."""
        sensor = _make_sensor_state()
        bt.hass.states.get.return_value = _make_trv_state()
        result = BetterThermostat._check_entities_ready(bt, sensor)
        assert result is True

    def test_multiple_trvs_second_unavailable(self, bt):
        """Return False when any TRV is unavailable."""
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
        """Collect state for a single TRV."""
        trv_state = _make_trv_state()
        bt.hass.states.get.return_value = trv_state
        result = BetterThermostat._collect_trv_states(bt)
        assert len(result) == 1
        assert result[0] is trv_state

    def test_includes_cooler_when_available(self, bt):
        """Include cooler entity in collected states."""
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
        """Test Skips unavailable cooler."""
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
        """Test Missing trv state skipped."""
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
        """Test Normal range."""
        states = [_make_trv_state(attrs={"min_temp": 5.0, "max_temp": 30.0})]
        BetterThermostat._resolve_temperature_range(bt, states)
        assert bt.bt_min_temp == 5.0
        assert bt.bt_max_temp == 30.0

    def test_min_greater_than_max(self, bt):
        """When heater min > cooler max, range is still set."""
        states = [_make_trv_state(attrs={"min_temp": 20.0, "max_temp": 15.0})]
        # Set attributes so reduce_attribute returns the right values
        bt.bt_min_temp = None
        bt.bt_max_temp = None
        BetterThermostat._resolve_temperature_range(bt, states)
        # min_temp=20, max_temp=15 → warning
        assert bt.bt_min_temp == 20.0
        assert bt.bt_max_temp == 15.0

    def test_step_already_set_not_overwritten(self, bt):
        """Test Step already set not overwritten."""
        bt.bt_target_temp_step = 1.0
        states = [_make_trv_state(attrs={"target_temp_step": 0.5})]
        BetterThermostat._resolve_temperature_range(bt, states)
        assert bt.bt_target_temp_step == 1.0

    def test_step_none_gets_resolved(self, bt):
        """Test Step none gets resolved."""
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
        """Test Sensor ok sets cur temp."""
        sensor = _make_sensor_state("21.5")
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.cur_temp is not None
        assert SENSOR_ID in bt.all_entities

    def test_sensor_unavailable_falls_back_to_trv(self, bt):
        """Test Sensor unavailable falls back to trv."""
        sensor = State(SENSOR_ID, STATE_UNAVAILABLE)
        trv_state = _make_trv_state(attrs={"current_temperature": 19.5})
        bt.hass.states.get.return_value = trv_state
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.cur_temp is not None

    def test_no_sensor_no_trv_uses_default(self, bt):
        """Test No sensor no trv uses default."""
        sensor = State(SENSOR_ID, STATE_UNAVAILABLE)
        # TRV has no current_temperature
        trv_state = _make_trv_state(attrs={"current_temperature": None})
        bt.hass.states.get.return_value = trv_state
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.cur_temp == DEFAULT_FALLBACK_TEMPERATURE

    def test_implausible_sensor_value_falls_back_to_trv(self, bt):
        """AVM 126.5 °C marker from the room sensor falls back to a TRV reading."""
        sensor = _make_sensor_state("126.5")
        trv_state = _make_trv_state(attrs={"current_temperature": 19.5})
        bt.hass.states.get.return_value = trv_state
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.cur_temp == 19.5

    def test_implausible_trv_value_falls_back_to_default(self, bt):
        """If both sensor and TRV are implausible, the default fallback is used."""
        sensor = State(SENSOR_ID, STATE_UNAVAILABLE)
        trv_state = _make_trv_state(attrs={"current_temperature": 126.5})
        bt.hass.states.get.return_value = trv_state
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.cur_temp == DEFAULT_FALLBACK_TEMPERATURE

    def test_implausible_sensor_implausible_trv_uses_default(self, bt):
        """Implausible sensor AND implausible TRV → default fallback."""
        sensor = _make_sensor_state("127.0")
        trv_state = _make_trv_state(attrs={"current_temperature": 126.5})
        bt.hass.states.get.return_value = trv_state
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.cur_temp == DEFAULT_FALLBACK_TEMPERATURE

    def test_window_open_detected(self, bt):
        """Test Window open detected."""
        bt.window_id = WINDOW_ID
        sensor = _make_sensor_state("20.0")

        def side_effect(entity_id):
            if entity_id == WINDOW_ID:
                return State(WINDOW_ID, "on")
            return None

        bt.hass.states.get.side_effect = side_effect
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.kernel_state.window.effective_open is True
        assert WINDOW_ID in bt.all_entities

    def test_window_none_defaults_closed(self, bt):
        """Test Window none defaults closed."""
        bt.window_id = None
        sensor = _make_sensor_state("20.0")
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.kernel_state.window.effective_open is False

    def test_door_open_detected(self, bt):
        """Test Door open detected."""
        bt.door_id = DOOR_ID
        sensor = _make_sensor_state("20.0")

        def side_effect(entity_id):
            if entity_id == DOOR_ID:
                return State(DOOR_ID, "on")
            return None

        bt.hass.states.get.side_effect = side_effect
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.kernel_state.door.effective_open is True
        assert DOOR_ID in bt.all_entities

    def test_door_none_defaults_closed(self, bt):
        """Test Door none defaults closed."""
        bt.door_id = None
        sensor = _make_sensor_state("20.0")
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.kernel_state.door.effective_open is False

    def test_door_unavailable_assumes_closed(self, bt):
        """Test Door sensor unavailable at startup counts as closed."""
        bt.door_id = DOOR_ID
        sensor = _make_sensor_state("20.0")

        def side_effect(entity_id):
            if entity_id == DOOR_ID:
                return State(DOOR_ID, "unavailable")
            return None

        bt.hass.states.get.side_effect = side_effect
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.kernel_state.door.effective_open is False

    def test_humidity_sensor_initialized(self, bt):
        """Test Humidity sensor initialized."""
        bt.humidity_sensor_entity_id = HUMIDITY_ID
        sensor = _make_sensor_state("20.0")
        bt.hass.states.get.return_value = State(HUMIDITY_ID, "55.0")
        BetterThermostat._initialize_sensors(bt, sensor)
        assert HUMIDITY_ID in bt.all_entities

    def test_ema_initialized_with_cur_temp(self, bt):
        """Test Ema initialized with cur temp."""
        sensor = _make_sensor_state("21.5")
        with patch(
            "custom_components.better_thermostat.events.temperature._update_external_temp_ema"
        ):
            BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.last_known_external_temp is not None


# ---------------------------------------------------------------------------
# 5. Cooling target seeded from the cooler
# ---------------------------------------------------------------------------


async def _run_startup(bt, restored_target, restored_cool_target=None):
    """Run startup() with a restore step that installs the restored targets."""
    bt.is_removed = False
    bt._check_entities_ready.return_value = True

    async def _restore(_states):
        bt.bt_target_temp = restored_target
        if restored_cool_target is not None:
            bt.bt_target_cooltemp = restored_cool_target

    bt._restore_state.side_effect = _restore
    with patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()):
        await asyncio.wait_for(BetterThermostat.startup(bt), timeout=1)


class TestStartupCoolTargetSeed:
    """startup() takes a cooling target off the cooler once the restore is done.

    The cooler's own setpoint is the only value that can fill a cooling target
    nothing else supplies, and it is read where both the temperature range and
    the heating target are final: the range is what the value is clamped into,
    the heating target what it is ordered against.
    """

    @pytest.mark.asyncio
    async def test_single_setpoint_cooler_seeds_the_cool_target(self, bt):
        """A cooler driven through a single setpoint is read from temperature."""
        bt.cooler_entity_id = COOLER_ID
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 24.0})})

        await _run_startup(bt, restored_target=21.0)

        assert bt.bt_target_cooltemp == 24.0

    @pytest.mark.asyncio
    async def test_range_only_cooler_seeds_from_target_temp_high(self, bt):
        """A range-only cooler publishes an empty temperature and a range.

        Its setpoint sits in target_temp_high, so reading only temperature
        would leave the cool target unset for the whole session.
        """
        bt.cooler_entity_id = COOLER_ID
        _install_states(
            bt,
            {
                COOLER_ID: _make_cooler_state(
                    {
                        ATTR_TEMPERATURE: None,
                        "target_temp_low": 19.0,
                        "target_temp_high": 25.5,
                    }
                )
            },
        )

        await _run_startup(bt, restored_target=21.0)

        assert bt.bt_target_cooltemp == 25.5

    @pytest.mark.asyncio
    async def test_unavailable_cooler_leaves_the_cool_target_unknown(self, bt):
        """An unavailable cooler contributes no setpoint to this read.

        The device may not have reported in yet, or it may have dropped off
        again; either way there is nothing to take, and the cool target stays
        unknown for the re-read at the end of startup to pick up.
        """
        bt.cooler_entity_id = COOLER_ID
        _install_states(
            bt,
            {
                COOLER_ID: _make_cooler_state(
                    {ATTR_TEMPERATURE: 24.0}, state=STATE_UNAVAILABLE
                )
            },
        )

        await _run_startup(bt, restored_target=21.0)

        assert bt.bt_target_cooltemp is None

    @pytest.mark.asyncio
    async def test_setpoint_inside_the_configured_range_is_taken_unchanged(
        self, bt, caplog
    ):
        """A setpoint clearing both bounds and the heating target is adopted as is.

        Nothing about such a value has to be corrected, so it reaches the
        cooling channel exactly as the device reports it and no correction is
        annunciated.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_temp_step = 0.5
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 24.0})})

        caplog.set_level(logging.WARNING)
        await _run_startup(bt, restored_target=21.0)

        assert bt.bt_target_cooltemp == 24.0
        assert bt.bt_target_temp == 21.0
        assert caplog.text == ""

    @pytest.mark.asyncio
    async def test_setpoint_outside_the_configured_range_is_clamped(self, bt, caplog):
        """A cooler reporting below the configured minimum is brought into range.

        Better Thermostat advertises the overlap of the ranges its devices
        advertise, and a cooler that was unreachable while that overlap was
        derived contributed no bounds to it. The heating target is well clear of
        the value, so the clamp is the only correction, and it is annunciated
        because the clamped value is written back to the device.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_min_temp = 18.0
        bt.bt_target_temp_step = 0.5
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 16.0})})

        caplog.set_level(logging.WARNING)
        await _run_startup(bt, restored_target=15.0)

        assert bt.bt_target_cooltemp == 18.0
        assert (
            "reported setpoint 16.0 outside of range while the cool target is "
            "unknown, taking 18.0 as the cool target" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_setpoint_is_ordered_against_the_restored_heating_target(self, bt):
        """The heating target the restore installs is the one that bounds the seed.

        Reading the cooler before the restore would order the value against the
        construction default instead, and the pair Better Thermostat ends up
        holding would have the cooling target below the heating one.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_temp = DEFAULT_TARGET_TEMP
        bt.bt_target_temp_step = 0.5
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 20.0})})

        await _run_startup(bt, restored_target=21.0)

        assert bt.bt_target_cooltemp == 21.5
        assert bt.bt_target_temp == 21.0

    @pytest.mark.asyncio
    async def test_restored_preset_cool_target_is_not_overwritten(self, bt):
        """A preset carries a cooling target the user chose, so it is kept.

        The device is only asked for a target nothing else supplies, and a
        restored preset supplies one.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_temp_step = 0.5
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 24.0})})

        await _run_startup(bt, restored_target=21.0, restored_cool_target=26.0)

        assert bt.bt_target_cooltemp == 26.0

    @pytest.mark.asyncio
    async def test_no_cooler_configured_leaves_the_cool_target_unknown(self, bt):
        """Without a cooling channel there is no device to take a target from.

        Every state lookup answers with a readable setpoint here, so the absent
        cooling channel is the only thing that can leave the target unknown: a
        read that went ahead without one would find a value and store it.
        """
        bt.cooler_entity_id = None
        bt.hass.states.get.return_value = _make_cooler_state({ATTR_TEMPERATURE: 24.0})

        await _run_startup(bt, restored_target=21.0)

        assert bt.bt_target_cooltemp is None
        bt._seed_cool_target.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreadable_step_is_logged_against_this_read(self, bt, caplog):
        """The read names itself when the cooler's step cannot be converted.

        The second startup read, at the cooler's listener registration, resolves
        the step through the same helper. Naming this one after the method it
        runs in is what keeps an entry the two could both have produced
        attributable to one of them.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_temp_step = 0.5
        _install_states(
            bt,
            {
                COOLER_ID: _make_cooler_state(
                    {ATTR_TEMPERATURE: 24.0, "target_temp_step": "unavailable"}
                )
            },
        )

        caplog.set_level(logging.DEBUG)
        await _run_startup(bt, restored_target=21.0)

        assert "Could not convert 'unavailable' to float in startup()" in caplog.text
        assert bt.bt_target_cooltemp == 24.0

    @pytest.mark.asyncio
    async def test_unreadable_setpoint_is_logged_against_this_read(self, bt, caplog):
        """The read names itself when the cooler's setpoint cannot be converted.

        The step and the setpoint are resolved through two separate helpers,
        and this read hands its own name to both of them, so whichever of the
        two attributes a cooler publishes unreadably is reported against the
        read that asked for it.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_temp_step = 0.5
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: "n/a"})})

        caplog.set_level(logging.DEBUG)
        await _run_startup(bt, restored_target=21.0)

        assert "Could not convert 'n/a' to float in startup()" in caplog.text
        assert bt.bt_target_cooltemp is None


# ---------------------------------------------------------------------------
# 6. TRV attribute initialization (_initialize_trvs)
# ---------------------------------------------------------------------------


class TestInitializeTrvCurrentTemperature:
    """Startup must not fabricate a TRV-internal temperature.

    A seeded value would feed SENSOR_FALLBACK as if it were live and
    keep the fail-soft ladder's HOLD rung unreachable, and a falsy-or
    fallback would swallow a real reading of 0.0.
    """

    def _trv_only_bt(self, bt, attrs, unit="°C"):
        bt.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID, calibration=1)}
        bt.hass.config.units.temperature_unit = unit
        bt.hass.states.get.return_value = _make_trv_state(attrs=attrs)
        return bt

    async def _run(self, bt):
        with (
            patch("custom_components.better_thermostat.climate.init", autospec=True),
            patch(
                "custom_components.better_thermostat.climate.initial_tweak",
                autospec=True,
            ),
            patch(
                "custom_components.better_thermostat.climate.control_trv",
                AsyncMock(return_value=True),
            ),
        ):
            await BetterThermostat._initialize_trvs(bt)

    @pytest.mark.asyncio
    async def test_missing_reading_stays_none(self, bt):
        """No reading at startup leaves the field unset."""
        bt = self._trv_only_bt(bt, {"current_temperature": None})
        await self._run(bt)
        assert bt.real_trvs[TRV_ID].current_temperature is None

    @pytest.mark.asyncio
    async def test_zero_reading_is_kept(self, bt):
        """A legitimate 0.0° reading is a reading, not a missing value."""
        bt = self._trv_only_bt(bt, {"current_temperature": 0.0})
        await self._run(bt)
        assert bt.real_trvs[TRV_ID].current_temperature == 0.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker_temp", [126.5, 127.0])
    async def test_implausible_startup_reading_is_dropped(self, bt, marker_temp):
        """AVM marker values must not seed the cache for the first control cycle."""
        bt = self._trv_only_bt(bt, {"current_temperature": marker_temp})
        await self._run(bt)
        assert bt.real_trvs[TRV_ID].current_temperature is None


# ---------------------------------------------------------------------------
# 7. _restore_state
# ---------------------------------------------------------------------------


class TestRestoreState:
    """Tests for _restore_state."""

    @pytest.mark.asyncio
    async def test_restores_ema_and_slope(self, bt):
        """Test Restores ema and slope."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {
            "external_temp_ema": "20.5",
            "temp_slope_K_min": "0.0012",
            ATTR_TEMPERATURE: 21.0,
        }
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.preset_mgr.temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.external_temp_ema == 20.5
        assert bt.cur_temp_filtered == 20.5
        assert bt.temp_slope == 0.0012

    @pytest.mark.asyncio
    async def test_target_clamped_to_min(self, bt):
        """Test Target clamped to min."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 2.0}  # below min
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.bt_min_temp = 5.0
        bt.bt_max_temp = 30.0
        bt.preset_mgr.temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.bt_target_temp is not None

    @pytest.mark.asyncio
    async def test_target_clamped_to_max(self, bt):
        """Test Target clamped to max."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 35.0}  # above max
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.bt_min_temp = 5.0
        bt.bt_max_temp = 30.0
        bt.preset_mgr.temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.bt_target_temp is not None

    @pytest.mark.asyncio
    async def test_restores_preset_mode(self, bt):
        """Test Restores preset mode."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 22.0, "preset_mode": "comfort"}
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.preset_mgr.temperatures = {"comfort": 22.0, "eco": 18.0}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.preset_mgr.mode == "comfort"

    @pytest.mark.asyncio
    async def test_restores_preset_cool_temperature_mapping(self, bt):
        """Restore user-customized cooling preset temperatures from state."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {
            ATTR_TEMPERATURE: 22.0,
            ATTR_STATE_PRESET_COOL_TEMPERATURES: json.dumps(
                {"comfort": 25.5, "eco": "26.0", "unknown": 10.0}
            ),
        }
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.preset_mgr.temperatures = {"comfort": 22.0, "eco": 18.0}
        bt._preset_cool_temperatures = {"comfort": 24.0, "eco": 27.0}

        await BetterThermostat._restore_state(bt, [_make_trv_state()])

        assert bt._preset_cool_temperatures == {"comfort": 25.5, "eco": 26.0}

    @pytest.mark.asyncio
    async def test_restored_preset_applies_persisted_cool_target(self, bt):
        """A restored preset applies its persisted cool target, not the default."""
        bt.cooler_entity_id = COOLER_ID
        bt._preset_cool_temperatures = {"none": 24.0, "comfort": 24.0, "eco": 27.0}
        bt._preset_cool_temperature = None
        old = MagicMock()
        old.state = "heat"
        old.attributes = {
            ATTR_TEMPERATURE: 22.0,
            "preset_mode": "comfort",
            ATTR_STATE_PRESET_COOL_TEMPERATURES: json.dumps({"comfort": 25.5}),
        }
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.preset_mgr.temperatures = {"comfort": 22.0, "eco": 18.0}

        await BetterThermostat._restore_state(bt, [_make_trv_state()])

        assert bt._preset_cool_temperatures["comfort"] == 25.5
        assert bt.bt_target_cooltemp == 25.5

    def _cooling_bt(self, bt, minimum, maximum):
        """Configure *bt* with a cooling channel and a real ordering method."""
        bt.cooler_entity_id = COOLER_ID
        bt.bt_min_temp = minimum
        bt.bt_max_temp = maximum
        bt.bt_target_temp_step = 0.5
        bt.hvac_mode = HVACMode.OFF
        bt._preset_cool_temperature = None
        bt._enforce_cool_above_heat = lambda **kwargs: (
            BetterThermostat._enforce_cool_above_heat(bt, **kwargs)
        )
        return bt

    @pytest.mark.asyncio
    async def test_restored_preset_pair_below_the_minimum_is_bounded_and_ordered(
        self, bt
    ):
        """A stored preset pair under the minimum is bounded, then ordered.

        A preset stored while the cooler was unavailable carries the pair the
        range in force then allowed. Re-injecting it verbatim publishes two
        targets the configured range does not contain, and the ordering is not
        revisited because Better Thermostat comes back OFF.
        """
        bt = self._cooling_bt(bt, 20.0, 30.0)
        bt._preset_cool_temperatures = {"none": 24.0, "comfort": 10.0, "eco": 27.0}
        bt.preset_mgr.temperatures = {"comfort": 9.0, "eco": 18.0}
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 9.0, "preset_mode": "comfort"}
        bt.async_get_last_state = AsyncMock(return_value=old)

        await BetterThermostat._restore_state(bt, [_make_trv_state()])

        assert bt.bt_target_temp == 20.0
        assert bt.bt_target_cooltemp == 20.5
        assert bt.bt_min_temp <= bt.bt_target_temp <= bt.bt_max_temp
        assert bt.bt_min_temp <= bt.bt_target_cooltemp <= bt.bt_max_temp
        assert bt.bt_target_cooltemp > bt.bt_target_temp

    @pytest.mark.asyncio
    async def test_restored_preset_cool_target_above_the_maximum_is_bounded(self, bt):
        """A stored preset cooling target over the maximum comes back bounded.

        The ordering leaves it alone because it already clears the heating
        target, so the range bound is the only thing that holds it.
        """
        bt = self._cooling_bt(bt, 16.0, 26.0)
        bt._preset_cool_temperatures = {"none": 24.0, "comfort": 35.0, "eco": 27.0}
        bt.preset_mgr.temperatures = {"comfort": 20.0, "eco": 18.0}
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 20.0, "preset_mode": "comfort"}
        bt.async_get_last_state = AsyncMock(return_value=old)

        await BetterThermostat._restore_state(bt, [_make_trv_state()])

        assert bt.bt_target_temp == 20.0
        assert bt.bt_target_cooltemp == 26.0
        assert bt.bt_min_temp <= bt.bt_target_cooltemp <= bt.bt_max_temp
        assert bt.bt_target_cooltemp > bt.bt_target_temp

    @pytest.mark.asyncio
    async def test_restored_preset_heating_target_above_the_maximum_is_bounded(
        self, bt
    ):
        """A stored preset heating target over the maximum comes back bounded."""
        bt = self._cooling_bt(bt, 16.0, 26.0)
        bt._preset_cool_temperatures = {"none": 24.0, "comfort": 25.0, "eco": 27.0}
        bt.preset_mgr.temperatures = {"comfort": 31.0, "eco": 18.0}
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 31.0, "preset_mode": "comfort"}
        bt.async_get_last_state = AsyncMock(return_value=old)

        await BetterThermostat._restore_state(bt, [_make_trv_state()])

        assert bt.bt_target_temp == 26.0
        assert bt.bt_min_temp <= bt.bt_target_temp <= bt.bt_max_temp

    @pytest.mark.asyncio
    async def test_restored_preset_pair_is_not_ordered_without_a_cooler(self, bt):
        """Without a cooling channel there is no pair to order."""
        bt = self._cooling_bt(bt, 16.0, 26.0)
        bt.cooler_entity_id = None
        bt.bt_target_cooltemp = 18.0
        bt._preset_cool_temperatures = {"none": 24.0, "comfort": 25.0, "eco": 27.0}
        bt.preset_mgr.temperatures = {"comfort": 22.0, "eco": 18.0}
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 22.0, "preset_mode": "comfort"}
        bt.async_get_last_state = AsyncMock(return_value=old)

        await BetterThermostat._restore_state(bt, [_make_trv_state()])

        assert bt.bt_target_temp == 22.0
        assert bt.bt_target_cooltemp == 18.0

    @pytest.mark.asyncio
    async def test_restores_heating_power_clamped(self, bt):
        """Test Restores heating power clamped."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {
            ATTR_TEMPERATURE: 21.0,
            ATTR_STATE_HEATING_POWER: "999.0",  # way above max
        }
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.preset_mgr.temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.heating_power == MAX_HEATING_POWER

    @pytest.mark.asyncio
    async def test_no_old_state_uses_trv_defaults(self, bt):
        """Test No old state uses trv defaults."""
        bt.async_get_last_state = AsyncMock(return_value=None)
        bt.bt_target_temp = None

        states = [_make_trv_state(attrs={ATTR_TEMPERATURE: 20.0})]
        await BetterThermostat._restore_state(bt, states)

        # Should have set bt_target_temp from TRV states
        assert bt.bt_target_temp is not None

    @pytest.mark.asyncio
    async def test_call_for_heat_not_restored(self, bt):
        """call_for_heat is an observation, not UI state.

        A stored False is ignored and the safe default (True) keeps
        ruling until the first live prediction.
        """
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 21.0, ATTR_STATE_CALL_FOR_HEAT: False}
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.preset_mgr.temperatures = {}
        bt.call_for_heat = True

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.call_for_heat is True

    @pytest.mark.asyncio
    async def test_restores_heat_loss_clamped(self, bt):
        """An out-of-range restored heat-loss rate is clamped to the max."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 21.0, ATTR_STATE_HEAT_LOSS: "5.0"}
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.preset_mgr.temperatures = {}

        states = [_make_trv_state()]
        await BetterThermostat._restore_state(bt, states)

        assert bt.heat_loss_rate == MAX_HEAT_LOSS

    @pytest.mark.asyncio
    async def test_old_state_without_target_falls_back_to_trv_mean(self, bt):
        """An old state lacking a target temperature falls back to the TRV mean."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {}  # no ATTR_TEMPERATURE
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.preset_mgr.temperatures = {}

        states = [
            _make_trv_state(attrs={ATTR_TEMPERATURE: 20.0}),
            _make_trv_state(attrs={ATTR_TEMPERATURE: 24.0}),
        ]
        await BetterThermostat._restore_state(bt, states)

        assert bt.bt_target_temp == 22.0

    @pytest.mark.asyncio
    async def test_restored_mode_is_parsed_to_enum(self, bt):
        """A valid restored state string becomes an HVACMode enum."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 21.0}
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.bt_hvac_mode = None
        bt.preset_mgr.temperatures = {}

        await BetterThermostat._restore_state(bt, [_make_trv_state()])

        assert bt.bt_hvac_mode is HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_unrecognised_mode_left_unset(self, bt):
        """An unrecognised restored state is not stored (stays None for validation)."""
        old = MagicMock()
        old.state = "not_a_mode"
        old.attributes = {ATTR_TEMPERATURE: 21.0}
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.bt_hvac_mode = None
        bt.preset_mgr.temperatures = {}

        await BetterThermostat._restore_state(bt, [_make_trv_state()])

        assert bt.bt_hvac_mode is None


# ---------------------------------------------------------------------------
# 8. Initial TRV sync (_finalize_startup / _startup_control_trvs)
# ---------------------------------------------------------------------------


_CLIMATE = "custom_components.better_thermostat.climate"


class TestStartupControlSync:
    """The initial device sync must run after the lifecycle gate opens."""

    @pytest.mark.asyncio
    async def test_finalize_startup_flips_lifecycle_before_initial_sync(self, bt):
        """The initial sync runs only after the lifecycle flip.

        While startup_running is True, decide() addresses no TRVs — a
        sync before the flip would silently write nothing.
        """
        bt.is_removed = False
        bt.all_entities = []
        bt.all_trvs = None
        gate_states = []

        async def record_sync():
            gate_states.append(bt.kernel_state.lifecycle.startup_running)
            bt.is_removed = True

        bt._startup_control_trvs = record_sync
        with (
            patch(f"{_CLIMATE}.await_critical_entities", AsyncMock()),
            patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
            patch(f"{_CLIMATE}.asyncio.sleep", AsyncMock()),
        ):
            await BetterThermostat._finalize_startup(bt)

        assert gate_states == [False]

    @pytest.mark.asyncio
    async def test_startup_control_trvs_controls_each_trv(self, bt):
        """Every configured TRV receives one initial control call."""
        bt.real_trvs = {TRV_ID: {}, TRV_ID_2: {}}
        with patch(f"{_CLIMATE}.control_trv", AsyncMock(return_value=True)) as ctl:
            await BetterThermostat._startup_control_trvs(bt)

        assert [call.args[1] for call in ctl.call_args_list] == [TRV_ID, TRV_ID_2]

    @pytest.mark.asyncio
    async def test_startup_control_trvs_computes_one_cycle_for_all(self, bt):
        """All TRVs are synced from one observation and decision."""
        bt.real_trvs = {TRV_ID: {}, TRV_ID_2: {}}
        cycle = object()
        with (
            patch(f"{_CLIMATE}.compute_control_cycle", return_value=cycle) as compute,
            patch(f"{_CLIMATE}.control_trv", AsyncMock(return_value=True)) as ctl,
        ):
            await BetterThermostat._startup_control_trvs(bt)

        compute.assert_called_once()
        assert [call.kwargs.get("cycle") for call in ctl.call_args_list] == [
            cycle,
            cycle,
        ]

    @pytest.mark.asyncio
    async def test_startup_control_trvs_survives_a_failing_trv(self, bt):
        """An error on one TRV must not stop the sync of the others."""
        bt.real_trvs = {TRV_ID: {}, TRV_ID_2: {}}
        ctl = AsyncMock(side_effect=[RuntimeError("boom"), True])
        with patch(f"{_CLIMATE}.control_trv", ctl):
            await BetterThermostat._startup_control_trvs(bt)

        assert ctl.call_count == 2


async def _run_finalize_startup(bt):
    """Run _finalize_startup with its external hooks patched out."""
    bt.is_removed = False
    bt.all_trvs = None
    bt.entity_ids = [TRV_ID]
    bt._async_unsub_state_changed = None
    # Background jobs are handed to a mocked task factory that never awaits
    # them, so they hand out plain values instead of orphaned coroutines.
    bt._post_grace_recheck = MagicMock()
    bt._external_temperature_keepalive = MagicMock()
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    with (
        patch(f"{_CLIMATE}.await_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.await_optional_sensors", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.async_track_state_change_event", MagicMock()),
        patch(f"{_CLIMATE}.async_track_time_interval", MagicMock()),
        patch(f"{_CLIMATE}.async_track_time_change", MagicMock()),
        patch(f"{_CLIMATE}.asyncio.sleep", AsyncMock()),
    ):
        await BetterThermostat._finalize_startup(bt)


class TestCoolerTargetReadAtListenerRegistration:
    """The cool target is re-read once the cooler subscription is live.

    A cooler that joins Home Assistant after the startup seed and then never
    changes state again produces no event, so the registration of its listener
    is the last point at which its setpoint can still be read.
    An unknown cool target holds the cooler off on every control cycle.
    """

    @pytest.mark.asyncio
    async def test_cooler_online_by_now_seeds_the_cool_target(self, bt):
        """The state the startup seed could not see is read here."""
        bt.cooler_entity_id = COOLER_ID
        bt.bt_hvac_mode = HVACMode.HEAT_COOL
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 24.0})})

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 24.0
        bt._seed_cool_target.assert_called_once()
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_seed_while_off_requests_no_control_cycle(self, bt):
        """An off thermostat still needs the field, but no cycle to use it.

        A cycle would command the cooler off whatever the target says, and
        switching the thermostat back on requests a cycle of its own.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_hvac_mode = HVACMode.OFF
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 24.0})})

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 24.0
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_setpoint_outside_the_range_is_clamped_and_reported(self, bt, caplog):
        """A cooler absent from the range derivation can report outside it.

        The temperature range comes from the devices that were reachable, and an
        offline cooler contributed no bounds of its own, so the setpoint it
        reports once it joins can sit above the advertised maximum. Storing it
        unclamped would put the published target outside the range and write a
        value the cooler may reject.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_max_temp = 30.0
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 31.0})})

        caplog.set_level(logging.WARNING)
        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 30.0
        assert "reported setpoint 31.0 outside of range" in caplog.text

    @pytest.mark.asyncio
    async def test_setpoint_colliding_with_the_heat_target_is_lifted(self, bt):
        """The observed value yields, the restored heating target stays.

        A setpoint read off the device carries no user intent, so a collision
        between the two targets of the published range is resolved on the
        cooling side.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_temp = 21.0
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 19.0})})

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 21.5
        assert bt.bt_target_temp == 21.0

    @pytest.mark.asyncio
    async def test_cooler_reporting_off_seeds_the_cool_target(self, bt):
        """An air conditioner at rest reports off and still carries a setpoint.

        Off is where an idle cooler sits and the only state a cooler that never
        switches on will ever publish, so it is the state this read exists for.
        The read asks whether a setpoint can be obtained, not whether the device
        is currently cooling.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_hvac_mode = HVACMode.HEAT_COOL
        _install_states(
            bt,
            {
                COOLER_ID: _make_cooler_state(
                    {ATTR_TEMPERATURE: 24.0}, state=HVACMode.OFF
                )
            },
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 24.0
        bt._seed_cool_target.assert_called_once()
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_unavailable_cooler_leaves_the_cool_target_unknown(self, bt):
        """Attributes an unavailable cooler carries are not a reading.

        The state tells whether the device can be reached, and attributes can
        survive alongside an unavailable one. Seeding from them would store a
        value no reachable device stands behind, while the listener registered
        just above delivers the real one as soon as the cooler returns.
        """
        bt.cooler_entity_id = COOLER_ID
        _install_states(
            bt,
            {
                COOLER_ID: _make_cooler_state(
                    {ATTR_TEMPERATURE: 24.0}, state=STATE_UNAVAILABLE
                )
            },
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp is None
        bt._seed_cool_target.assert_not_called()
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_unknown_cooler_leaves_the_cool_target_unknown(self, bt):
        """A cooler that has joined without reporting yet has nothing to offer.

        An entity registered but not yet updated holds the unknown state, and
        an attribute standing next to it is no value the device has confirmed.
        """
        bt.cooler_entity_id = COOLER_ID
        _install_states(
            bt,
            {
                COOLER_ID: _make_cooler_state(
                    {ATTR_TEMPERATURE: 24.0}, state=STATE_UNKNOWN
                )
            },
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp is None
        bt._seed_cool_target.assert_not_called()
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_cooler_without_a_state_leaves_the_cool_target_unknown(self, bt):
        """An entity that does not exist yet returns no state at all."""
        bt.cooler_entity_id = COOLER_ID
        _install_states(bt, {})

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp is None
        bt._seed_cool_target.assert_not_called()
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_cooler_publishing_no_setpoint_requests_no_cycle(self, bt):
        """An available cooler may still carry no setpoint in its attributes."""
        bt.cooler_entity_id = COOLER_ID
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: None})})

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp is None
        bt._seed_cool_target.assert_not_called()
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_known_cool_target_is_not_re_read(self, bt):
        """A target the startup seed or a restore already filled is not re-read.

        The device is asked again only for a target that is still unknown, so a
        cooler that has moved on since the startup seed cannot overwrite a value
        Better Thermostat already holds.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_cooltemp = 26.0
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 24.0})})

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 26.0
        bt._seed_cool_target.assert_not_called()
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_fahrenheit_cooler_is_read_with_its_own_converted_step(self, bt):
        """The device's step reaches the adoption gate as a Celsius delta.

        A Fahrenheit cooler publishes its step as a °F delta, so the 2 °F grid
        it advertises is 1.11 °C wide, and the setpoint it reports is converted
        along with it.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_temp_step = 0.5
        bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        _install_states(
            bt,
            {
                COOLER_ID: _make_cooler_state(
                    {ATTR_TEMPERATURE: 75.0, "target_temp_step": 2.0}
                )
            },
        )
        spy = MagicMock(side_effect=resolve_inbound_setpoint)

        with patch(f"{_CLIMATE}.resolve_inbound_setpoint", spy):
            await _run_finalize_startup(bt)

        assert spy.call_args.kwargs["step"] == 1.1111
        assert bt.bt_target_cooltemp == 23.89

    @pytest.mark.asyncio
    async def test_unreadable_step_is_logged_against_this_read(self, bt, caplog):
        """The read names itself when the cooler's step cannot be converted.

        Three reads resolve a cooler's step through the same helper: the seed
        under startup(), this one, and the event handler. The caller each names
        is what tells a reader which of them produced the entry, so this one
        names the method it runs in rather than the startup around it.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_temp_step = 0.5
        _install_states(
            bt,
            {
                COOLER_ID: _make_cooler_state(
                    {ATTR_TEMPERATURE: 24.0, "target_temp_step": "unavailable"}
                )
            },
        )

        caplog.set_level(logging.DEBUG)
        await _run_finalize_startup(bt)

        assert (
            "Could not convert 'unavailable' to float in _finalize_startup()"
            in caplog.text
        )
        assert bt.bt_target_cooltemp == 24.0

    @pytest.mark.asyncio
    async def test_unreadable_setpoint_is_logged_against_this_read(self, bt, caplog):
        """The read names itself when the cooler's setpoint cannot be converted.

        The step and the setpoint are resolved through two separate helpers,
        and this read hands its own name to both of them, so a setpoint this
        read cannot convert is not reported against the read under startup()
        that stumbles over the same attribute.
        """
        bt.cooler_entity_id = COOLER_ID
        bt.bt_target_temp_step = 0.5
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: "n/a"})})

        caplog.set_level(logging.DEBUG)
        await _run_finalize_startup(bt)

        assert "Could not convert 'n/a' to float in _finalize_startup()" in caplog.text
        assert bt.bt_target_cooltemp is None


class TestFinalizeStartupBatteryScan:
    """The battery scan covers every configured device.

    It reads ``all_entities``, so a device registered after the scan is
    never asked for a battery entity at all.
    """

    @staticmethod
    async def _scan(bt):
        """Run _finalize_startup and return the entity IDs the scan visited."""
        scanned: list[str] = []

        async def spy(_self, entity_id, _visited=None):
            scanned.append(entity_id)
            return f"sensor.{entity_id.split('.')[-1]}_battery"

        with patch(f"{_CLIMATE}.find_battery_entity", new=spy):
            await _run_finalize_startup(bt)
        return scanned

    @pytest.mark.asyncio
    async def test_scan_reaches_the_cooler(self, bt):
        """A configured cooler is asked for its battery entity."""
        bt.cooler_entity_id = COOLER_ID
        bt.devices_states = {}
        _install_states(bt, {COOLER_ID: _make_cooler_state({ATTR_TEMPERATURE: 24.0})})

        scanned = await self._scan(bt)

        assert COOLER_ID in scanned
        assert bt.devices_states[COOLER_ID]["battery_id"] == "sensor.cooler_battery"

    @pytest.mark.asyncio
    async def test_scan_reaches_the_outdoor_sensor(self, bt):
        """A configured outdoor sensor is asked for its battery entity."""
        bt.cooler_entity_id = None
        bt.outdoor_sensor = OUTDOOR_ID
        bt.devices_states = {}

        scanned = await self._scan(bt)

        assert OUTDOOR_ID in scanned
        assert (
            bt.devices_states[OUTDOOR_ID]["battery_id"] == "sensor.outdoor_temp_battery"
        )

    @pytest.mark.asyncio
    async def test_unconfigured_devices_are_not_scanned(self, bt):
        """Nothing is registered for a cooler or outdoor sensor that is absent."""
        bt.cooler_entity_id = None
        bt.outdoor_sensor = None
        bt.devices_states = {}

        scanned = await self._scan(bt)

        assert scanned == []
        assert bt.all_entities == []


# ---------------------------------------------------------------------------
# 9. _validate_hvac_mode
# ---------------------------------------------------------------------------


class TestValidateHvacMode:
    """Tests for _validate_hvac_mode."""

    def test_already_set_stays(self, bt):
        """Test Already set stays."""
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state(state="heat")]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.bt_hvac_mode == HVACMode.HEAT

    def test_none_mode_all_off_sets_off(self, bt):
        """Test None mode all off sets off."""
        bt.bt_hvac_mode = None
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state(state="off")]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.bt_hvac_mode == HVACMode.OFF

    def test_none_mode_most_heat_sets_heat(self, bt):
        """Test None mode most heat sets heat."""
        bt.bt_hvac_mode = None
        bt.humidity_sensor_entity_id = None
        states = [
            _make_trv_state(TRV_ID, state="heat"),
            _make_trv_state(TRV_ID_2, state="heat"),
        ]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.bt_hvac_mode == HVACMode.HEAT

    def test_invalid_mode_forced_to_heat(self, bt):
        """Test Invalid mode forced to heat."""
        bt.bt_hvac_mode = "auto"  # not in allowed set
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state(state="heat")]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.bt_hvac_mode == HVACMode.HEAT

    def test_last_main_hvac_mode_default(self, bt):
        """Test Last main hvac mode default."""
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.last_main_hvac_mode = None
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state()]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.last_main_hvac_mode == HVACMode.HEAT

    def test_last_window_state_set(self, bt):
        """Test Last window state set."""
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.window_open = True
        bt.contact_open = True
        bt.humidity_sensor_entity_id = None
        states = [_make_trv_state()]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt.last_window_state is True

    def test_humidity_sensor_re_read(self, bt):
        """Test Humidity sensor re read."""
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.humidity_sensor_entity_id = HUMIDITY_ID
        bt.hass.states.get.return_value = State(HUMIDITY_ID, "60.0")
        states = [_make_trv_state()]
        BetterThermostat._validate_hvac_mode(bt, states)
        # humidity should be re-read
        assert bt._current_humidity is not None

    def test_humidity_sensor_none_sets_zero(self, bt):
        """Test Humidity sensor none sets zero."""
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.humidity_sensor_entity_id = HUMIDITY_ID
        bt.hass.states.get.return_value = None
        states = [_make_trv_state()]
        BetterThermostat._validate_hvac_mode(bt, states)
        assert bt._current_humidity == 0
