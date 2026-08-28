"""Tests for the startup() submethods extracted from BetterThermostat.startup().

Covers: _check_entities_ready, _collect_trv_states, _resolve_temperature_range,
_initialize_sensors, _seed_cool_target_from_cooler, _finalize_startup,
_restore_state, _validate_hvac_mode.
"""

import asyncio
from datetime import timedelta
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import PRESET_NONE, HVACMode
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.climate import (
    DEFAULT_FALLBACK_TEMPERATURE,
    BetterThermostat,
)
from custom_components.better_thermostat.events.temperature import (
    PLATEAU_ACCEPT_WINDOW,
    trigger_temperature_change,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_CALL_FOR_HEAT,
    ATTR_STATE_HEAT_LOSS,
    ATTR_STATE_HEATING_POWER,
    ATTR_STATE_PRESET_COOL_TEMPERATURES,
    CONF_HOMEMATICIP,
    DEFAULT_TARGET_TEMP,
    MAX_HEAT_LOSS,
    MAX_HEATING_POWER,
)
from custom_components.better_thermostat.utils.helpers import device_setpoint_step

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
    mock.hass = MagicMock()
    mock.device_name = "Test BT"
    mock.sensor_entity_id = SENSOR_ID
    mock.real_trvs = {TRV_ID: {"calibration": 1}}
    mock.cooler_entity_id = None
    mock.humidity_sensor_entity_id = None
    mock.window_id = None
    mock.door_id = None
    mock.all_entities = []
    mock.unavailable_sensors = []
    mock.degraded_mode = False
    mock.bt_min_temp = 5.0
    mock.bt_max_temp = 30.0
    mock.bt_target_temp = 21.0
    mock.bt_target_temp_min = None
    mock.bt_target_temp_max = None
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
    mock._bound_target_to_range = lambda value: BetterThermostat._bound_target_to_range(
        mock, value
    )
    return mock


@pytest.fixture
def plateau_bt(bt, hass):
    """A thermostat whose plateau timer is scheduled on a real event loop.

    The plateau path only exists once a reading has been accepted, so the
    baseline reading is fed through the production handler rather than
    assigned, and the TRV write the timer performs is observable on the
    quirk the handler delegates to.
    """
    bt.hass = hass
    bt.startup_running = False
    bt._control_task = None
    bt._window_task = None
    bt._door_task = None
    bt.control_queue_task = None
    bt.in_maintenance = False
    bt._control_needed_after_maintenance = False
    bt.last_external_sensor_change = None
    bt.prev_stable_temp = None
    bt.last_change_direction = 0
    bt.accum_delta = 0.0
    bt.accum_dir = 0
    bt.pending_temp = None
    bt.pending_since = None
    bt.plateau_timer_cancel = None
    bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: False}}]
    trv = MagicMock()
    trv.model_quirks = MagicMock()
    trv.model_quirks.maybe_set_external_temperature = AsyncMock()
    bt.real_trvs = {TRV_ID: trv}
    return bt


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

        with (
            # Each pass of the wait branch reports on the critical entities;
            # against a mocked thermostat that check has nothing to read, and
            # this test is about the bail-out, not about the reporting.
            patch(
                "custom_components.better_thermostat.climate.check_critical_entities",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.climate.asyncio.sleep",
                side_effect=fake_sleep,
            ) as mock_sleep,
        ):
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
        """Unload clears startup_running so the loop condition terminates."""
        bt._control_task = None
        bt._window_task = None
        bt._door_task = None
        bt.plateau_timer_cancel = None
        bt.startup_running = True

        await BetterThermostat.async_will_remove_from_hass(bt)

        assert bt.startup_running is False


# ---------------------------------------------------------------------------
# 0b. what an unload has to withdraw from hass
# ---------------------------------------------------------------------------


# Half the significance threshold, so this reading reaches the TRVs through
# the plateau timer and through no other accept path.
SUB_THRESHOLD_TEMP = 20.05


async def _feed_sensor_reading(entity, temperature):
    """Deliver one external temperature reading to the event handler."""
    event = MagicMock()
    event.data = {"new_state": State(SENSOR_ID, str(temperature))}
    await trigger_temperature_change(entity, event)


def _external_temperature_writes(entity):
    """Return the TRV write an accepted reading performs."""
    return entity.real_trvs[TRV_ID].model_quirks.maybe_set_external_temperature


async def _arm_plateau_timer(entity):
    """Establish a baseline reading, then leave a plateau timer pending."""
    await _feed_sensor_reading(entity, 20.0)
    _external_temperature_writes(entity).assert_awaited_once()
    _external_temperature_writes(entity).reset_mock()

    # The accepted reading is a minute old, so the debounce interval the
    # timer re-checks when it fires has long passed.
    entity.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)

    await _feed_sensor_reading(entity, SUB_THRESHOLD_TEMP)
    assert entity.plateau_timer_cancel is not None
    assert entity.pending_temp == SUB_THRESHOLD_TEMP
    _external_temperature_writes(entity).assert_not_awaited()


def _pass_the_plateau_window(hass):
    """Advance the loop past the plateau window so a pending timer fires."""
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=PLATEAU_ACCEPT_WINDOW + 5)
    )


class TestPlateauTimerOnRemoval:
    """A pending plateau timer must not outlive the entity that armed it.

    The timer is scheduled on hass, so nothing about the unload stops it on
    its own: it fires against the torn-down instance and writes to devices
    the entity no longer drives.
    """

    @pytest.mark.asyncio
    async def test_armed_timer_fires_while_the_entity_lives(self, hass, plateau_bt):
        """The window elapsing accepts the reading and writes it to the TRV."""
        await _arm_plateau_timer(plateau_bt)

        _pass_the_plateau_window(hass)
        await hass.async_block_till_done()

        _external_temperature_writes(plateau_bt).assert_awaited_once_with(
            plateau_bt, TRV_ID, SUB_THRESHOLD_TEMP
        )

    @pytest.mark.asyncio
    async def test_armed_timer_writes_nothing_after_removal(self, hass, plateau_bt):
        """Removal withdraws the timer, so the window passes without a write."""
        await _arm_plateau_timer(plateau_bt)

        await BetterThermostat.async_will_remove_from_hass(plateau_bt)

        _pass_the_plateau_window(hass)
        await hass.async_block_till_done()

        _external_temperature_writes(plateau_bt).assert_not_awaited()
        assert plateau_bt.plateau_timer_cancel is None


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
        assert bt.window_open is True
        assert WINDOW_ID in bt.all_entities

    def test_window_none_defaults_closed(self, bt):
        """Test Window none defaults closed."""
        bt.window_id = None
        sensor = _make_sensor_state("20.0")
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.window_open is False

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
        assert bt.door_open is True
        assert DOOR_ID in bt.all_entities

    def test_door_none_defaults_closed(self, bt):
        """Test Door none defaults closed."""
        bt.door_id = None
        sensor = _make_sensor_state("20.0")
        BetterThermostat._initialize_sensors(bt, sensor)
        assert bt.door_open is False

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
        assert bt.door_open is False

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


def _make_startup_bt():
    """Build a BetterThermostat mock for a startup() run with a cooler."""
    mock = MagicMock(spec=BetterThermostat)
    mock.hass = MagicMock()
    mock.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    mock.device_name = "Test BT"
    mock.version = "1.0.0"
    mock.is_removed = False
    mock.startup_running = True
    mock.sensor_entity_id = SENSOR_ID
    mock.cooler_entity_id = COOLER_ID
    # A spec'd mock carries no instance attributes, and the cooler of this
    # case is a device of its own, so the set of controlled thermostats is
    # stated explicitly and does not contain it.
    mock.real_trvs = {}
    mock.bt_target_cooltemp = None
    # The heating target carries its construction default until the restore
    # step replaces it, which is what makes the ordering of the two steps
    # observable.
    mock.bt_target_temp = DEFAULT_TARGET_TEMP
    mock.bt_target_temp_step = 0.5
    mock.bt_min_temp = 5.0
    mock.bt_max_temp = 30.0
    mock.hvac_mode = HVACMode.HEAT_COOL
    mock.bt_hvac_mode = HVACMode.HEAT_COOL
    mock._check_entities_ready.return_value = True
    mock._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(mock, **kwargs)
    )
    mock._seed_cool_target = lambda setpoint, entity_id: (
        BetterThermostat._seed_cool_target(mock, setpoint, entity_id)
    )
    mock._seed_cool_target_from_cooler = lambda log_source: (
        BetterThermostat._seed_cool_target_from_cooler(mock, log_source)
    )
    mock._bound_target_to_range = lambda value: BetterThermostat._bound_target_to_range(
        mock, value
    )
    return mock


async def _run_startup(bt, cooler_state, restored_target, restored_cool_target=None):
    """Run startup() with a restore step that installs the restored targets."""

    def _states_get(entity_id):
        if entity_id == SENSOR_ID:
            return _make_sensor_state("20.0")
        # Every other lookup answers with the cooler state, including the
        # ``None`` a run without a configured cooler looks up: the seed then has
        # a perfectly readable setpoint in front of it, so the missing
        # configuration is the only thing left that can turn it down.
        return cooler_state

    bt.hass.states.get.side_effect = _states_get

    async def _restore(_states):
        bt.bt_target_temp = restored_target
        if restored_cool_target is not None:
            bt.bt_target_cooltemp = restored_cool_target

    bt._restore_state.side_effect = _restore

    climate = "custom_components.better_thermostat.climate"
    with patch(f"{climate}.check_and_update_degraded_mode", AsyncMock()):
        await asyncio.wait_for(BetterThermostat.startup(bt), timeout=1)


class TestStartupCoolTargetSeed:
    """startup() takes a cooling target off the cooler once restore is done."""

    @pytest.mark.asyncio
    async def test_single_setpoint_cooler_seeds_the_cool_target(self):
        """A cooler exposing ``temperature`` fills a cool target nothing else set."""
        bt = _make_startup_bt()

        await _run_startup(bt, State(COOLER_ID, "cool", {"temperature": 24.0}), 21.0)

        assert bt.bt_target_cooltemp == 24.0

    @pytest.mark.asyncio
    async def test_range_only_cooler_seeds_from_target_temp_high(self):
        """A range-only cooler carries its setpoint in ``target_temp_high``."""
        bt = _make_startup_bt()
        cooler = State(
            COOLER_ID,
            "cool",
            {"temperature": None, "target_temp_low": 19.0, "target_temp_high": 26.0},
        )

        await _run_startup(bt, cooler, 21.0)

        assert bt.bt_target_cooltemp == 26.0

    @pytest.mark.asyncio
    async def test_unavailable_cooler_leaves_the_cool_target_unknown(self):
        """An unavailable cooler contributes no setpoint to this read.

        An entity that lost contact with its device keeps the attributes it last
        published, so the setpoint in front of this read is perfectly readable
        and the state string is the only thing that rejects it. The device may
        not have reported in yet, or it may have dropped off again; either way
        there is nothing to take, and the cool target stays unknown for the
        re-read at the end of startup to pick up.
        """
        bt = _make_startup_bt()

        await _run_startup(
            bt, State(COOLER_ID, STATE_UNAVAILABLE, {"temperature": 24.0}), 21.0
        )

        assert bt.bt_target_cooltemp is None

    @pytest.mark.asyncio
    async def test_setpoint_inside_the_configured_range_is_taken_unchanged(
        self, caplog
    ):
        """A setpoint that clears both bounds and the heating target is adopted as is.

        Nothing about such a value has to be corrected, so it reaches the
        cooling channel exactly as the device reports it and no correction is
        annunciated.
        """
        bt = _make_startup_bt()

        with caplog.at_level(logging.WARNING):
            await _run_startup(
                bt, State(COOLER_ID, "cool", {"temperature": 24.0}), 21.0
            )

        assert bt.bt_target_cooltemp == 24.0
        assert bt.bt_target_temp == 21.0
        assert "outside of range" not in caplog.text
        assert "cooling target" not in caplog.text

    @pytest.mark.asyncio
    async def test_setpoint_outside_the_configured_range_is_clamped(self, caplog):
        """A cooler reporting below the configured minimum is brought into range.

        The heating target is well clear of the value, so the clamp is the only
        correction, and it is annunciated because the clamped value is written
        back to the device.
        """
        bt = _make_startup_bt()
        bt.bt_min_temp = 18.0

        with caplog.at_level(logging.WARNING):
            await _run_startup(
                bt, State(COOLER_ID, "cool", {"temperature": 16.0}), 15.0
            )

        assert bt.bt_target_cooltemp == 18.0
        assert (
            "reported setpoint 16.0 outside of range while the cool target is "
            "unknown, taking 18.0 as the cool target" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_setpoint_is_ordered_against_the_restored_heating_target(self):
        """The heating target the restore step installs is the one that bounds the seed.

        Reading the cooler before the restore would order the value against the
        construction default instead, and the pair Better Thermostat ends up
        holding would have the cooling target below the heating one.
        """
        bt = _make_startup_bt()

        await _run_startup(bt, State(COOLER_ID, "cool", {"temperature": 20.0}), 21.0)

        assert bt.bt_target_cooltemp == 21.5
        assert bt.bt_target_temp == 21.0

    @pytest.mark.asyncio
    async def test_restored_preset_cool_target_is_not_overwritten(self):
        """A preset carries a cooling target the user chose, so the device is not read."""
        bt = _make_startup_bt()

        await _run_startup(
            bt,
            State(COOLER_ID, "cool", {"temperature": 24.0}),
            21.0,
            restored_cool_target=26.0,
        )

        assert bt.bt_target_cooltemp == 26.0

    @pytest.mark.asyncio
    async def test_no_cooler_configured_leaves_the_cool_target_unknown(self):
        """Without a cooling channel there is no device to take a target from.

        A readable setpoint answers the lookup all the same, so the missing
        configuration is what leaves the cool target unknown.
        """
        bt = _make_startup_bt()
        bt.cooler_entity_id = None

        await _run_startup(bt, State(COOLER_ID, "cool", {"temperature": 24.0}), 21.0)

        assert bt.bt_target_cooltemp is None

    @pytest.mark.asyncio
    async def test_this_read_names_itself_when_an_attribute_cannot_be_read(
        self, caplog
    ):
        """An unreadable setpoint attribute is reported against this read.

        The startup sequence reads the cooler twice through the same helper, so
        the line that reports the failed conversion names the read it belongs
        to rather than the sequence both reads run in.
        """
        bt = _make_startup_bt()

        with caplog.at_level(logging.DEBUG):
            await _run_startup(
                bt, State(COOLER_ID, "cool", {"temperature": "n/a"}), 21.0
            )

        assert bt.bt_target_cooltemp is None
        assert "Could not convert 'n/a' to float in startup()" in caplog.text

    @pytest.mark.asyncio
    async def test_this_read_names_itself_when_the_step_cannot_be_read(self, caplog):
        """An unreadable step attribute is reported against this read as well.

        The cooler's step reaches the resolution through a forwarding of its
        own, so it names the read it belongs to on the same terms as the
        setpoint does. The setpoint is readable here, which leaves the step as
        the only thing the reported conversion can be about.
        """
        bt = _make_startup_bt()

        with caplog.at_level(logging.DEBUG):
            await _run_startup(
                bt,
                State(
                    COOLER_ID, "cool", {"temperature": 24.0, "target_temp_step": "n/a"}
                ),
                21.0,
            )

        assert bt.bt_target_cooltemp == 24.0
        assert "Could not convert 'n/a' to float in startup()" in caplog.text


# ---------------------------------------------------------------------------
# 6. _finalize_startup cooler setpoint re-read
# ---------------------------------------------------------------------------


def _make_finalize_bt():
    """Build a BetterThermostat mock for a _finalize_startup run with a cooler."""
    mock = MagicMock(spec=BetterThermostat)
    mock.hass = MagicMock()
    mock.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    mock.device_name = "Test BT"
    mock.is_removed = False
    mock.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID)}
    mock.all_trvs = None
    mock.all_entities = []
    mock.entity_ids = [TRV_ID]
    mock.sensor_entity_id = SENSOR_ID
    mock.humidity_sensor_entity_id = None
    mock.window_id = None
    mock.door_id = None
    mock.cooler_entity_id = COOLER_ID
    mock.outdoor_sensor = None
    mock._async_unsub_state_changed = None
    mock.bt_target_cooltemp = None
    mock.bt_target_temp = 21.0
    mock.bt_target_temp_step = 0.5
    mock.bt_min_temp = 5.0
    mock.bt_max_temp = 30.0
    mock.hvac_mode = HVACMode.HEAT_COOL
    mock.bt_hvac_mode = HVACMode.HEAT_COOL
    mock.control_queue_task = AsyncMock()
    # Plain MagicMocks so the un-awaited coroutines handed to the background
    # task mock do not raise "coroutine was never awaited" warnings.
    mock._post_grace_recheck = MagicMock()
    mock._external_temperature_keepalive = MagicMock()
    mock._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(mock, **kwargs)
    )
    mock._seed_cool_target = lambda setpoint, entity_id: (
        BetterThermostat._seed_cool_target(mock, setpoint, entity_id)
    )
    mock._seed_cool_target_from_cooler = lambda log_source: (
        BetterThermostat._seed_cool_target_from_cooler(mock, log_source)
    )
    mock._bound_target_to_range = lambda value: BetterThermostat._bound_target_to_range(
        mock, value
    )
    return mock


async def _run_finalize_startup(bt):
    """Run _finalize_startup with its waits, timers and listeners stubbed out."""
    climate = "custom_components.better_thermostat.climate"
    with (
        patch(f"{climate}.await_critical_entities", AsyncMock()),
        patch(f"{climate}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{climate}.await_optional_sensors", AsyncMock()),
        patch(f"{climate}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{climate}.asyncio.sleep", AsyncMock()),
        patch(f"{climate}.async_track_time_interval"),
        patch(f"{climate}.async_track_state_change_event"),
        patch(f"{climate}.async_track_time_change"),
    ):
        await BetterThermostat._finalize_startup(bt)


class TestFinalizeStartupCoolerReread:
    """The cooler setpoint is re-read once the cooler listener is live."""

    @pytest.mark.asyncio
    async def test_reread_seeds_cooltemp_when_cooler_arrived(self):
        """A cooler that joined HA after the startup read is picked up."""
        bt = _make_finalize_bt()
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 24.0}
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 24.0
        bt.control_queue_task.put.assert_awaited_once_with(bt)

    @pytest.mark.asyncio
    async def test_reread_seeds_from_a_cooler_resting_at_off(self):
        """A cooler switched off still carries the setpoint it would cool to.

        This is the resting state of an idle air conditioner, and a device at
        rest publishes no further state change, so the re-read is the only place
        its setpoint is ever seen. What the state string names is the mode the
        device is in, not whether its setpoint can be read, so the value is
        taken and Better Thermostat runs a cycle on it.
        """
        bt = _make_finalize_bt()
        bt.hass.states.get.return_value = State(
            COOLER_ID, HVACMode.OFF, {"temperature": 24.0}
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 24.0
        bt.control_queue_task.put.assert_awaited_once_with(bt)

    @pytest.mark.asyncio
    async def test_seed_while_bt_is_off_runs_no_control_cycle(self):
        """A BT that is OFF learns the cool target from the re-read but stays idle.

        Switching it on queues its own cycle, so a target arriving while it is
        off is no reason to run one.
        """
        bt = _make_finalize_bt()
        bt.hvac_mode = HVACMode.OFF
        bt.bt_hvac_mode = HVACMode.OFF
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 24.0}
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 24.0
        bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reread_raises_a_setpoint_colliding_with_the_heat_target(self):
        """A read setpoint below the heating target is raised above it.

        The ordering has to hold for a BT that is still off, because switching it
        on does not revisit the pair.
        """
        bt = _make_finalize_bt()
        bt.hvac_mode = HVACMode.OFF
        bt.bt_hvac_mode = HVACMode.OFF
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 16.0}
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 21.5
        assert bt.bt_target_temp == 21.0

    @pytest.mark.asyncio
    async def test_reread_ignores_an_unavailable_cooler_reporting_a_setpoint(self):
        """A cooler still absent by then leaves the cool target for the handler.

        An entity that lost contact with its device can keep the attributes it
        last published, so a readable setpoint alone says nothing about whether
        the device still stands behind it. The state string is what decides.
        """
        bt = _make_finalize_bt()
        bt.hass.states.get.return_value = State(
            COOLER_ID, STATE_UNAVAILABLE, {"temperature": 24.0}
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp is None
        bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reread_ignores_an_unknown_cooler_reporting_a_setpoint(self):
        """A cooler that reports no mode yet leaves the cool target unknown.

        An entity Home Assistant has created but whose device has not reported
        in holds ``unknown`` while its attributes may already carry defaults, so
        the state string decides here as well.
        """
        bt = _make_finalize_bt()
        bt.hass.states.get.return_value = State(
            COOLER_ID, STATE_UNKNOWN, {"temperature": 24.0}
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp is None
        bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reread_leaves_cooltemp_unknown_when_cooler_has_no_state(self):
        """An entity that has not been created yet carries no state object.

        A cooler whose integration has not set it up returns nothing at all
        rather than an unavailable state, so the re-read has no attributes to
        look at.
        """
        bt = _make_finalize_bt()
        bt.hass.states.get.return_value = None

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp is None
        bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reread_requests_no_cycle_without_a_usable_setpoint(self):
        """An available cooler may still publish no setpoint to read.

        None of the setpoint attributes holds a usable value, so there is
        nothing to seed the cool target with and nothing for a control cycle to
        act on.
        """
        bt = _make_finalize_bt()
        bt.hass.states.get.return_value = State(
            COOLER_ID,
            "cool",
            {"temperature": None, "target_temp_low": None, "target_temp_high": None},
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp is None
        bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reread_skipped_when_cooltemp_already_known(self):
        """A cool target the startup read resolved is not overwritten."""
        bt = _make_finalize_bt()
        bt.bt_target_cooltemp = 26.0
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 24.0}
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 26.0
        bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reread_raises_a_seed_colliding_with_the_heat_target(self):
        """The re-read is an observation, so the cooling side yields on a collision."""
        bt = _make_finalize_bt()
        bt.bt_target_temp = 21.0
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 20.0}
        )

        await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 21.5
        assert bt.bt_target_temp == 21.0

    @pytest.mark.asyncio
    async def test_reread_reads_a_fahrenheit_cooler_in_its_own_unit(self):
        """A Fahrenheit cooler's setpoint and step are read as Fahrenheit.

        The re-read shares the unit-aware boundary with the event handler, so
        the seeded target is the Celsius value of the reported setpoint and the
        echo window is built from the device's step scaled to a Celsius delta,
        not from Better Thermostat's own step.
        """
        bt = _make_finalize_bt()
        bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        bt.bt_target_temp = 20.0
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 75.0, "target_temp_step": 2.0}
        )
        derived_steps = []

        def _record_step(instance, state, log_source):
            derived_steps.append(device_setpoint_step(instance, state, log_source))
            return derived_steps[-1]

        with patch(
            "custom_components.better_thermostat.climate.device_setpoint_step",
            _record_step,
        ):
            await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 23.89
        assert derived_steps == [round(2.0 * 5.0 / 9.0, 4)]

    @pytest.mark.asyncio
    async def test_reread_clamps_a_setpoint_outside_the_configured_range(self, caplog):
        """A cooler absent while the range was derived can report outside it.

        Such a cooler contributed no bounds to the temperature range, so its own
        setpoint may sit below the configured minimum. The clamped value is
        written back to the device, so it is annunciated.
        """
        bt = _make_finalize_bt()
        bt.bt_min_temp = 22.0
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 20.0}
        )

        with caplog.at_level(logging.WARNING):
            await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 22.0
        assert (
            "reported setpoint 20.0 outside of range while the cool target is "
            "unknown, taking 22.0 as the cool target" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_the_reread_names_itself_when_an_attribute_cannot_be_read(
        self, caplog
    ):
        """An unreadable setpoint attribute is reported against the re-read.

        This read shares the helper with the one that runs earlier in startup,
        so it names its own site: a cooler whose setpoint attribute holds
        something unreadable is otherwise indistinguishable from one the
        earlier read already stumbled over.
        """
        bt = _make_finalize_bt()
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": "n/a"}
        )

        with caplog.at_level(logging.DEBUG):
            await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp is None
        assert "Could not convert 'n/a' to float in _finalize_startup()" in caplog.text

    @pytest.mark.asyncio
    async def test_the_reread_names_itself_when_the_step_cannot_be_read(self, caplog):
        """An unreadable step attribute is reported against the re-read as well.

        The cooler's step reaches the resolution through a forwarding of its
        own, so this read names its own site for a step it cannot convert on
        the same terms as for a setpoint. The setpoint is readable here, which
        leaves the step as the only thing the reported conversion can be about.
        """
        bt = _make_finalize_bt()
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 24.0, "target_temp_step": "n/a"}
        )

        with caplog.at_level(logging.DEBUG):
            await _run_finalize_startup(bt)

        assert bt.bt_target_cooltemp == 24.0
        assert "Could not convert 'n/a' to float in _finalize_startup()" in caplog.text


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

        with patch(
            "custom_components.better_thermostat.climate.find_battery_entity", new=spy
        ):
            await _run_finalize_startup(bt)
        return scanned

    @pytest.mark.asyncio
    async def test_scan_reaches_the_cooler(self):
        """A configured cooler is asked for its battery entity."""
        bt = _make_finalize_bt()
        bt.devices_states = {}
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 24.0}
        )

        scanned = await self._scan(bt)

        assert COOLER_ID in scanned
        assert bt.devices_states[COOLER_ID]["battery_id"] == "sensor.cooler_battery"

    @pytest.mark.asyncio
    async def test_scan_reaches_the_outdoor_sensor(self):
        """A configured outdoor sensor is asked for its battery entity."""
        bt = _make_finalize_bt()
        bt.cooler_entity_id = None
        bt.outdoor_sensor = OUTDOOR_ID
        bt.devices_states = {}

        scanned = await self._scan(bt)

        assert OUTDOOR_ID in scanned
        assert (
            bt.devices_states[OUTDOOR_ID]["battery_id"] == "sensor.outdoor_temp_battery"
        )

    @pytest.mark.asyncio
    async def test_unconfigured_devices_are_not_scanned(self):
        """Nothing is registered for a cooler or outdoor sensor that is absent."""
        bt = _make_finalize_bt()
        bt.cooler_entity_id = None
        bt.outdoor_sensor = None
        bt.devices_states = {}

        scanned = await self._scan(bt)

        assert scanned == []
        assert bt.all_entities == []


# ---------------------------------------------------------------------------
# 7. _restore_state
# ---------------------------------------------------------------------------


class TestInitializeTrvCurrentTemperature:
    """The startup fallback for a missing TRV reading is 5.0 °C, literally.

    Passing the literal through the unit conversion turned it into about
    -15 °C on Fahrenheit systems, and the falsy-or fallback swallowed a
    real reading of 0.0.
    """

    def _trv_only_bt(self, bt, attrs, unit="°C"):
        bt.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID, calibration=1)}
        bt.hass.config.units.temperature_unit = unit
        bt.hass.states.get.return_value = _make_trv_state(attrs=attrs)
        return bt

    async def _run(self, bt):
        with (
            patch("custom_components.better_thermostat.climate.init", AsyncMock()),
            patch(
                "custom_components.better_thermostat.climate.initial_tweak", AsyncMock()
            ),
            patch(
                "custom_components.better_thermostat.climate.control_trv",
                AsyncMock(return_value=True),
            ),
        ):
            await BetterThermostat._initialize_trvs(bt)

    @pytest.mark.asyncio
    async def test_missing_reading_falls_back_to_five_celsius(self, bt):
        """No reading: the fallback is 5.0 °C on a Celsius system."""
        bt = self._trv_only_bt(bt, {"current_temperature": None})
        await self._run(bt)
        assert bt.real_trvs[TRV_ID].current_temperature == 5.0

    @pytest.mark.asyncio
    async def test_fallback_is_not_unit_converted(self, bt):
        """On a Fahrenheit system the fallback stays 5.0 °C, not -15 °C."""
        bt = self._trv_only_bt(bt, {"current_temperature": None}, unit="°F")
        await self._run(bt)
        assert bt.real_trvs[TRV_ID].current_temperature == 5.0

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
    async def test_restores_call_for_heat(self, bt):
        """Test Restores call for heat."""
        old = MagicMock()
        old.state = "heat"
        old.attributes = {ATTR_TEMPERATURE: 21.0, ATTR_STATE_CALL_FOR_HEAT: True}
        bt.async_get_last_state = AsyncMock(return_value=old)
        bt.preset_mgr.temperatures = {}

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
# 8. _validate_hvac_mode
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


class TestFinalizeStartupOnADualRoleEntity:
    """A cooler that is also one of the controlled thermostats.

    Such a device is already tracked as a thermostat, and one device reporting
    into two handlers means each handler reads the other channel's write as a
    user press.
    """

    @staticmethod
    def _make_shared_bt():
        bt = _make_finalize_bt()
        bt.cooler_entity_id = TRV_ID
        bt.preset_mgr = MagicMock()
        bt.preset_mgr.mode = PRESET_NONE
        bt._preset_cool_temperatures = {PRESET_NONE: 24.0}
        return bt

    @staticmethod
    async def _run_capturing_subscriptions(bt):
        """Run _finalize_startup and return the (entity ids, handler) pairs."""
        climate = "custom_components.better_thermostat.climate"
        with (
            patch(f"{climate}.await_critical_entities", AsyncMock()),
            patch(f"{climate}.check_critical_entities", AsyncMock(return_value=True)),
            patch(f"{climate}.await_optional_sensors", AsyncMock()),
            patch(f"{climate}.check_and_update_degraded_mode", AsyncMock()),
            patch(f"{climate}.asyncio.sleep", AsyncMock()),
            patch(f"{climate}.async_track_time_interval"),
            patch(f"{climate}.async_track_time_change"),
            patch(f"{climate}.async_track_state_change_event") as track,
        ):
            await BetterThermostat._finalize_startup(bt)
        return [(call.args[1], call.args[2]) for call in track.call_args_list]

    @pytest.mark.asyncio
    async def test_a_shared_entity_registers_only_the_trv_subscription(self):
        """The device is tracked once, and by the handler that survives."""
        bt = self._make_shared_bt()
        bt.hass.states.get.return_value = State(TRV_ID, "heat", {"temperature": 21.0})

        tracked = await self._run_capturing_subscriptions(bt)

        assert (bt.entity_ids, bt._trigger_trv_change) in tracked
        assert bt._trigger_cooler_change not in [handler for _, handler in tracked]

    @pytest.mark.asyncio
    async def test_a_distinct_cooler_still_registers_its_own_subscription(self):
        """A cooler of its own keeps the handler written for it."""
        bt = _make_finalize_bt()
        bt.hass.states.get.return_value = State(
            COOLER_ID, "cool", {"temperature": 24.0}
        )

        tracked = await self._run_capturing_subscriptions(bt)

        assert ([COOLER_ID], bt._trigger_cooler_change) in tracked

    @pytest.mark.asyncio
    async def test_a_shared_entity_seeds_the_cool_target_from_the_preset(self):
        """The cooling target comes from the preset, not off the device.

        The setpoint a shared device reports belongs to whichever channel last
        wrote it, and at startup that is the heating one. The seeded value only
        reaches the device through a control cycle, so the seed requests one.
        """
        bt = self._make_shared_bt()
        bt.hass.states.get.return_value = State(TRV_ID, "heat", {"temperature": 21.0})

        await self._run_capturing_subscriptions(bt)

        assert bt.bt_target_cooltemp == 24.0
        bt.control_queue_task.put.assert_awaited_once_with(bt)

    @pytest.mark.asyncio
    async def test_a_shared_entity_leaves_a_restored_cool_target_alone(self):
        """A cooling target the user already chose is never overwritten.

        Nothing was seeded, so there is no new value for a cycle to carry.
        """
        bt = self._make_shared_bt()
        bt.bt_target_cooltemp = 26.0
        bt.hass.states.get.return_value = State(TRV_ID, "heat", {"temperature": 21.0})

        await self._run_capturing_subscriptions(bt)

        assert bt.bt_target_cooltemp == 26.0
        bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_shared_entity_bounds_a_preset_outside_the_configured_range(self):
        """A preset stored under a wider range is seeded inside this one.

        The seeded value is published as ``target_temperature_high`` and
        written to the device, so a preset the configured range does not
        contain is not a setpoint the group can hold.
        """
        bt = self._make_shared_bt()
        bt._preset_cool_temperatures = {PRESET_NONE: 35.0}
        bt.hass.states.get.return_value = State(TRV_ID, "heat", {"temperature": 21.0})

        await self._run_capturing_subscriptions(bt)

        assert bt.bt_target_cooltemp == bt.bt_max_temp
        bt.control_queue_task.put.assert_awaited_once_with(bt)
