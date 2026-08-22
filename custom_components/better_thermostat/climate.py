"""Better Thermostat."""

from __future__ import annotations

from abc import ABC
import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
import json
import logging
import math
from random import randint
from typing import Any

# Home Assistant imports
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_ACTIVITY,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    PRESET_SLEEP,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
    UnitOfTemperature,
)
from homeassistant.core import Context, State, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    device_registry as dr,
    entity_platform,
    entity_registry as er,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.start import async_at_started
from homeassistant.util.unit_conversion import TemperatureConverter

# Local imports
from .adapters.delegate import (
    get_current_offset,
    get_max_offset,
    get_min_offset,
    get_offset_step,
    init,
    load_adapter,
    set_hvac_mode as adapter_set_hvac_mode,
    set_temperature as adapter_set_temperature,
    set_valve as adapter_set_valve,
)
from .core.clock import Clock
from .core.containers import BtConfig, BtRuntime
from .core.decide import KernelState
from .core.fsm.lifecycle import (
    startup_finished as lifecycle_startup_finished,
    stop as lifecycle_stop,
)
from .core.fsm.maintenance import (
    MaintenancePhase,
    MaintenanceState,
    evaluate_tick as maintenance_evaluate_tick,
    finish_run as maintenance_finish_run,
    start_run as maintenance_start_run,
)
from .core.fsm.mode import (
    set_hvac_mode as mode_set_hvac_mode,
    set_preset as mode_set_preset,
)
from .core.fsm.window import WindowPhase, WindowState
from .core.recorder import FlightRecorder
from .device_binding import async_bind_trv_device, async_unbind_trv_device
from .events.cooler import trigger_cooler_change
from .events.door import door_queue, trigger_door_change
from .events.temperature import _update_external_temp_ema, trigger_temperature_change
from .events.trv import trigger_trv_change
from .events.window import trigger_window_change, window_queue
from .model_fixes.model_quirks import initial_tweak, load_model_quirks
from .trv import Trv
from .utils.calibration.pid import (
    PIDParams,
    format_bucket,
    resolve_unique_id,
    round_to_bucket,
)
from .utils.clock import SystemClock
from .utils.const import (
    ATTR_STATE_BATTERIES,
    ATTR_STATE_CALL_FOR_HEAT,
    ATTR_STATE_DOOR_OPEN,
    ATTR_STATE_ERRORS,
    ATTR_STATE_HEAT_LOSS,
    ATTR_STATE_HEATING_POWER,
    ATTR_STATE_LAST_CHANGE,
    ATTR_STATE_MAIN_MODE,
    ATTR_STATE_OFF_TEMPERATURE,
    ATTR_STATE_PRESET_COOL_TEMPERATURE,
    ATTR_STATE_PRESET_COOL_TEMPERATURES,
    ATTR_STATE_PRESET_TEMPERATURE,
    ATTR_STATE_SAVED_TEMPERATURE,
    ATTR_STATE_WINDOW_OPEN,
    BETTERTHERMOSTAT_RESET_PID_SCHEMA,
    CONF_COOLER,
    CONF_DOOR_TIMEOUT,
    CONF_DOOR_TIMEOUT_AFTER,
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_MODEL,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_PRESETS,
    CONF_SENSOR,
    CONF_SENSOR_DOOR,
    CONF_SENSOR_WINDOW,
    CONF_TARGET_TEMP_STEP,
    CONF_TOLERANCE,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_TARGET_TEMP,
    DOMAIN,
    SERVICE_RESET_HEATING_POWER,
    SERVICE_RESET_PID_LEARNINGS,
    SERVICE_RUN_VALVE_MAINTENANCE,
    SUPPORT_FLAGS,
    VERSION,
    CalibrationMode,
    CalibrationType,
)
from .utils.controlling import (
    compute_control_cycle,
    control_queue,
    control_trv,
    cooler_send_cache,
    reconcile_tick,
)
from .utils.helpers import (
    COOLER_SETPOINT_KEYS,
    InboundSetpoint,
    async_fire_logbook_entry,
    async_normalize_bt_entity_ids,
    attr_to_celsius,
    convert_to_float,
    convert_to_float_celsius,
    device_setpoint_step,
    dual_role_entity_id,
    find_battery_entity,
    get_device_model,
    get_hvac_bt_mode,
    is_reasonable_temperature,
    normalize_hvac_mode,
    normalize_step,
    reported_setpoint_step_celsius,
    resolve_inbound_setpoint,
    state_temperature_unit,
)
from .utils.hvac_action import (
    ToleranceHysteresis,
    TrvSnapshot,
    compute_hvac_action,
    should_heat_with_tolerance,
)
from .utils.migrate_v0_stores import migrate_v0_stores
from .utils.preset_manager import PresetManager
from .utils.restore import (
    clamp_heat_loss,
    clamp_heating_power,
    mean_trv_target,
    restore_target_temperature,
)
from .utils.scheduler import request_control_cycle
from .utils.state_manager import StateManager
from .utils.telemetry import (
    collect_balance_attrs,
    collect_cycle_telemetry,
    collect_mpc_v2_debug_attrs,
    collect_pid_debug_attrs,
)
from .utils.thermal_learning import (
    HeatingCycle,
    HeatingPowerTracker,
    HeatLossTracker,
    LossCycle,
    LossStats,
)
from .utils.valve_maintenance import (
    build_trv_snapshots,
    collect_maintenance_trvs,
    compute_initial_maintenance,
    compute_next_maintenance,
    run_valve_maintenance,
)
from .utils.watcher import (
    STARTUP_CRITICAL_GRACE_PERIOD,
    STARTUP_DEGRADED_GRACE_PERIOD,
    await_critical_entities,
    await_optional_sensors,
    check_and_update_degraded_mode,
    check_critical_entities,
    is_entity_available,
)
from .utils.weather import check_ambient_air_temperature, check_weather

_LOGGER = logging.getLogger(__name__)

# Default temperature when no sensor data is available (last resort fallback)
DEFAULT_FALLBACK_TEMPERATURE = 20.0

# Signal for dynamic entity updates
SIGNAL_BT_CONFIG_CHANGED = "bt_config_changed_{}"


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Better Thermostat platform."""
    _LOGGER.debug("better_thermostat: async_setup_platform called (deprecated no-op)")


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Better Thermostat climate entity for a config entry."""
    _LOGGER.debug(
        "better_thermostat %s: async_setup_entry start (entry_id=%s)",
        entry.data.get(CONF_NAME),
        entry.entry_id,
    )

    platform = entity_platform.async_get_current_platform()
    # Register entity services (validator done manually inside method)
    platform.async_register_entity_service(
        SERVICE_RESET_HEATING_POWER, {}, "reset_heating_power"
    )
    platform.async_register_entity_service(
        SERVICE_RUN_VALVE_MAINTENANCE, {}, "run_valve_maintenance_service"
    )
    platform.async_register_entity_service(
        SERVICE_RESET_PID_LEARNINGS,
        BETTERTHERMOSTAT_RESET_PID_SCHEMA,
        "reset_pid_learnings_service",
    )

    bt_entity = BetterThermostat(
        entry.data.get(CONF_NAME),
        entry.data.get(CONF_HEATER),
        entry.data.get(CONF_SENSOR),
        entry.data.get(CONF_HUMIDITY, None),
        entry.data.get(CONF_SENSOR_WINDOW, None),
        entry.data.get(CONF_WINDOW_TIMEOUT, None),
        entry.data.get(CONF_WINDOW_TIMEOUT_AFTER, None),
        entry.data.get(CONF_SENSOR_DOOR, None),
        entry.data.get(CONF_DOOR_TIMEOUT, None),
        entry.data.get(CONF_DOOR_TIMEOUT_AFTER, None),
        entry.data.get(CONF_WEATHER, None),
        entry.data.get(CONF_OUTDOOR_SENSOR, None),
        entry.data.get(CONF_OFF_TEMPERATURE, None),
        entry.data.get(CONF_TOLERANCE, 0.0),
        entry.data.get(CONF_TARGET_TEMP_STEP, "0.0"),
        entry.data.get(CONF_MODEL, None),
        entry.data.get(CONF_COOLER, None),
        entry.data.get(CONF_PRESETS, None),
        hass.config.units.temperature_unit,
        entry.entry_id,
        device_class="better_thermostat",
        state_class="better_thermostat_state",
    )
    hass.data[DOMAIN][entry.entry_id]["climate"] = bt_entity
    async_normalize_bt_entity_ids(hass, entry, Platform.CLIMATE)
    async_add_entities([bt_entity])
    _LOGGER.debug(
        "better_thermostat %s: async_setup_entry finished creating entity",
        entry.data.get(CONF_NAME),
    )


def _seed_contact_region_at_startup(
    self, entity_id: str | None, kind: str
) -> WindowState:
    """Seed a contact region (window/door) from the sensor's startup state.

    At startup, unavailable/unknown usually means the sensor has not joined
    HA yet, so the region starts closed and heating continues normally. The
    runtime handlers (events/window.py, events/door.py) treat the same
    states as closed too, logging the lost sensor so it does not go
    unnoticed.
    """
    if entity_id is None:
        return WindowState()
    self.all_entities.append(entity_id)
    state = self.hass.states.get(entity_id)

    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
        _LOGGER.debug(
            "better_thermostat %s: %s sensor unavailable, assuming closed",
            self.device_name,
            kind,
        )
        return WindowState()

    is_open = state.state in ("on", "true", "open")
    _LOGGER.debug(
        "better_thermostat %s: detected %s state at startup: %s",
        self.device_name,
        kind,
        "Open" if is_open else "Closed",
    )
    return WindowState(phase=WindowPhase.OPEN if is_open else WindowPhase.CLOSED)


def _arm_degraded_grace(self) -> None:
    """Arm the degraded-mode annunciation grace window once.

    Stores the deadline on the entity and mirrors it into the lifecycle
    region, so every degraded-mode check that runs while startup is still in
    progress already sees the grace window. A deadline that is already armed
    is kept unchanged.
    """
    if self._degraded_grace_until is not None:
        return
    self._degraded_grace_until = self.clock.now() + STARTUP_DEGRADED_GRACE_PERIOD
    self.kernel_state = replace(
        self.kernel_state,
        lifecycle=replace(
            self.kernel_state.lifecycle, grace_until=self._degraded_grace_until
        ),
    )


def _target_temp_step_celsius(
    state: State | None, device_name: str, system_unit: str | None
) -> float | None:
    """Read a child's own setpoint step and return it as a Celsius delta.

    ``None`` stays ``None``: a child that publishes no convertible step
    contributes nothing, which is what lets the callers tell "no child told us
    anything" apart from a step that was read off a child. The positive-step
    fallback that ``device_setpoint_step`` applies on top of the same rule is
    deliberately not applied here.
    """
    return reported_setpoint_step_celsius(
        state, device_name, system_unit, "_target_temp_step_celsius"
    )


class BetterThermostat(ClimateEntity, RestoreEntity, ABC):
    """Representation of a Better Thermostat device."""

    _attr_has_entity_name = True
    _attr_name = None
    _enable_turn_on_off_backwards_compatibility = False

    # ECO mode removed; set_eco_mode service and logic deleted.

    async def reset_heating_power(self):
        """Reset heating power to default value."""
        self._heating_tracker.reset_power()
        self.async_write_ha_state()

    # Thermal tracker properties
    # These forward to self._heating_tracker / self._loss_tracker and provide
    # the read-only surface that the TelemetrySource protocol (utils/telemetry.py)
    # consumes, plus the attribute names sensor.py maps via _climate_attr. Keeping
    # them on the entity is what lets telemetry stay decoupled from the tracker
    # internals instead of reaching into the private trackers directly.
    # ------------------------------------------------------------------

    # -- Container bridges -----------------------------------------------
    # Historical attribute names delegating into the typed containers
    # (BtConfig is frozen: config bridges are read-only by design).

    @property
    def device_name(self) -> str:
        """Return the configured device name."""
        return self.config.device_name

    @property
    def model(self) -> str | None:
        """Return the configured model string."""
        return self.config.model

    @property
    def sensor_entity_id(self) -> str | None:
        """Return the room temperature sensor entity id."""
        return self.config.sensor_entity_id

    @property
    def humidity_sensor_entity_id(self) -> str | None:
        """Return the humidity sensor entity id."""
        return self.config.humidity_sensor_entity_id

    @property
    def cooler_entity_id(self) -> str | None:
        """Return the cooler entity id."""
        return self.config.cooler_entity_id

    @property
    def window_id(self) -> str | None:
        """Return the window sensor entity id."""
        return self.config.window_id

    @property
    def window_delay(self) -> float:
        """Return the window-open debounce delay in seconds."""
        return self.config.window_delay

    @property
    def window_delay_after(self) -> float:
        """Return the window-close debounce delay in seconds."""
        return self.config.window_delay_after

    @property
    def door_id(self) -> str | None:
        """Return the door sensor entity id."""
        return self.config.door_id

    @property
    def door_delay(self) -> float:
        """Return the door-open debounce delay in seconds."""
        return self.config.door_delay

    @property
    def door_delay_after(self) -> float:
        """Return the door-close debounce delay in seconds."""
        return self.config.door_delay_after

    @property
    def weather_entity(self) -> str | None:
        """Return the weather entity id."""
        return self.config.weather_entity

    @property
    def outdoor_sensor(self) -> str | None:
        """Return the outdoor sensor entity id."""
        return self.config.outdoor_sensor

    @property
    def off_temperature(self) -> float | None:
        """Return the outdoor threshold temperature."""
        return self.config.off_temperature

    @property
    def tolerance(self) -> float:
        """Return the configured tolerance in Kelvin."""
        return self.config.tolerance

    @property
    def cur_temp(self) -> float | None:
        """Return the current room temperature."""
        return self.runtime.cur_temp

    @cur_temp.setter
    def cur_temp(self, value: float | None) -> None:
        """Set the current room temperature."""
        self.runtime.cur_temp = value

    @property
    def cur_temp_filtered(self) -> float | None:
        """Return the EMA-filtered room temperature."""
        return self.runtime.cur_temp_filtered

    @cur_temp_filtered.setter
    def cur_temp_filtered(self, value: float | None) -> None:
        """Set the EMA-filtered room temperature."""
        self.runtime.cur_temp_filtered = value

    @property
    def external_temp_ema(self) -> float | None:
        """Return the raw external temperature EMA."""
        return self.runtime.external_temp_ema

    @external_temp_ema.setter
    def external_temp_ema(self, value: float | None) -> None:
        """Set the raw external temperature EMA."""
        self.runtime.external_temp_ema = value

    @property
    def temp_slope(self) -> float | None:
        """Return the temperature slope in K/min."""
        return self.runtime.temp_slope

    @temp_slope.setter
    def temp_slope(self, value: float | None) -> None:
        """Set the temperature slope in K/min."""
        self.runtime.temp_slope = value

    @property
    def window_open(self) -> bool:
        """Return the committed window-open state (window region)."""
        return self.kernel_state.window.effective_open

    @property
    def door_open(self) -> bool:
        """Return the committed door-open state (door region)."""
        return self.kernel_state.door.effective_open

    @property
    def call_for_heat(self) -> bool:
        """Return whether the room currently demands heat."""
        return self.runtime.call_for_heat

    @call_for_heat.setter
    def call_for_heat(self, value: bool) -> None:
        """Set whether the room currently demands heat."""
        self.runtime.call_for_heat = value

    @property
    def ignore_states(self) -> bool:
        """Return the control queue's reentrancy guard."""
        return self.runtime.ignore_states

    @ignore_states.setter
    def ignore_states(self, value: bool) -> None:
        """Set the control queue's reentrancy guard."""
        self.runtime.ignore_states = value

    @property
    def in_maintenance(self) -> bool:
        """Return whether valve maintenance pre-empts control.

        Derived from the maintenance region; a stale RUNNING phase stops
        blocking (liveness invariant).
        """
        return self.kernel_state.maintenance.is_blocking(self.clock.monotonic())

    @property
    def startup_running(self) -> bool:
        """Return whether the startup sequence is running (lifecycle region)."""
        return self.kernel_state.lifecycle.startup_running

    @property
    def degraded_mode(self) -> bool:
        """Return the degraded-mode annunciation (control-mode region)."""
        return self.kernel_state.control_mode.degraded

    @property
    def bt_target_temp(self) -> float | None:
        """Return the BT-internal target temperature."""
        return self.runtime.bt_target_temp

    @bt_target_temp.setter
    def bt_target_temp(self, value: float | None) -> None:
        """Set the BT-internal target temperature."""
        self.runtime.bt_target_temp = value

    @property
    def bt_target_cooltemp(self) -> float | None:
        """Return the BT-internal cooling target temperature."""
        return self.runtime.bt_target_cooltemp

    @bt_target_cooltemp.setter
    def bt_target_cooltemp(self, value: float | None) -> None:
        """Set the BT-internal cooling target temperature."""
        self.runtime.bt_target_cooltemp = value

    @property
    def heating_power(self) -> float:
        """Return the current heating power in °C/min."""
        return self._heating_tracker.heating_power

    @heating_power.setter
    def heating_power(self, value: float) -> None:
        self._heating_tracker.heating_power = value

    @property
    def heating_power_normalized(self) -> float | None:
        """Return the normalized heating power."""
        return self._heating_tracker.normalized_power

    @heating_power_normalized.setter
    def heating_power_normalized(self, value: float | None) -> None:
        self._heating_tracker.normalized_power = value

    @property
    def last_heating_power_stats(self) -> deque:
        """Return recent heating power statistics."""
        return self._heating_tracker.stats

    @property
    def heating_cycles(self) -> deque[HeatingCycle]:
        """Return recorded heating cycles."""
        return self._heating_tracker.cycles

    @property
    def heat_loss_rate(self) -> float:
        """Return the current heat loss rate in °C/min."""
        return self._loss_tracker.heat_loss_rate

    @heat_loss_rate.setter
    def heat_loss_rate(self, value: float) -> None:
        self._loss_tracker.heat_loss_rate = value

    @property
    def last_heat_loss_stats(self) -> deque[LossStats]:
        """Return recent heat loss statistics."""
        return self._loss_tracker.stats

    @property
    def loss_cycles(self) -> deque[LossCycle]:
        """Return recorded heat loss cycles."""
        return self._loss_tracker.cycles

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        info = DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            name=self.device_name,
            manufacturer="Better Thermostat",
            model=self.model,
            sw_version=VERSION,
        )

        try:
            if hasattr(self, "hass") and self.hass and self.all_trvs:
                main_trv_id = None
                if isinstance(self.all_trvs, list) and len(self.all_trvs) > 0:
                    main_trv_id = self.all_trvs[0].get("trv")
                elif isinstance(self.all_trvs, str):
                    main_trv_id = self.all_trvs

                if main_trv_id:
                    ent_reg = er.async_get(self.hass)
                    dev_reg = dr.async_get(self.hass)
                    trv_ent = ent_reg.async_get(main_trv_id)
                    if trv_ent and trv_ent.device_id:
                        trv_dev = dev_reg.async_get(trv_ent.device_id)
                        if trv_dev and trv_dev.identifiers:
                            info["via_device"] = list(trv_dev.identifiers)[0]
        except Exception as e:
            _LOGGER.debug("better_thermostat: Error getting via_device: %s", e)

        return info

    def __init__(
        self,
        name,
        heater_entity_id,
        sensor_entity_id,
        humidity_sensor_entity_id,
        window_id,
        window_delay,
        window_delay_after,
        door_id,
        door_delay,
        door_delay_after,
        weather_entity,
        outdoor_sensor,
        off_temperature,
        tolerance,
        target_temp_step,
        model,
        cooler_entity_id,
        enabled_presets,
        unit,
        unique_id,
        device_class,
        state_class,
    ):
        """Initialize the thermostat.

        Parameters
        ----------
        name : str
            Display name of the thermostat.
        heater_entity_id : list[dict]
            TRV configuration entries controlled by this thermostat.
        sensor_entity_id : str | None
            External temperature sensor entity id.
        humidity_sensor_entity_id : str | None
            External humidity sensor entity id.
        window_id : str | None
            Window contact sensor entity id for open-window detection.
        window_delay : int
            Delay in seconds before reacting to a window opening.
        window_delay_after : int
            Delay in seconds before reacting to a window closing.
        door_id : str | None
            Door contact sensor entity id for open-door detection.
        door_delay : int
            Delay in seconds before reacting to a door opening.
        door_delay_after : int
            Delay in seconds before reacting to a door closing.
        weather_entity : str | None
            Weather entity used as outdoor temperature source.
        outdoor_sensor : str | None
            Outdoor temperature sensor entity id.
        off_temperature : float | None
            Outdoor temperature above which heating is switched off.
        tolerance : float
            Temperature hysteresis in degrees.
        target_temp_step : str | float | None
            Step size for target temperature adjustments.
        model : str
            Detected TRV model identifier.
        cooler_entity_id : str | None
            Cooler entity id.
        enabled_presets : list[str]
            Presets enabled for this thermostat.
        unit : str
            Temperature unit reported by the entity.
        unique_id : str
            Unique id of the config entry.
        device_class : str | None
            Device class of the climate entity.
        state_class : str | None
            State class of the climate entity.
        """
        self.real_trvs: dict[str, Trv] = {}
        self.entity_ids = []
        self.all_trvs = heater_entity_id
        # Robust off temperature parsing: preserve 0.0 and ignore invalid strings
        _off_temperature = None
        if off_temperature not in (None, "", "None"):  # allow numeric 0
            try:
                parsed_off = float(off_temperature)
                # Accept any float (including 0.0); reject extreme nonsense
                if -100.0 < parsed_off < 150.0:
                    if unit == UnitOfTemperature.FAHRENHEIT:
                        parsed_off = TemperatureConverter.convert(
                            parsed_off,
                            UnitOfTemperature.FAHRENHEIT,
                            UnitOfTemperature.CELSIUS,
                        )
                    _off_temperature = parsed_off
                else:
                    _LOGGER.warning(
                        "better_thermostat %s: off_temperature %.2f outside plausible range, ignoring",
                        name,
                        parsed_off,
                    )
            except TypeError, ValueError:
                _LOGGER.warning(
                    "better_thermostat %s: invalid off_temperature '%s', ignoring",
                    name,
                    off_temperature,
                )

        # Robust tolerance parsing & sanitizing
        try:
            _tolerance = float(tolerance) if tolerance is not None else 0.0
            if not math.isfinite(_tolerance):
                raise ValueError
            if unit == UnitOfTemperature.FAHRENHEIT:
                _tolerance = _tolerance * 5.0 / 9.0
        except TypeError, ValueError:
            _LOGGER.warning(
                "better_thermostat %s: invalid tolerance '%s', falling back to 0.0",
                name,
                tolerance,
            )
            _tolerance = 0.0
        if _tolerance < 0:
            _LOGGER.warning(
                "better_thermostat %s: negative tolerance '%s' adjusted to 0.0",
                name,
                _tolerance,
            )
            _tolerance = 0.0
        if _tolerance > 10:
            _LOGGER.warning(
                "better_thermostat %s: unusually high tolerance '%s' (>10) may cause sluggish response",
                name,
                _tolerance,
            )

        # The three attribute lifecycles, each in its own container; the
        # historical attribute names delegate via properties.
        self.config = BtConfig(
            device_name=name,
            model=model,
            sensor_entity_id=sensor_entity_id,
            humidity_sensor_entity_id=humidity_sensor_entity_id,
            cooler_entity_id=cooler_entity_id,
            window_id=window_id or None,
            window_delay=window_delay or 0,
            window_delay_after=window_delay_after or 0,
            door_id=door_id or None,
            door_delay=door_delay or 0,
            door_delay_after=door_delay_after or 0,
            weather_entity=weather_entity or None,
            outdoor_sensor=outdoor_sensor or None,
            off_temperature=_off_temperature,
            tolerance=_tolerance,
        )
        self.runtime = BtRuntime()
        self._unique_id = unique_id
        self._unit = unit
        self._device_class = device_class
        self._state_class = state_class
        self._hvac_list = [HVACMode.HEAT, HVACMode.OFF]
        self.map_on_hvac_mode = HVACMode.HEAT
        self.clock: Clock = SystemClock()
        self.kernel_state = KernelState()
        self.flight_recorder = FlightRecorder()
        self.next_valve_maintenance = self.clock.now() + timedelta(
            hours=randint(1, 24 * 5)
        )
        self.cur_temp = None
        self._current_humidity: float | None = 0.0
        self.bt_target_temp_step = (
            float(target_temp_step)
            if target_temp_step and target_temp_step != "0.0"
            else None
        )
        if (
            self.bt_target_temp_step is not None
            and unit == UnitOfTemperature.FAHRENHEIT
        ):
            self.bt_target_temp_step = round(self.bt_target_temp_step * 5.0 / 9.0, 4)
        # ``bt_target_temp_step`` also absorbs the step derived from the child
        # entities, so the explicitly configured value is kept apart: it is the
        # only step that may override a device's own grid.
        self._configured_target_temp_step: float | None = (
            self.bt_target_temp_step
            if self.bt_target_temp_step and self.bt_target_temp_step > 0.0
            else None
        )
        self.bt_min_temp: float | None = DEFAULT_MIN_TEMP
        self.bt_max_temp: float | None = DEFAULT_MAX_TEMP
        self.bt_target_temp = DEFAULT_TARGET_TEMP
        self.bt_target_cooltemp = None
        self._support_flags = SUPPORT_FLAGS | ClimateEntityFeature.PRESET_MODE
        self._bt_hvac_mode: HVACMode | None = None
        # Track min/max encountered target temps (initialize to default span)
        self.min_target_temp = 18.0
        self.max_target_temp = 21.0
        self.closed_window_triggered = False
        self.call_for_heat = True
        self.ignore_states = False
        self.last_dampening_timestamp = None
        self.version = VERSION
        self.last_change = self.clock.now() - timedelta(hours=2)
        self.last_external_sensor_change = self.clock.now() - timedelta(hours=2)
        self.last_internal_sensor_change = self.clock.now() - timedelta(hours=2)
        self._temp_lock = asyncio.Lock()
        self.bt_update_lock = False
        self._saved_temperature = None
        if enabled_presets is not None:
            self.preset_mgr = PresetManager(enabled_presets=enabled_presets)
        else:
            self.preset_mgr = PresetManager()
        self._preset_cool_temperatures = {
            PRESET_NONE: 24.0,
            PRESET_AWAY: 28.0,
            PRESET_BOOST: 28.0,
            PRESET_COMFORT: 24.0,
            PRESET_ECO: 27.0,
            PRESET_HOME: 24.0,
            PRESET_SLEEP: 22.0,
            PRESET_ACTIVITY: 23.0,
        }
        self._preset_cool_temperature = None  # saved cool temp before entering preset
        # Config entry id (same as unique id passed in) used for durable persistence beyond RestoreEntity
        self._config_entry_id = self._unique_id
        self.last_avg_outdoor_temp = None
        self.last_main_hvac_mode = None
        self._last_call_for_heat = None
        self._available = False
        self.context = None
        self.attr_hvac_action = None
        self.old_attr_hvac_action = None
        self._hysteresis = ToleranceHysteresis()
        self.heating_start_temp = None
        self.heating_start_timestamp = None
        self.heating_end_temp = None
        self.heating_end_timestamp = None
        # Thermal learning trackers (state machines for heating power / heat loss)
        # Must be initialised before property-based assignments below.
        self._heating_tracker = HeatingPowerTracker()
        self._loss_tracker = HeatLossTracker()
        # Heat loss tracking (idle cooling rate)
        self.loss_start_temp = None
        self.loss_start_timestamp = None
        self.loss_end_temp = None
        self.loss_end_timestamp = None
        self.heat_loss_rate = 0.01
        self._loss_last_action = None
        self._tolerance_last_action = HVACAction.IDLE
        self._tolerance_hold_active = False
        self._async_unsub_state_changed = None
        self.all_entities = []
        self.devices_states = {}
        self.devices_errors = []
        # Degraded mode: thermostat continues operating with some sensors unavailable
        self.unavailable_sensors = []
        # Startup grace period suppresses the degraded-mode WARNING and the HA
        # repair issue while slow integrations finish initializing.
        self._degraded_grace_until: datetime | None = None
        self._degraded_warning_emitted: bool = False
        self.control_queue_task: asyncio.Queue[BetterThermostat] = asyncio.Queue(
            maxsize=1
        )
        if self.window_id is not None:
            self.window_queue_task: asyncio.Queue[BetterThermostat] = asyncio.Queue(
                maxsize=1
            )
        if self.door_id is not None:
            self.door_queue_task: asyncio.Queue[BetterThermostat] = asyncio.Queue(
                maxsize=1
            )
        self._control_task = None
        self._window_task = None
        self._door_task = None
        self.is_removed = False
        # Valve maintenance control
        # If control actions are requested during valve maintenance, defer them and
        # trigger one control cycle once maintenance finishes.
        self._control_needed_after_maintenance = False
        # Balance / Hydraulic: temperature trend (K/min)
        self.temp_slope = None
        self._slope_last_temp = None
        self._slope_last_ts = None
        # External temperature filter (anti-jitter for controllers like MPC)
        # 900s = 15min, 1800s = 30min
        self.external_temp_ema_tau_s = 300.0
        self.external_temp_ema = None
        self._external_temp_ema_ts = None
        self.cur_temp_filtered = None
        # Unified state persistence (replaces per-controller stores)
        self.state_mgr: StateManager | None = None

        self.last_known_external_temp = None
        self._slope_periodic_last_ts = None

        # Anti-flicker state
        self.flicker_unignore_cancel = None
        self.flicker_candidate = None
        self.plateau_timer_cancel = None
        self.last_change_direction = 0
        self.prev_stable_temp = None
        self.accum_delta = 0.0
        self.accum_dir = 0
        self.pending_temp = None
        self.pending_since = None

    async def async_added_to_hass(self):
        """Run when entity about to be added.

        Returns
        -------
        None
        """
        if isinstance(self.all_trvs, str):
            return _LOGGER.error(
                "You updated from version before 1.0.0-Beta36 of the Better Thermostat integration, "
                "you need to remove the BT devices (integration) and add it again."
            )

        self._control_task = self.hass.async_create_background_task(
            control_queue(self), name=f"bt_control_queue_{self.device_name}"
        )
        if self.window_id is not None:
            self._window_task = self.hass.async_create_background_task(
                window_queue(self), name=f"bt_window_queue_{self.device_name}"
            )
        if self.door_id is not None:
            self._door_task = self.hass.async_create_background_task(
                door_queue(self), name=f"bt_door_queue_{self.device_name}"
            )

        if self.cooler_entity_id is not None:
            self._hvac_list.remove(HVACMode.HEAT)
            self._hvac_list.append(HVACMode.HEAT_COOL)
            self.map_on_hvac_mode = HVACMode.HEAT_COOL

        self.entity_ids = [
            entity for trv in self.all_trvs if (entity := trv["trv"]) is not None
        ]

        for trv in self.all_trvs:
            _calibration = 1
            _advanced = trv.get("advanced", {})
            _calibration_type = _advanced.get("calibration")
            if _calibration_type == CalibrationType.TARGET_TEMP_BASED:
                _calibration = 0
            if _calibration_type == CalibrationType.DIRECT_VALVE_BASED:
                _calibration = 2
            if _calibration_type == CalibrationType.LOCAL_BASED:
                _calibration = 3
            _adapter = await load_adapter(self, trv["integration"], trv["trv"])
            # Resolve/refresh model dynamically at startup to ensure correct quirks
            resolved_model = trv.get("model")
            try:
                # prefers state model_id when present
                detected_model = await get_device_model(self, trv["trv"])
                if (
                    isinstance(detected_model, str)
                    and detected_model
                    and detected_model != resolved_model
                ):
                    _LOGGER.info(
                        "better_thermostat %s: detected model '%s' for %s (was '%s' in config), using detected model",
                        self.device_name,
                        detected_model,
                        trv["trv"],
                        resolved_model,
                    )
                    resolved_model = detected_model
            except (AttributeError, TypeError) as e:
                _LOGGER.debug(
                    "better_thermostat %s: get_device_model(%s) failed: %s",
                    self.device_name,
                    trv.get("trv"),
                    e,
                )
            _LOGGER.debug(
                "better_thermostat %s: loading model quirks: model='%s' trv='%s'",
                self.device_name,
                resolved_model,
                trv.get("trv"),
            )
            _model_quirks = await load_model_quirks(self, resolved_model, trv["trv"])
            try:
                mod_name = getattr(_model_quirks, "__name__", str(_model_quirks))
                _LOGGER.debug(
                    "better_thermostat %s: loaded model quirks module '%s' for model '%s' (trv %s)",
                    self.device_name,
                    mod_name,
                    resolved_model,
                    trv.get("trv"),
                )
            except (AttributeError, TypeError) as e:
                _LOGGER.debug(
                    "better_thermostat %s: could not determine quirks module name for model '%s' (trv %s): %s",
                    self.device_name,
                    resolved_model,
                    trv.get("trv"),
                    e,
                )
            self.real_trvs[trv["trv"]] = Trv(
                entity_id=trv["trv"],
                calibration=_calibration,
                integration=trv["integration"],
                adapter=_adapter,
                model_quirks=_model_quirks,
                model=resolved_model,
                advanced=_advanced,
            )

        def on_remove():
            self.is_removed = True
            self.kernel_state = replace(
                self.kernel_state, lifecycle=lifecycle_stop(self.kernel_state.lifecycle)
            )
            # flush() saves immediately; the Store cancels its own
            # pending delayed write when async_save runs.
            if self.state_mgr is not None:
                try:
                    self._record_runtime_to_state()
                    self.hass.async_create_background_task(
                        self.state_mgr.flush(),
                        name=f"bt_state_flush_{self.device_name}",
                    )
                except RuntimeError:
                    pass

        self.async_on_remove(on_remove)

        await super().async_added_to_hass()

        _LOGGER.info(
            "better_thermostat %s: Waiting for entity to be ready...", self.device_name
        )

        # Unified state persistence
        try:
            self.state_mgr = StateManager(self.hass, self._config_entry_id)
            await self.state_mgr.load()
            await migrate_v0_stores(
                self.hass,
                self.state_mgr,
                entity_prefix=f"{self._unique_id}:",
                config_entry_id=self._config_entry_id,
            )
            self._hydrate_thermal_from_state()
        except (FileNotFoundError, PermissionError, RuntimeError) as e:
            _LOGGER.debug(
                "better_thermostat %s: state storage init/load failed: %s",
                self.device_name,
                e,
            )

        @callback
        def _async_startup(*_):
            """Init on startup.

            Parameters
            ----------
            _ :
                    All parameters are piped.
            """
            self.context = Context()
            self.hass.async_create_background_task(
                self.startup(), name=f"better_thermostat_startup_{self.device_name}"
            )

        # Run after Home Assistant has finished starting (CoreState.running),
        # so dependent integrations like ZHA / MQTT have published their
        # entities and are ready to accept service calls. Runs immediately
        # if BT is added after HA is already up.
        self.async_on_remove(async_at_started(self.hass, _async_startup))

    async def _trigger_check_weather(self, event=None):
        # The degradation ladder advances first: it must keep stepping (e.g.
        # room sensor lost) even while an unavailable TRV aborts the trigger.
        await check_and_update_degraded_mode(self)
        _check = await check_critical_entities(self)
        if _check is False:
            return
        await check_weather(self)
        if self._last_call_for_heat != self.call_for_heat:
            self._last_call_for_heat = self.call_for_heat
            await self.async_update_ha_state(force_refresh=True)
            self.async_write_ha_state()
            if event is not None:
                request_control_cycle(self)

    async def _trigger_time(self, event=None):
        # The degradation ladder advances first: it must keep stepping (e.g.
        # room sensor lost) even while an unavailable TRV aborts the trigger.
        await check_and_update_degraded_mode(self)
        _check = await check_critical_entities(self)
        if _check is False:
            return
        if getattr(self, "in_maintenance", False):
            _LOGGER.debug(
                "better_thermostat %s: periodic tick skipped (valve maintenance running)",
                self.device_name,
            )
            return
        _LOGGER.debug(
            "better_thermostat %s: get last avg outdoor temps...", self.device_name
        )
        await check_ambient_air_temperature(self)
        self.async_write_ha_state()
        if event is not None:
            request_control_cycle(self)

    async def _trigger_outdoor_change(self, event=None):
        """Re-evaluate the outdoor-temperature threshold on sensor changes.

        The threshold is otherwise only refreshed at startup and the daily
        tick. Re-running the ambient check when the outdoor sensor changes
        lets heating react promptly. Control is only re-queued when
        ``call_for_heat`` actually flips, so frequent outdoor readings that
        stay on the same side of the threshold do not spam the queue.
        """
        # The degradation ladder advances first: it must keep stepping (e.g.
        # room sensor lost) even while an unavailable TRV aborts the trigger.
        await check_and_update_degraded_mode(self)
        _check = await check_critical_entities(self)
        if _check is False:
            return
        if getattr(self, "in_maintenance", False):
            return
        await check_ambient_air_temperature(self)
        if self._last_call_for_heat != self.call_for_heat:
            if not self.call_for_heat:
                await async_fire_logbook_entry(
                    self,
                    "summer_mode_on",
                    "turned off because the outdoor temperature is too high",
                )
            else:
                await async_fire_logbook_entry(
                    self,
                    "summer_mode_off",
                    "resumed heating because the outdoor temperature dropped",
                )

            self._last_call_for_heat = self.call_for_heat
            self.async_write_ha_state()
            if event is not None:
                request_control_cycle(self)

    async def _trigger_temperature_change(self, event):
        # The degradation ladder advances first: it must keep stepping (e.g.
        # room sensor lost) even while an unavailable TRV aborts the trigger.
        await check_and_update_degraded_mode(self)
        _check = await check_critical_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return
        self.hass.async_create_background_task(
            trigger_temperature_change(self, event),
            name=f"bt_trigger_temp_change_{self.device_name}",
        )

    async def _external_temperature_keepalive(self, event=None):
        """Re-send the external temperature regularly to the TRVs.

        Many devices expect an update at least every ~30 minutes.
        """
        try:
            cur = self.cur_temp
            if cur is None:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature keepalive skipped (cur_temp is None)",
                    self.device_name,
                )
                return

            # Use the known TRV entity IDs (keys in real_trvs)
            trv_ids = list(self.real_trvs.keys())
            # Fallback (normally should not be needed)
            if not trv_ids and hasattr(self, "entity_ids"):
                trv_ids = list(self.entity_ids or [])
            if not trv_ids:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature keepalive: no TRVs found",
                    self.device_name,
                )
                return
            else:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature keepalive: %d TRV(s) found",
                    self.device_name,
                    len(trv_ids),
                )

            for trv_id in trv_ids:
                try:
                    _mq_trv = (
                        self.real_trvs.get(trv_id)
                        if hasattr(self, "real_trvs")
                        else None
                    )
                    quirks = _mq_trv.model_quirks if _mq_trv is not None else None
                    if quirks and hasattr(quirks, "maybe_set_external_temperature"):
                        ok = await quirks.maybe_set_external_temperature(
                            self, trv_id, cur
                        )
                        _LOGGER.debug(
                            "better_thermostat %s: external_temperature keepalive sent to %s (ok=%s, value=%s)",
                            self.device_name,
                            trv_id,
                            ok,
                            cur,
                        )
                    else:
                        _LOGGER.debug(
                            "better_thermostat %s: no quirks with maybe_set_external_temperature for %s",
                            self.device_name,
                            trv_id,
                        )
                except OSError, RuntimeError, AttributeError, TypeError:
                    _LOGGER.debug(
                        "better_thermostat %s: external_temperature keepalive write failed for %s (non critical)",
                        self.device_name,
                        trv_id,
                    )
        except OSError, RuntimeError, AttributeError, TypeError:
            _LOGGER.debug(
                "better_thermostat %s: external_temperature keepalive encountered an error",
                self.device_name,
            )

    async def _trigger_humidity_change(self, event):
        # The degradation ladder advances first: it must keep stepping (e.g.
        # room sensor lost) even while an unavailable TRV aborts the trigger.
        await check_and_update_degraded_mode(self)
        _check = await check_critical_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return
        # Only update humidity if sensor is available
        if self.humidity_sensor_entity_id is not None and is_entity_available(
            self.hass, self.humidity_sensor_entity_id
        ):
            humidity_state = self.hass.states.get(self.humidity_sensor_entity_id)
            if humidity_state is not None:
                self._current_humidity = convert_to_float(
                    str(humidity_state.state), self.device_name, "humidity_update"
                )
        self.async_write_ha_state()

    async def _trigger_trv_change(self, event):
        # The degradation ladder advances first: it must keep stepping (e.g.
        # room sensor lost) even while an unavailable TRV aborts the trigger.
        await check_and_update_degraded_mode(self)
        _check = await check_critical_entities(self)
        if _check is False:
            return
        if getattr(self, "in_maintenance", False):
            _LOGGER.debug(
                "better_thermostat %s: TRV change skipped (valve maintenance running)",
                self.device_name,
            )
            return
        self.async_set_context(event.context)
        if self._async_unsub_state_changed is None:
            return

        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_background_task(
            trigger_trv_change(self, event),
            name=f"bt_trigger_trv_change_{self.device_name}",
        )

    async def _trigger_contact_change(self, event, trigger_fn, task_label):
        # The degradation ladder advances first: it must keep stepping (e.g.
        # room sensor lost) even while an unavailable TRV aborts the trigger.
        await check_and_update_degraded_mode(self)
        _check = await check_critical_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        # The window/door handler interprets unknown/unavailable readings
        # itself (a lost sensor counts as closed so heating resumes), so
        # events are dispatched regardless of sensor availability.
        self.hass.async_create_background_task(
            trigger_fn(self, event),
            name=f"bt_trigger_{task_label}_change_{self.device_name}",
        )

    async def _trigger_window_change(self, event):
        await self._trigger_contact_change(event, trigger_window_change, "window")

    async def _trigger_door_change(self, event):
        await self._trigger_contact_change(event, trigger_door_change, "door")

    async def _trigger_cooler_change(self, event):
        # The degradation ladder advances first: it must keep stepping (e.g.
        # room sensor lost) even while an unavailable TRV aborts the trigger.
        await check_and_update_degraded_mode(self)
        _check = await check_critical_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_background_task(
            trigger_cooler_change(self, event),
            name=f"bt_trigger_cooler_change_{self.device_name}",
        )

    def _set_trv_calibration_defaults(self, trv):
        """Set default calibration values for TRV."""
        if self.real_trvs[trv].last_calibration is None:
            self.real_trvs[trv].last_calibration = 0
        if self.real_trvs[trv].local_calibration_min is None:
            self.real_trvs[trv].local_calibration_min = -7
        if self.real_trvs[trv].local_calibration_max is None:
            self.real_trvs[trv].local_calibration_max = 7
        if self.real_trvs[trv].local_calibration_step is None:
            self.real_trvs[trv].local_calibration_step = 0.5

    async def startup(self) -> None:
        """Orchestrate entity startup."""
        # Start the critical-entity grace window at the very beginning so that
        # any availability check fired during the startup loop (or shortly
        # after) does not raise a premature ``missing_entity`` repair for a
        # slow-to-load underlying integration. The window is re-anchored in
        # ``_finalize_startup`` to cover post-startup reconnection blips.
        self._critical_grace_until = self.clock.now() + STARTUP_CRITICAL_GRACE_PERIOD
        # Arm the degraded-mode grace window before the first degraded check:
        # the startup loop below and the triggers in _finalize_startup all
        # call check_and_update_degraded_mode, whose warning suppression
        # reads the grace deadline from the lifecycle region.
        _arm_degraded_grace(self)
        while self.startup_running:
            if self.is_removed:
                return
            _LOGGER.info(
                "better_thermostat %s: Starting version %s. Waiting for entity to be ready...",
                self.device_name,
                self.version,
            )

            # The external room sensor is a required configuration field.
            if self.sensor_entity_id is None:
                _LOGGER.error(
                    "better_thermostat %s: no room temperature sensor configured "
                    "(the required 'temperature_sensor' option is missing from "
                    "the config entry); aborting startup, the entity stays "
                    "unavailable",
                    self.device_name,
                )
                return
            sensor_state = self.hass.states.get(self.sensor_entity_id)
            if not self._check_entities_ready(sensor_state):
                # This loop waits for as long as it takes, and nothing
                # downstream of it runs while it does, so without this call a
                # TRV that never comes back is never reported at all — while
                # one that disappears after startup is. The critical check
                # owns the rule for when waiting turns into reporting and
                # stays quiet for the length of the grace window.
                await check_critical_entities(self)
                await asyncio.sleep(20)
                if self.is_removed:
                    return
                continue

            states = self._collect_trv_states()
            self._resolve_temperature_range(states)
            self._initialize_sensors(sensor_state)
            await check_and_update_degraded_mode(self)
            await self._restore_state(states)
            # The awaits above yield to the event loop, so the entity may have
            # been removed in the meantime; bail out before writing to TRVs.
            if self.is_removed:
                return
            # A restored preset carries a cooling target the user chose, so the
            # cooler's own setpoint only fills a target that is still unknown.
            # Both the temperature range and the heating target are final at
            # this point, which is what a value read off the device has to be
            # clamped into and ordered against.
            self._seed_cool_target_from_cooler("startup()")
            self._validate_hvac_mode(states)
            await self._initialize_trvs()
            await self._finalize_startup()
            break

    def _check_entities_ready(self, sensor_state: State | None) -> bool:
        """Check whether sensor and all TRVs are available.

        Returns True when every entity is ready, False otherwise.
        """
        if sensor_state is None or sensor_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            None,
        ):
            _LOGGER.info(
                "better_thermostat %s: waiting for sensor entity with id '%s' to become fully available...",
                self.device_name,
                self.sensor_entity_id,
            )
            return False

        for trv in self.real_trvs:
            trv_state = self.hass.states.get(trv)
            if trv_state is None or trv_state.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                None,
            ):
                _LOGGER.info(
                    "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                    self.device_name,
                    trv,
                )
                return False
        return True

    def _collect_trv_states(self) -> list[State]:
        """Collect current State objects for all TRVs and optional cooler."""
        states = [
            state
            for entity_id in self.real_trvs
            if (state := self.hass.states.get(entity_id)) is not None
        ]

        # Include cooler entity in min/max calculation to ensure BT's
        # temperature range is compatible with all controlled devices
        if self.cooler_entity_id is not None:
            cooler_state = self.hass.states.get(self.cooler_entity_id)
            if cooler_state is not None and cooler_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                None,
            ):
                states.append(cooler_state)

        return states

    def _resolve_temperature_range(self, states: list[State]) -> None:
        """Derive min/max/step temperature from TRV states."""
        # Convert each child's min/max to Celsius before reducing, because
        # children may report in Fahrenheit while BT works internally in °C.
        min_temps: list[float] = []
        max_temps: list[float] = []
        steps: list[float] = []
        for s in states:
            _unit = state_temperature_unit(
                s.attributes, self.hass.config.units.temperature_unit
            )
            _raw_min = s.attributes.get(ATTR_MIN_TEMP)
            if _raw_min is not None:
                _c = convert_to_float_celsius(
                    str(_raw_min),
                    self.device_name,
                    "_resolve_temperature_range(min)",
                    unit_of_measurement=_unit,
                )
                if _c is not None:
                    min_temps.append(_c)
            _raw_max = s.attributes.get(ATTR_MAX_TEMP)
            if _raw_max is not None:
                _c = convert_to_float_celsius(
                    str(_raw_max),
                    self.device_name,
                    "_resolve_temperature_range(max)",
                    unit_of_measurement=_unit,
                )
                if _c is not None:
                    max_temps.append(_c)
            _sf = _target_temp_step_celsius(
                s, self.device_name, self.hass.config.units.temperature_unit
            )
            if _sf is not None:
                steps.append(_sf)
        self.bt_min_temp = max(min_temps) if min_temps else None
        self.bt_max_temp = min(max_temps) if max_temps else None

        if (
            self.bt_min_temp is not None
            and self.bt_max_temp is not None
            and self.bt_min_temp > self.bt_max_temp
        ):
            _LOGGER.warning(
                "better_thermostat %s: min temp (%.1f°) > max temp (%.1f°). "
                "This indicates non-overlapping temperature ranges between "
                "heater and cooler entities. Please check your configuration.",
                self.device_name,
                self.bt_min_temp,
                self.bt_max_temp,
            )

        if self.bt_target_temp_step is None:
            self.bt_target_temp_step = max(steps) if steps else None

    def _initialize_sensors(self, sensor_state: State | None) -> None:
        """Set up room temperature, humidity, window and door sensors."""
        self.all_entities.append(self.sensor_entity_id)

        # Handle room temperature sensor with TRV fallback
        room_candidate: float | None = None
        if sensor_state is not None and sensor_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            None,
        ):
            room_candidate = convert_to_float_celsius(
                str(sensor_state.state),
                self.device_name,
                "startup()",
                unit_of_measurement=sensor_state.attributes.get("unit_of_measurement"),
            )
            if not is_reasonable_temperature(room_candidate):
                _LOGGER.warning(
                    "better_thermostat %s: Room temperature sensor '%s' reports "
                    "implausible value %s; falling back to TRV internal temperature.",
                    self.device_name,
                    self.sensor_entity_id,
                    room_candidate,
                )
                room_candidate = None

        if room_candidate is not None:
            self.cur_temp = room_candidate
        else:
            if sensor_state is None or sensor_state.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                None,
            ):
                _LOGGER.warning(
                    "better_thermostat %s: Room temperature sensor '%s' unavailable. "
                    "Falling back to TRV internal temperature.",
                    self.device_name,
                    self.sensor_entity_id,
                )
            self.cur_temp = None
            for trv_id in self.real_trvs:
                trv_state = self.hass.states.get(trv_id)
                if trv_state is None:
                    continue
                trv_temp = trv_state.attributes.get("current_temperature")
                if trv_temp is None:
                    continue
                candidate = attr_to_celsius(
                    self,
                    trv_state,
                    "current_temperature",
                    None,
                    "startup() TRV fallback",
                )
                if not is_reasonable_temperature(candidate):
                    _LOGGER.warning(
                        "better_thermostat %s: TRV '%s' reports implausible "
                        "current_temperature %s; trying next TRV.",
                        self.device_name,
                        trv_id,
                        candidate,
                    )
                    continue
                self.cur_temp = candidate
                _LOGGER.info(
                    "better_thermostat %s: Using TRV '%s' temperature: %.1f°C",
                    self.device_name,
                    trv_id,
                    candidate,
                )
                break
            if self.cur_temp is None:
                self.cur_temp = DEFAULT_FALLBACK_TEMPERATURE
                _LOGGER.warning(
                    "better_thermostat %s: No temperature available, using default %.1f°C",
                    self.device_name,
                    DEFAULT_FALLBACK_TEMPERATURE,
                )

        # Initialize EMA with current temperature at startup
        if self.cur_temp is not None:
            self.last_known_external_temp = self.cur_temp
            try:
                _update_external_temp_ema(self, float(self.cur_temp))
                _LOGGER.debug(
                    "better_thermostat %s: initialized external_temp_ema at startup with %.2f",
                    self.device_name,
                    self.cur_temp,
                )
            except (ValueError, TypeError) as e:
                _LOGGER.warning(
                    "better_thermostat %s: failed to initialize external_temp_ema at startup: %s",
                    self.device_name,
                    e,
                )

        if self.humidity_sensor_entity_id is not None:
            self.all_entities.append(self.humidity_sensor_entity_id)
            _hum_state = self.hass.states.get(self.humidity_sensor_entity_id)
            if _hum_state is not None and _hum_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                None,
            ):
                self._current_humidity = (
                    convert_to_float(
                        str(_hum_state.state), self.device_name, "startup()"
                    )
                    or 0.0
                )
            # else: already logged warning above, _current_humidity stays None

        # Seed the window and door regions from the sensors' startup state.
        # A non-active sensor (unavailable/unknown) is treated as closed so
        # heating continues; the contacts are usually closed and a lost
        # sensor must not stop heating. Runtime handling matches this (see
        # events/window.py and events/door.py).
        self.kernel_state = replace(
            self.kernel_state,
            window=_seed_contact_region_at_startup(self, self.window_id, "window"),
            door=_seed_contact_region_at_startup(self, self.door_id, "door"),
        )

    async def _restore_state(self, states: list[State]) -> None:
        """Restore previous state from HA state machine or fall back to defaults."""
        _LOGGER.debug(
            "better_thermostat %s: calling async_get_last_state", self.device_name
        )
        old_state = await self.async_get_last_state()
        _LOGGER.debug(
            "better_thermostat %s: async_get_last_state returned", self.device_name
        )
        if old_state is not None:
            _LOGGER.debug("better_thermostat %s: restoring state...", self.device_name)
            # Migration fallback: read the filter state from the last
            # entity attributes only when the unified store has none.
            _store_filters = self.state_mgr.filters if self.state_mgr else None
            if (
                _store_filters is None or _store_filters.external_temp_ema is None
            ) and "external_temp_ema" in old_state.attributes:
                try:
                    _restored_ema = float(old_state.attributes["external_temp_ema"])
                    self.external_temp_ema = _restored_ema
                    self.cur_temp_filtered = round(_restored_ema, 2)
                    # Reset timestamp to now so the next delta is calculated from restart time
                    self._external_temp_ema_ts = self.clock.monotonic()
                    _LOGGER.debug(
                        "better_thermostat %s: restored external_temp_ema from state: %.2f",
                        self.device_name,
                        _restored_ema,
                    )
                except ValueError, TypeError:
                    pass

            if (
                _store_filters is None or _store_filters.temp_slope is None
            ) and "temp_slope_K_min" in old_state.attributes:
                try:
                    _restored_slope = float(old_state.attributes["temp_slope_K_min"])
                    self.temp_slope = _restored_slope
                    _LOGGER.debug(
                        "better_thermostat %s: restored temp_slope from state: %.4f",
                        self.device_name,
                        _restored_slope,
                    )
                except ValueError, TypeError:
                    pass

            _LOGGER.debug(
                "better_thermostat %s: restoring target temperature...",
                self.device_name,
            )
            # Clamp the saved target, or fall back to the TRV mean.
            _restored_target = restore_target_temperature(
                old_state.attributes.get(ATTR_TEMPERATURE),
                states,
                self.bt_min_temp,
                self.bt_max_temp,
                self.device_name,
                self.hass.config.units.temperature_unit,
            )
            if _restored_target is not None:
                self.bt_target_temp = _restored_target
            _LOGGER.debug(
                "better_thermostat %s: target temperature restored", self.device_name
            )

            _LOGGER.debug(
                "better_thermostat %s: restoring preset mode...", self.device_name
            )
            # Restore preset mode if present
            _old_preset = old_state.attributes.get("preset_mode")
            if (
                isinstance(_old_preset, str)
                and _old_preset in self.preset_mgr.available_modes
            ):
                self.preset_mgr.mode = _old_preset
            else:
                self.preset_mgr.mode = PRESET_NONE

            _LOGGER.debug(
                "better_thermostat %s: applying restored preset temperature...",
                self.device_name,
            )
            # Restore the persisted per-preset cooling map before applying it below,
            # so a restored preset uses its saved cooling target instead of the default.
            if (
                old_state.attributes.get(ATTR_STATE_PRESET_COOL_TEMPERATURE, None)
                is not None
            ):
                self._preset_cool_temperature = convert_to_float(
                    str(
                        old_state.attributes.get(
                            ATTR_STATE_PRESET_COOL_TEMPERATURE, None
                        )
                    ),
                    self.device_name,
                    "startup()",
                )
            if (
                old_state.attributes.get(ATTR_STATE_PRESET_COOL_TEMPERATURES, None)
                is not None
            ):
                try:
                    restored_cool_temperatures = json.loads(
                        str(
                            old_state.attributes.get(
                                ATTR_STATE_PRESET_COOL_TEMPERATURES, "{}"
                            )
                        )
                    )
                except TypeError, json.JSONDecodeError:
                    _LOGGER.debug(
                        "better_thermostat %s: could not restore preset cool temperatures",
                        self.device_name,
                    )
                else:
                    if isinstance(restored_cool_temperatures, dict):
                        for preset, temp in restored_cool_temperatures.items():
                            if preset not in self._preset_cool_temperatures:
                                continue
                            cool_temp = convert_to_float(
                                str(temp), self.device_name, "startup()"
                            )
                            if cool_temp is not None:
                                self._preset_cool_temperatures[preset] = cool_temp
            # If we restored a preset (not NONE) and we have a stored temperature for it,
            # ensure target temp matches (unless the restored target was already equal).
            if self.preset_mgr.mode is not None and self.preset_mgr.mode != PRESET_NONE:
                preset_temp = self.preset_mgr.get_temperature(self.preset_mgr.mode)
                # Only override if different to avoid masking manual restore logic
                if (
                    isinstance(preset_temp, (int, float))
                    and preset_temp is not None
                    and self.bt_target_temp != preset_temp
                ):
                    _LOGGER.debug(
                        "better_thermostat %s: Applying restored preset %s temperature %s after startup",
                        self.device_name,
                        self.preset_mgr.mode,
                        preset_temp,
                    )
                    self.bt_target_temp = self._bound_target_to_range(preset_temp)
                if (
                    self.cooler_entity_id is not None
                    and self.preset_mgr.mode in self._preset_cool_temperatures
                ):
                    cool_temp = self._preset_cool_temperatures[self.preset_mgr.mode]
                    if isinstance(cool_temp, (int, float)):
                        self.bt_target_cooltemp = self._bound_target_to_range(cool_temp)
                # A target that is re-injected rather than chosen is ordered the
                # moment it is stored: the HVAC mode can change without the pair
                # being looked at again, and async_set_hvac_mode does not
                # re-enforce the ordering.
                if self.cooler_entity_id is not None:
                    self._enforce_cool_above_heat(regardless_of_hvac_mode=True)
            _LOGGER.debug(
                "better_thermostat %s: restored preset temperature applied",
                self.device_name,
            )

            _LOGGER.debug(
                "better_thermostat %s: restoring other attributes...", self.device_name
            )
            if old_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                try:
                    self.bt_hvac_mode = HVACMode(old_state.state)
                except ValueError:
                    _LOGGER.warning(
                        "better_thermostat %s: restored an unrecognised hvac mode %s; "
                        "leaving it for validation",
                        self.device_name,
                        old_state.state,
                    )
            # call_for_heat and humidity are observations, not UI state:
            # they are rebuilt from live data within the first cycles, so
            # they are deliberately not restored from entity attributes.
            if old_state.attributes.get(ATTR_STATE_SAVED_TEMPERATURE, None) is not None:
                self._saved_temperature = convert_to_float(
                    str(old_state.attributes.get(ATTR_STATE_SAVED_TEMPERATURE, None)),
                    self.device_name,
                    "startup()",
                )
            if old_state.attributes.get(ATTR_STATE_MAIN_MODE, None) is not None:
                self.last_main_hvac_mode = str(
                    old_state.attributes[ATTR_STATE_MAIN_MODE]
                )

            # Learned values: the StateManager is the persistence
            # authority. The restored attributes only fill in when the
            # store carries nothing — a one-time migration fallback for
            # upgrades from versions that persisted via RestoreEntity.
            _stored_power, _stored_loss = (
                self.state_mgr.clamped_thermal()
                if self.state_mgr is not None
                else (None, None)
            )
            if (
                _stored_power is None
                and old_state.attributes.get(ATTR_STATE_HEATING_POWER, None) is not None
            ):
                self.heating_power = clamp_heating_power(
                    old_state.attributes.get(ATTR_STATE_HEATING_POWER), self.device_name
                )
            if (
                _stored_loss is None
                and old_state.attributes.get(ATTR_STATE_HEAT_LOSS, None) is not None
            ):
                _restored_loss = clamp_heat_loss(
                    old_state.attributes.get(ATTR_STATE_HEAT_LOSS)
                )
                if _restored_loss is not None:
                    self.heat_loss_rate = _restored_loss
            if (
                old_state.attributes.get(ATTR_STATE_PRESET_TEMPERATURE, None)
                is not None
            ):
                self.preset_mgr.saved_temperature = convert_to_float(
                    str(old_state.attributes.get(ATTR_STATE_PRESET_TEMPERATURE, None)),
                    self.device_name,
                    "startup()",
                )
            # Restore preset mode
            if old_state.attributes.get("preset_mode", None) is not None:
                restored_preset: str = str(old_state.attributes["preset_mode"])
                if restored_preset in self.preset_modes:
                    self.preset_mgr.mode = restored_preset
                    _LOGGER.debug(
                        "better_thermostat %s: Restored preset mode: %s",
                        self.device_name,
                        restored_preset,
                    )
            _LOGGER.debug(
                "better_thermostat %s: state restoration completed", self.device_name
            )

            # ECO mode state / saved ECO temperature not restored; Eco preset is supported via PRESET_ECO.

        else:
            # No previous state, try and restore defaults
            _LOGGER.debug(
                "better_thermostat %s: no previous state, restoring defaults...",
                self.device_name,
            )
            if self.bt_target_temp is None or not isinstance(
                self.bt_target_temp, float
            ):
                _LOGGER.info(
                    "better_thermostat %s: No previously saved temperature found on startup, get it from the TRV",
                    self.device_name,
                )
                _restored_target = mean_trv_target(
                    states,
                    self.device_name,
                    system_unit=self.hass.config.units.temperature_unit,
                )
                if _restored_target is not None:
                    self.bt_target_temp = _restored_target
            _LOGGER.debug("better_thermostat %s: defaults restored", self.device_name)

    def _validate_hvac_mode(self, states: list[State]) -> None:
        """Validate and fix HVAC mode after state restoration."""
        # if hvac mode could not be restored, turn heat off
        _LOGGER.debug("better_thermostat %s: checking hvac mode...", self.device_name)
        if self.bt_hvac_mode in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            # OFF is filtered out, so a non-empty list means at least one child
            # is running -> adopt HEAT; otherwise (all OFF or none) stay OFF.
            current_hvac_modes = [x.state for x in states if x.state != HVACMode.OFF]
            if current_hvac_modes:
                self.bt_hvac_mode = HVACMode.HEAT
            else:
                self.bt_hvac_mode = HVACMode.OFF
            _LOGGER.debug(
                "better_thermostat %s: No previously hvac mode found on startup, turn bt to trv mode %s",
                self.device_name,
                self.bt_hvac_mode,
            )

        _LOGGER.debug(
            "better_thermostat %s: Startup config, BT hvac mode is %s, Target temp %s",
            self.device_name,
            self.bt_hvac_mode,
            self.bt_target_temp,
        )

        if self.last_main_hvac_mode is None:
            self.last_main_hvac_mode = self.bt_hvac_mode

        _LOGGER.debug(
            "better_thermostat %s: checking humidity sensor...", self.device_name
        )
        if self.humidity_sensor_entity_id is not None:
            _hum_state = self.hass.states.get(self.humidity_sensor_entity_id)
            if _hum_state is None:
                _LOGGER.warning(
                    "better_thermostat %s: Humidity sensor %s not found or not ready",
                    self.device_name,
                    self.humidity_sensor_entity_id,
                )
                self._current_humidity = 0
            else:
                self._current_humidity = (
                    convert_to_float(
                        str(_hum_state.state), self.device_name, "startup()"
                    )
                    or 0.0
                )
        else:
            self._current_humidity = 0.0

        if self.bt_hvac_mode not in (HVACMode.OFF, HVACMode.HEAT_COOL, HVACMode.HEAT):
            self.bt_hvac_mode = HVACMode.HEAT

        _LOGGER.debug(
            "better_thermostat %s: writing initial state...", self.device_name
        )
        self.async_write_ha_state()

    async def _initialize_trvs(self) -> None:
        """Initialize each TRV: init, tweak, calibration offsets, attributes, control."""
        for trv, trv_data in self.real_trvs.items():
            self.all_entities.append(trv)
            _LOGGER.debug(
                "better_thermostat %s: initializing TRV %s", self.device_name, trv
            )
            try:
                await asyncio.wait_for(init(self, trv), timeout=30)
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s initialized", self.device_name, trv
                )
            except TimeoutError:
                _LOGGER.error(
                    "better_thermostat %s: Timeout initializing TRV %s",
                    self.device_name,
                    trv,
                )
            except Exception as exc:
                _LOGGER.error(
                    "better_thermostat %s: Error initializing TRV %s: %s",
                    self.device_name,
                    trv,
                    exc,
                )

            try:
                await initial_tweak(self, trv)
            except Exception as exc:
                _LOGGER.error(
                    "better_thermostat %s: Error running initial tweak for TRV %s: %s",
                    self.device_name,
                    trv,
                    exc,
                )

            if trv_data.calibration != 1:
                _LOGGER.debug(
                    "better_thermostat %s: getting offsets for TRV %s",
                    self.device_name,
                    trv,
                )

                try:
                    async with asyncio.timeout(10):
                        trv_data.last_calibration = await get_current_offset(self, trv)
                        trv_data.local_calibration_min = await get_min_offset(self, trv)
                        trv_data.local_calibration_max = await get_max_offset(self, trv)
                        trv_data.local_calibration_step = await get_offset_step(
                            self, trv
                        )
                    # Ensure None values are replaced with sensible defaults
                    self._set_trv_calibration_defaults(trv)
                    _LOGGER.debug(
                        "better_thermostat %s: offsets for TRV %s retrieved",
                        self.device_name,
                        trv,
                    )
                except TimeoutError:
                    _LOGGER.error(
                        "better_thermostat %s: Timeout getting offsets for TRV %s",
                        self.device_name,
                        trv,
                    )
                    self._set_trv_calibration_defaults(trv)
                except Exception as exc:
                    _LOGGER.error(
                        "better_thermostat %s: Error getting offsets for TRV %s: %s",
                        self.device_name,
                        trv,
                        exc,
                    )
                    self._set_trv_calibration_defaults(trv)
            else:
                trv_data.last_calibration = 0
                trv_data.local_calibration_min = -7
                trv_data.local_calibration_max = 7
                trv_data.local_calibration_step = 0.5

            _s = self.hass.states.get(trv)
            _attrs = _s.attributes if _s else {}
            _LOGGER.debug(
                "better_thermostat %s: reading TRV %s attributes...",
                self.device_name,
                trv,
            )
            trv_data.valve_position = convert_to_float(
                str(_attrs.get("valve_position", None)), self.device_name, "startup"
            )
            trv_data.max_temp = attr_to_celsius(self, _s, "max_temp", 30, "startup")
            trv_data.min_temp = attr_to_celsius(self, _s, "min_temp", 5, "startup")
            # This step is the grid the device rounds to: it sizes the echo
            # window for inbound setpoints and the rounding of outbound ones,
            # so it must be this device's own step and not the coarsest step
            # across all children in ``bt_target_temp_step``. An explicitly
            # configured step still overrides the device, and the aggregate
            # only fills in for a device that publishes no usable step.
            _device_step = _target_temp_step_celsius(
                _s, self.device_name, self.hass.config.units.temperature_unit
            )
            if self._configured_target_temp_step is not None:
                trv_data.target_temp_step = self._configured_target_temp_step
            elif _device_step is not None and _device_step > 0.0:
                trv_data.target_temp_step = _device_step
            elif self.bt_target_temp_step and self.bt_target_temp_step > 0.0:
                trv_data.target_temp_step = self.bt_target_temp_step
            else:
                trv_data.target_temp_step = 0.5
            trv_data.temperature = attr_to_celsius(
                self, _s, "temperature", 5, "startup"
            )
            trv_data.hvac_modes = _attrs.get("hvac_modes", None)
            trv_data.hvac_mode = _s.state if _s else None
            trv_data.last_hvac_mode = _s.state if _s else None
            trv_data.last_temperature = attr_to_celsius(
                self, _s, "temperature", None, "startup()"
            )
            # No reading is no reading: a fabricated value would feed
            # SENSOR_FALLBACK as if it were live and keep the ladder's
            # HOLD rung unreachable.
            _raw_current_temp = _attrs.get("current_temperature")
            _current_temp = (
                convert_to_float_celsius(
                    str(_raw_current_temp),
                    self.device_name,
                    "startup()",
                    unit_of_measurement=state_temperature_unit(
                        _attrs, self.hass.config.units.temperature_unit
                    ),
                )
                if _raw_current_temp is not None
                else None
            )
            # Marker / garbage readings (for example AVM's 126.5 / 127 °C)
            # must not seed the cache and feed the first control cycle.
            if _current_temp is not None and not is_reasonable_temperature(
                _current_temp
            ):
                _LOGGER.warning(
                    "better_thermostat %s: TRV %s reports implausible "
                    "current_temperature %s at startup; ignoring",
                    self.device_name,
                    trv,
                    _current_temp,
                )
                _current_temp = None
            trv_data.current_temperature = _current_temp

    async def _startup_control_trvs(self) -> None:
        """Write the initial mode/setpoint/calibration to every TRV.

        Must run after the lifecycle gate has opened: while startup is
        running, decide() addresses no TRVs and a control cycle writes
        nothing. One observation and decision covers every TRV; without
        it each call would build its own snapshot and recorder entry.
        """
        try:
            cycle = compute_control_cycle(self)
        except Exception:
            _LOGGER.exception(
                "better_thermostat %s: ERROR computing the startup control cycle",
                self.device_name,
            )
            cycle = None
        for trv in self.real_trvs:
            _LOGGER.debug(
                "better_thermostat %s: controlling TRV %s...", self.device_name, trv
            )
            try:
                await asyncio.wait_for(control_trv(self, trv, cycle=cycle), timeout=10)
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s controlled", self.device_name, trv
                )
            except TimeoutError:
                _LOGGER.error(
                    "better_thermostat %s: Timeout controlling TRV %s",
                    self.device_name,
                    trv,
                )
            except Exception as exc:
                _LOGGER.error(
                    "better_thermostat %s: Error controlling TRV %s: %s",
                    self.device_name,
                    trv,
                    exc,
                )

    async def _post_grace_recheck(
        self,
        grace_until: datetime | None,
        recheck: Callable[[BetterThermostat], Awaitable[bool]],
    ) -> None:
        """Re-run an availability check once a startup grace window elapses.

        During a startup grace window, availability checks defer their
        Home Assistant repair issue. This helper sleeps for the remaining
        grace time (if any) and runs the check again, so an entity that is
        still unavailable after the window surfaces a repair issue without
        waiting for the next unrelated trigger.

        Parameters
        ----------
        grace_until : datetime | None
            End of the grace window; ``None`` or a past instant runs the
            recheck immediately.
        recheck : Callable[[BetterThermostat], Awaitable[bool]]
            Availability check coroutine function, invoked with this
            thermostat instance.
        """
        remaining = (
            (grace_until - self.clock.now()).total_seconds() if grace_until else 0.0
        )
        if remaining > 0:
            await asyncio.sleep(remaining)
        if self.is_removed:
            return
        try:
            await recheck(self)
        except Exception:
            _LOGGER.warning(
                "better_thermostat %s: post-grace availability recheck failed",
                self.device_name,
                exc_info=True,
            )

    async def _finalize_startup(self) -> None:
        """Run post-init tasks: triggers, listeners, periodic jobs."""
        # The degraded-mode grace window is normally armed at the top of
        # startup(); arming here as well keeps the deadline in place before
        # the triggers below run their degraded checks.
        _arm_degraded_grace(self)
        # Likewise give critical (TRV) entities a short grace before raising
        # ``missing_entity`` repairs; cloud-backed valves (Tado, etc.) can lag
        # behind HA startup and would otherwise produce dismissable noise.
        self._critical_grace_until = self.clock.now() + STARTUP_CRITICAL_GRACE_PERIOD
        # Wait for critical entities (TRVs) with increasing retry delays before
        # any startup path can raise a missing_entity repair issue.  Both
        # _trigger_time and _trigger_check_weather below call
        # check_critical_entities internally, so the retry must complete first.
        # Cloud-backed valves (e.g. Tado) often initialise later than Home
        # Assistant itself; without this wait a single immediate check reports a
        # false-positive that lingers in the repair dashboard even after the
        # valve comes online.
        await await_critical_entities(self)
        if self.is_removed:
            return
        _LOGGER.debug(
            "better_thermostat %s: checking critical entities...", self.device_name
        )
        await check_critical_entities(self)

        # The retry schedule above finishes before the critical grace window
        # ends, so a TRV that is still missing defers its repair issue. Re-run
        # the check once the grace window has elapsed; otherwise the issue
        # only appears when some later event happens to trigger a check.
        self.hass.async_create_background_task(
            self._post_grace_recheck(
                self._critical_grace_until, check_critical_entities
            ),
            name=f"bt_post_grace_critical_{self.device_name}",
        )

        _LOGGER.debug("better_thermostat %s: triggering time...", self.device_name)
        await self._trigger_time(None)
        _LOGGER.debug(
            "better_thermostat %s: triggering check weather...", self.device_name
        )
        await self._trigger_check_weather(None)
        _LOGGER.debug("better_thermostat %s: startup finishing...", self.device_name)
        # The transition to STARTING carries the degraded-grace deadline that
        # was armed when startup began; the first control cycle right below
        # already runs a lifecycle tick, which promotes STARTING to RUNNING
        # as soon as no grace deadline is set.
        self.kernel_state = replace(
            self.kernel_state,
            lifecycle=lifecycle_startup_finished(
                self.kernel_state.lifecycle, grace_until=self._degraded_grace_until
            ),
        )
        await self._startup_control_trvs()
        self._available = True
        self.async_write_ha_state()

        if isinstance(self.all_trvs, list):
            # via_device is single-valued: binding every TRV rewrites the same
            # BT device row, leaving it attached only to the last valve. Only
            # bind when there is exactly one TRV; skip for multi-TRV setups.
            trv_ids = [
                trv_conf.get("trv") for trv_conf in self.all_trvs if trv_conf.get("trv")
            ]
            if len(trv_ids) == 1:
                await async_bind_trv_device(
                    self.hass, self._unique_id, trv_ids[0], self._config_entry_id
                )
            elif len(trv_ids) > 1:
                _LOGGER.debug(
                    "better_thermostat %s: skipping via_device binding for multi-TRV setup",
                    self.device_name,
                )
                # A via_device link written while the setup had (or was
                # treated as having) a single valve would keep the BT device
                # attached to one arbitrary TRV; clear it.
                await async_unbind_trv_device(self.hass, self._unique_id)

        _LOGGER.debug("better_thermostat %s: sleeping 15s...", self.device_name)
        await asyncio.sleep(15)
        _LOGGER.debug(
            "better_thermostat %s: finding battery entities...", self.device_name
        )

        # The battery scan below reads all_entities, so every configured
        # device has to be registered before it runs. The cooler and the
        # outdoor sensor are the two that no earlier init step registers.
        if self.cooler_entity_id is not None:
            self.all_entities.append(self.cooler_entity_id)
        if self.outdoor_sensor is not None:
            self.all_entities.append(self.outdoor_sensor)

        # try to find battery entities for all related entities
        for entity in self.all_entities:
            if entity is not None:
                battery_id = await find_battery_entity(self, entity)
                if battery_id is not None:
                    self.devices_states[entity] = {
                        "battery_id": battery_id,
                        "battery": None,
                    }

        if self.is_removed:
            return

        # Add listener
        if self.outdoor_sensor is not None:
            self.async_on_remove(
                async_track_time_change(self.hass, self._trigger_time, 5, 0, 0)
            )

        # Wait for optional sensors with increasing retry delays before
        # entering degraded mode (see await_optional_sensors for details).
        # During the startup grace window (armed above, carried by the
        # lifecycle region), a transition into degraded mode is logged at
        # DEBUG and the HA repair issue is deferred — slow cloud integrations
        # get time to come online before the user sees a warning.
        await await_optional_sensors(self)
        await check_and_update_degraded_mode(self)

        self.hass.async_create_background_task(
            self._post_grace_recheck(
                self._degraded_grace_until, check_and_update_degraded_mode
            ),
            name=f"bt_post_grace_degraded_{self.device_name}",
        )

        if self.is_removed:
            return

        _LOGGER.debug(
            "better_thermostat %s: registering periodic tasks...", self.device_name
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._trigger_check_weather, timedelta(hours=1)
            )
        )

        # Periodic 5-minute tick: only enable when balance is configured
        balance_modes = {"heuristic", "pid"}
        active_balance_modes = set()
        active_calibration_modes = set()
        try:
            for trv_info in self.real_trvs.values():
                advanced = trv_info.advanced or {}

                raw_balance = advanced.get("balance_mode", "")
                balance_value = getattr(raw_balance, "value", raw_balance)
                if isinstance(balance_value, str):
                    balance_mode = balance_value.lower()
                    if balance_mode in balance_modes:
                        active_balance_modes.add(balance_mode)

                raw_calibration = advanced.get("calibration_mode", "")
                calibration_value = getattr(raw_calibration, "value", raw_calibration)
                if isinstance(calibration_value, str):
                    calibration_mode = calibration_value.lower()
                    if calibration_mode in (
                        CalibrationMode.DEFAULT.value,
                        CalibrationMode.MPC_CALIBRATION.value,
                        CalibrationMode.MPC_V2_CALIBRATION.value,
                        CalibrationMode.TPI_CALIBRATION.value,
                        CalibrationMode.PID_CALIBRATION.value,
                    ):
                        active_calibration_modes.add(calibration_mode)
        except Exception:
            active_balance_modes = set()
            active_calibration_modes = set()

        if active_balance_modes or active_calibration_modes:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._trigger_time, timedelta(minutes=5)
                )
            )
            _LOGGER.debug(
                "better_thermostat %s: 5min periodic tick enabled (balance_modes=%s calibration_modes=%s)",
                self.device_name,
                sorted(active_balance_modes),
                sorted(active_calibration_modes),
            )
        else:
            _LOGGER.debug(
                "better_thermostat %s: 5min periodic tick skipped (no supported balance/calibration mode)",
                self.device_name,
            )

        # Valve maintenance is orthogonal to balance/calibration: enable its
        # tick whenever at least one TRV has it turned on.
        try:
            maint_trvs = collect_maintenance_trvs(self.real_trvs)
        except Exception:
            maint_trvs = []

        if maint_trvs:
            self.next_valve_maintenance = compute_initial_maintenance(
                self.real_trvs, maint_trvs
            )
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._maintenance_tick, timedelta(minutes=5)
                )
            )
            _LOGGER.debug(
                "better_thermostat %s: valve maintenance tick enabled (5min), first run at %s",
                self.device_name,
                self.next_valve_maintenance,
            )
        else:
            _LOGGER.debug(
                "better_thermostat %s: valve maintenance tick skipped (no TRV enabled)",
                self.device_name,
            )

        # The external room sensor is a required configuration field.
        if self.sensor_entity_id is None:
            _LOGGER.error(
                "better_thermostat %s: no room temperature sensor configured "
                "(the required 'temperature_sensor' option is missing from "
                "the config entry); skipping listener registration",
                self.device_name,
            )
            return
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.sensor_entity_id], self._trigger_temperature_change
            )
        )
        if self.humidity_sensor_entity_id is not None:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self.humidity_sensor_entity_id],
                    self._trigger_humidity_change,
                )
            )
        if self._async_unsub_state_changed is None:
            self._async_unsub_state_changed = async_track_state_change_event(
                self.hass, self.entity_ids, self._trigger_trv_change
            )
            self.async_on_remove(self._async_unsub_state_changed)
        if self.window_id is not None:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.window_id], self._trigger_window_change
                )
            )
        if self.door_id is not None:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.door_id], self._trigger_door_change
                )
            )
        if self.cooler_entity_id is not None:
            _shared_entity_id = dual_role_entity_id(self)
            if _shared_entity_id is None:
                self.async_on_remove(
                    async_track_state_change_event(
                        self.hass, [self.cooler_entity_id], self._trigger_cooler_change
                    )
                )
            else:
                # A device that carries both roles is already tracked as a
                # thermostat, and one device reporting into two handlers means
                # each handler reads the other channel's write as a user press.
                # The TRV handler is the one that survives: it is the only
                # reader of the device's internal temperature, its model
                # quirks, its valve and its mode, and it files a reported
                # setpoint under whichever channel drives the device.
                _LOGGER.info(
                    "better_thermostat %s: %s is configured as both the "
                    "thermostat and the cooler; one channel drives it per "
                    "cycle and its reports are handled as a thermostat's",
                    self.device_name,
                    _shared_entity_id,
                )
            # A cool target still unknown here means the earlier read of the
            # cooler setpoint yielded nothing: the cooler published no state
            # yet, or the state it published carried no readable setpoint. An
            # unknown cool target holds control_cooler() at OFF on every cycle,
            # and the event handler only ever sees a cooler that changes state
            # again. The subscription above is live from this point on, so this
            # is the last moment a state that never changes again can still be
            # read. It names itself rather than the startup it runs under, so a
            # conversion this read cannot complete is not attributed to the
            # earlier one.
            if (
                self._seed_cool_target_from_cooler("_finalize_startup()")
                and self.bt_hvac_mode != HVACMode.OFF
            ):
                # A thermostat that is off has nothing to act on: the target is
                # stored for the first cycle after it is switched on, and that
                # switch requests a cycle of its own.
                request_control_cycle(self)
        if self.outdoor_sensor is not None:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.outdoor_sensor], self._trigger_outdoor_change
                )
            )
        # Send an immediate initial keepalive so TRVs don't have to wait for the first 30min tick
        try:
            _LOGGER.debug(
                "better_thermostat %s: creating keepalive task...", self.device_name
            )
            self.hass.async_create_background_task(
                self._external_temperature_keepalive(),
                name=f"bt_ext_temp_keepalive_{self.device_name}",
            )
        except Exception as exc:
            _LOGGER.error(
                "better_thermostat %s: Failed to create external temperature keepalive task: %s",
                self.device_name,
                exc,
            )
        # Start periodic EMA update (every minute)
        _LOGGER.debug("better_thermostat %s: starting EMA timer...", self.device_name)
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_update_ema_periodic, timedelta(minutes=1)
            )
        )
        # Periodic reconciliation: heal lost writes by re-converging the
        # devices onto the kernel's intent.
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._reconcile_tick, timedelta(minutes=5)
            )
        )
        _LOGGER.info("better_thermostat %s: startup completed.", self.device_name)
        self.async_write_ha_state()
        await self.async_update_ha_state(force_refresh=True)

    async def _reconcile_tick(self, now=None):
        """Periodic reconciliation tick (see controlling.reconcile_tick)."""
        await reconcile_tick(self, now)

    async def _maintenance_tick(self, event=None):
        """Periodic maintenance tick: runs valve exercise when due and enabled."""
        # quick availability check - only critical entities needed for maintenance
        try:
            # The degradation ladder advances first: it must keep stepping
            # even while an unavailable TRV aborts the tick.
            await check_and_update_degraded_mode(self)
            ok = await check_critical_entities(self)
            if ok is False:
                return
        except Exception:
            _LOGGER.debug(
                "better_thermostat %s: maintenance availability check failed; "
                "skipping this tick",
                self.device_name,
            )
            return

        now = self.clock.now()
        if self.kernel_state.maintenance.is_blocking(self.clock.monotonic()):
            return

        # Check if any TRV actually has maintenance enabled
        try:
            trvs_to_service = collect_maintenance_trvs(self.real_trvs)
        except Exception:
            _LOGGER.debug(
                "better_thermostat %s: could not collect maintenance TRVs",
                self.device_name,
            )
            trvs_to_service = []

        # Sync the schedule with legacy writers, then advance the region.
        region = self.kernel_state.maintenance
        schedule = self.next_valve_maintenance
        if not isinstance(schedule, datetime):
            schedule = None
        if region.next_due != schedule:
            region = MaintenanceState(
                phase=region.phase,
                next_due=schedule,
                running_since=region.running_since,
            )
        region = maintenance_evaluate_tick(
            region,
            now,
            window_open=bool(self.contact_open),
            has_enabled_trvs=bool(trvs_to_service),
        )
        self.kernel_state = replace(self.kernel_state, maintenance=region)
        if region.next_due is not None:
            self.next_valve_maintenance = region.next_due

        if region.phase != MaintenancePhase.DUE:
            return

        # Run maintenance asynchronously (don't block the tick)
        self.hass.async_create_background_task(
            self._run_valve_maintenance(trvs_to_service),
            name=f"bt_valve_maintenance_{self.device_name}",
        )

    async def _run_valve_maintenance(self, trvs: list[str]) -> None:
        """Perform valve exercise: open fully, then close, restore state, and reschedule.

        Manages state flags (ignore_states, in_maintenance,
        ignore_trv_states, control_queue) around the pure maintenance
        logic in ``utils.valve_maintenance``.
        """
        region = self.kernel_state.maintenance
        if region.is_blocking(self.clock.monotonic()):
            return
        if region.phase != MaintenancePhase.DUE:
            # Direct invocation (service call / tests): arm before starting.
            region = MaintenanceState(
                phase=MaintenancePhase.DUE, next_due=region.next_due
            )
        self.kernel_state = replace(
            self.kernel_state,
            maintenance=maintenance_start_run(region, self.clock.monotonic()),
        )
        # Suppress control loop briefly to prevent interference during maintenance
        self.ignore_states = True

        try:
            # Set per-TRV guard
            for trv_id in trvs:
                try:
                    self.real_trvs[trv_id].ignore_trv_states = True
                except KeyError, TypeError:
                    pass

            # Build snapshots (skips TRVs with state=None)
            infos = build_trv_snapshots(
                self.real_trvs, trvs, self.hass.states.get, self.device_name
            )
            serviced_ids = {info.entity_id for info in infos}

            # Release guard for TRVs that were skipped (state=None)
            for trv_id in trvs:
                if trv_id not in serviced_ids:
                    try:
                        self.real_trvs[trv_id].ignore_trv_states = False
                    except KeyError, TypeError:
                        pass

            # Bind adapter callbacks to self
            async def _set_valve(entity_id: str, pct: int) -> bool:
                try:
                    ok = await adapter_set_valve(self, entity_id, int(pct))
                    return bool(ok)
                except Exception:
                    _LOGGER.debug(
                        "better_thermostat %s: maintenance valve set failed for %s",
                        self.device_name,
                        entity_id,
                    )
                    return False

            async def _set_temp(entity_id: str, temp: float) -> None:
                await adapter_set_temperature(self, entity_id, temp)

            async def _set_mode(entity_id: str, mode: str) -> None:
                await adapter_set_hvac_mode(self, entity_id, mode)

            # Run 2× open/close + restore
            await run_valve_maintenance(
                infos,
                set_valve_fn=_set_valve,
                set_temperature_fn=_set_temp,
                set_hvac_mode_fn=_set_mode,
                device_name=self.device_name,
            )

            # Release per-TRV guard for serviced TRVs
            for trv_id in serviced_ids:
                try:
                    self.real_trvs[trv_id].ignore_trv_states = False
                except KeyError, TypeError:
                    pass

            # Schedule next run
            self.next_valve_maintenance = compute_next_maintenance(self.real_trvs, trvs)
            _LOGGER.info(
                "better_thermostat %s: next valve maintenance at %s",
                self.device_name,
                self.next_valve_maintenance,
            )
        finally:
            control_needed = self._control_needed_after_maintenance
            self._control_needed_after_maintenance = False
            next_due = (
                self.next_valve_maintenance
                if isinstance(self.next_valve_maintenance, datetime)
                else None
            )
            self.kernel_state = replace(
                self.kernel_state,
                maintenance=maintenance_finish_run(
                    self.kernel_state.maintenance, next_due
                ),
            )
            # Release every TRV guard even if maintenance raised before the
            # serviced-TRV cleanup above; a lingering guard suppresses future
            # TRV updates.
            for trv_id in trvs:
                try:
                    self.real_trvs[trv_id].ignore_trv_states = False
                except KeyError, TypeError:
                    pass
            # Always release ignore_states after maintenance.
            # If we restore a previous True here, the control_queue loop can get
            # stuck sleeping forever and never consume queued control actions.
            self.ignore_states = False

            # Trigger one control cycle after maintenance so BT immediately
            # resumes with the latest window/temp/target states.
            if control_needed or self.bt_hvac_mode != HVACMode.OFF:
                try:
                    request_control_cycle(self)
                except Exception:
                    # Queue not ready; the periodic tick will catch up.
                    pass

    # -- Unified state persistence helpers ------------------------------------

    def _hydrate_thermal_from_state(self) -> None:
        """Apply persisted thermal stats and filter state to the entity.

        The StateManager is the persistence authority; the RestoreEntity
        attributes only serve as a one-time migration fallback.
        """
        if self.state_mgr is None:
            return
        heating_power, heat_loss_rate = self.state_mgr.clamped_thermal()
        if heating_power is not None:
            self.heating_power = heating_power
        if heat_loss_rate is not None:
            self.heat_loss_rate = heat_loss_rate
        filters = self.state_mgr.filters
        if filters.external_temp_ema is not None:
            self.external_temp_ema = filters.external_temp_ema
            self.cur_temp_filtered = round(filters.external_temp_ema, 2)
            self._external_temp_ema_ts = self.clock.monotonic()
        if filters.temp_slope is not None:
            self.temp_slope = filters.temp_slope

    def _record_runtime_to_state(self) -> None:
        """Push the entity-held thermal stats and filters into the StateManager."""
        if self.state_mgr is None:
            return
        self.state_mgr.record_thermal(
            getattr(self, "heating_power", None), getattr(self, "heat_loss_rate", None)
        )
        self.state_mgr.record_filters(
            getattr(self, "external_temp_ema", None), getattr(self, "temp_slope", None)
        )

    @callback
    def schedule_save_state(self, delay_s: float = 15.0) -> None:
        """Schedule a coalesced persist of unified state.

        Delegates to the Store's delayed save: the runtime values are
        recorded at write time, a pending save is flushed on Home
        Assistant's final-write event (normal shutdown), and repeated
        triggers cannot starve the write.
        """
        if self.state_mgr is None:
            return
        self.state_mgr.schedule_delay_save(
            pre_save=self._record_runtime_to_state, delay_s=delay_s
        )

    async def calculate_heating_power(self):
        """Learn effective heating power (°C/min) from completed heating cycles.

        Delegates to :class:`HeatingPowerTracker` and handles HA side-effects.
        """
        if self.cur_temp is None:
            return

        # Lazy init of target range bounds
        if not hasattr(self, "min_target_temp"):
            self.min_target_temp = self.bt_target_temp or 18.0
        if not hasattr(self, "max_target_temp"):
            self.max_target_temp = self.bt_target_temp or 21.0

        current_action = self._compute_hvac_action()
        outdoor_temp = self._get_outdoor_temp()

        result = self._heating_tracker.update(
            self.cur_temp,
            current_action,
            self.clock.utcnow(),
            target_temp=self.bt_target_temp,
            outdoor_temp=outdoor_temp,
        )

        if result.action_changed:
            self.old_attr_hvac_action = result.current_action
            self.attr_hvac_action = result.current_action

        if result.cycle_result is not None or result.action_changed:
            if result.cycle_result and result.cycle_result.power_changed:
                self.schedule_save_state()
            self.async_write_ha_state()

    async def calculate_heat_loss(self):
        """Learn effective heat loss (°C/min) during idle cooling periods.

        Delegates to :class:`HeatLossTracker` and handles HA side-effects.
        """
        if self.cur_temp is None:
            return

        current_action = self._compute_hvac_action()

        result = self._loss_tracker.update(
            self.cur_temp,
            current_action,
            self.clock.utcnow(),
            window_open=bool(self.contact_open),
        )

        if result.cycle_result is not None:
            self.async_write_ha_state()
            if result.cycle_result.loss_changed:
                self.schedule_save_state()

    def _get_outdoor_temp(self) -> float | None:
        """Resolve outdoor temperature from sensor entity, if configured."""
        if self.outdoor_sensor is None:
            return None
        try:
            outdoor_state = self.hass.states.get(self.outdoor_sensor)
            if outdoor_state is not None:
                return convert_to_float_celsius(
                    str(outdoor_state.state),
                    self.device_name,
                    "calculate_heating_power.outdoor",
                    unit_of_measurement=outdoor_state.attributes.get(
                        "unit_of_measurement"
                    ),
                )
        except Exception:
            pass
        return None

    @property
    def contact_open(self) -> bool:
        """Return True when a window or door contact is confirmed open.

        Both sensor kinds suppress heating once their debounce delay has
        passed; this is the combined flag the control logic gates on.
        """
        return bool(self.window_open) or bool(self.door_open)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the device specific state attributes.

        Returns
        -------
        dict
                Attribute dictionary for the extra device specific state attributes.
        """
        dev_specific = {
            ATTR_STATE_WINDOW_OPEN: self.window_open,
            ATTR_STATE_DOOR_OPEN: self.door_open,
            ATTR_STATE_CALL_FOR_HEAT: self.call_for_heat,
            ATTR_STATE_LAST_CHANGE: self.last_change.isoformat(),
            ATTR_STATE_SAVED_TEMPERATURE: self._saved_temperature,
            ATTR_STATE_PRESET_TEMPERATURE: self.preset_mgr.saved_temperature,
            ATTR_STATE_PRESET_COOL_TEMPERATURE: self._preset_cool_temperature,
            ATTR_STATE_MAIN_MODE: self.last_main_hvac_mode,
            ATTR_STATE_OFF_TEMPERATURE: self.off_temperature,
            CONF_TOLERANCE: self.tolerance,
            CONF_TARGET_TEMP_STEP: self.bt_target_temp_step,
            ATTR_STATE_HEATING_POWER: self.heating_power,
            ATTR_STATE_HEAT_LOSS: getattr(self, "heat_loss_rate", None),
            ATTR_STATE_ERRORS: json.dumps(self.devices_errors),
            ATTR_STATE_BATTERIES: json.dumps(self.devices_states),
            "external_temp_ema": self.cur_temp_filtered,
            # Degraded mode: thermostat running with some sensors unavailable
            "degraded_mode": self.degraded_mode,
            "unavailable_sensors": self.unavailable_sensors,
            # Mode annunciation: which fail-soft rung rules, since when
            "control_mode": str(self.kernel_state.control_mode.mode),
            # Calibrator annunciation: worst self-reported health per TRV
            "calibrator_health": {
                entity_id: str(trv.calibrator_health)
                for entity_id, trv in self.real_trvs.items()
            },
            "degraded_for_s": (
                round(
                    self.clock.monotonic()
                    - self.kernel_state.control_mode.degraded_since
                )
                if self.kernel_state.control_mode.degraded_since is not None
                else None
            ),
            # ECO mode attribute removed: eco preset supported via PRESET_ECO
            ATTR_STATE_PRESET_COOL_TEMPERATURES: json.dumps(
                self._preset_cool_temperatures
            ),
        }

        # Optional: next scheduled valve maintenance (ISO8601)
        try:
            if (
                hasattr(self, "next_valve_maintenance")
                and self.next_valve_maintenance is not None
            ):
                dev_specific["next_valve_maintenance"] = (
                    self.next_valve_maintenance.isoformat()
                )
        except Exception:
            pass

        # Optional: summarize last valve method per TRV (adapter vs override)
        try:
            methods = {}
            for trv_id, info in (self.real_trvs or {}).items():
                m = info.last_valve_method
                if m:
                    methods[trv_id] = m
            if methods:
                dev_specific["valve_method"] = methods
        except Exception:
            pass

        dev_specific.update(collect_cycle_telemetry(self))
        dev_specific.update(collect_balance_attrs(self))
        dev_specific.update(collect_pid_debug_attrs(self))
        dev_specific.update(collect_mpc_v2_debug_attrs(self))

        return dev_specific

    @property
    def available(self):
        """Return if thermostat is available.

        Returns
        -------
        bool
                True if the thermostat is available.
        """
        return self._available

    @property
    def should_poll(self):
        """Return the polling state.

        Returns
        -------
        bool
                True if the thermostat uses polling.
        """
        return False

    @property
    def unique_id(self):
        """Return the unique id of this thermostat.

        Returns
        -------
        string
                The unique id of this thermostat.
        """
        return self._unique_id

    @property
    def precision(self):
        """Return the precision of the system.

        Returns
        -------
        float
                Precision of the thermostat.
        """
        return super().precision

    @property
    def target_temperature_step(self) -> float | None:
        """Return the supported step of target temperature.

        Returns
        -------
        float
                Step size of target temperature.
        """
        if self.bt_target_temp_step is not None:
            return self.bt_target_temp_step

        return super().precision

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self.cur_temp

    @property
    def current_humidity(self) -> float | None:
        """Return the current humidity if supported."""
        return self._current_humidity if hasattr(self, "_current_humidity") else None

    @property
    def bt_hvac_mode(self) -> HVACMode | None:
        """Return the BT-internal HVAC mode."""
        return self._bt_hvac_mode

    @bt_hvac_mode.setter
    def bt_hvac_mode(self, value: HVACMode | None) -> None:
        """Set the BT-internal HVAC mode and advance the mode region."""
        self._bt_hvac_mode = value
        self.kernel_state = replace(
            self.kernel_state, mode=mode_set_hvac_mode(self.kernel_state.mode, value)
        )

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return current operation."""
        # Fallback if None
        if self.bt_hvac_mode is None:
            return HVACMode.OFF
        mapped = get_hvac_bt_mode(self, self.bt_hvac_mode)
        if isinstance(mapped, HVACMode):
            result = mapped
        else:
            try:
                result = HVACMode(mapped)
            except Exception:
                try:
                    result = HVACMode[mapped.upper()]
                except Exception:
                    return HVACMode.OFF

        # Ensure result is in available modes list
        if result not in self._hvac_list:
            # HEAT should map to map_on_hvac_mode (HEAT_COOL when cooler exists)
            if result == HVACMode.HEAT and self.map_on_hvac_mode in self._hvac_list:
                return self.map_on_hvac_mode
                # Fallback to OFF if mode still invalid
            return HVACMode.OFF

        return result

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available operation modes."""
        return self._hvac_list

    @property
    def hvac_action(self):
        """Return the current HVAC action.

        Every control cycle commits a fresh action; the one computation
        here bridges the gap until the first commit and is cached so
        repeated state reads do not rebuild it.
        """
        if self.attr_hvac_action is None:
            self.attr_hvac_action = self._compute_hvac_action_pure().action
        return self.attr_hvac_action

    def _should_heat_with_tolerance(
        self, previous_action: HVACAction | None, tol: float
    ) -> bool:
        """Apply hysteresis so heating restarts only below target - tolerance."""
        if self.bt_target_temp is None or self.cur_temp is None:
            return False
        return should_heat_with_tolerance(
            self.cur_temp, self.bt_target_temp, tol, previous_action
        )

    def _build_trv_snapshots(self) -> list[TrvSnapshot]:
        """Build TrvSnapshot list from real_trvs with hass state fallback."""
        snapshots: list[TrvSnapshot] = []
        for trv_id, info in (self.real_trvs or {}).items():
            if not isinstance(info, Trv):
                continue

            # Resolve hvac_action: cached first, hass state fallback
            action_val = info.hvac_action
            action_str = str(action_val).lower() if action_val is not None else ""
            if not action_str:
                try:
                    trv_state = self.hass.states.get(trv_id)
                    action_raw = None
                    if trv_state is not None:
                        action_raw = trv_state.attributes.get("hvac_action")
                        if action_raw is None:
                            action_raw = trv_state.attributes.get("action")
                    action_str = (
                        str(action_raw).lower() if action_raw is not None else ""
                    )
                    if action_str:
                        try:
                            info.hvac_action = action_str
                        except Exception:
                            pass
                except Exception:
                    action_str = ""

            snapshots.append(
                TrvSnapshot(
                    trv_id=trv_id,
                    ignore_trv_states=bool(info.ignore_trv_states),
                    hvac_action=action_str or None,
                    valve_position=info.valve_position,
                    last_valve_percent=info.last_valve_percent,
                )
            )
        return snapshots

    def _compute_hvac_action(self):
        """Return the current HVAC action enum value."""
        result = self._compute_hvac_action_pure()
        self._commit_hvac_action(result)
        return result.action

    def _cooler_previously_active(self) -> bool:
        """Whether the cooling hysteresis band currently holds its hold edge.

        Seeded the way ``control_cooler`` seeds it: the latched decision wins,
        and the cooler's own reported mode stands in while Better Thermostat
        has not decided a cooler mode of its own.
        """
        if self.cooler_entity_id is None:
            return False
        decided = cooler_send_cache(self).get("hvac_mode_decided")
        if decided is not None:
            return decided == HVACMode.COOL
        cooler_state = self.hass.states.get(self.cooler_entity_id)
        return cooler_state is not None and cooler_state.state == HVACMode.COOL

    def _compute_hvac_action_pure(self):
        """Compute current HVAC action from the typed containers and regions.

        This runs on every state write (via the hvac_action property), so
        it reads the container-backed attributes directly instead of
        building a full world snapshot; the control path's snapshot is
        built once per cycle in compute_control_cycle().
        """
        return compute_hvac_action(
            hysteresis=self._hysteresis,
            cur_temp=self.cur_temp,
            target_temp=self.bt_target_temp,
            cool_target=self.bt_target_cooltemp,
            hvac_mode=self.hvac_mode,
            bt_hvac_mode=self.bt_hvac_mode,
            window_open=self.contact_open,
            tolerance=self.tolerance or 0.0,
            ignore_states=self.ignore_states,
            trv_snapshots=self._build_trv_snapshots(),
            cool_previously_active=self._cooler_previously_active(),
            device_name=self.device_name,
        )

    def _commit_hvac_action(self, result) -> None:
        """Apply computed hysteresis state."""
        self._hysteresis.last_action = result.new_last_action
        self._hysteresis.hold_active = result.new_hold_active

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach.

        Returns
        -------
        float
                Target temperature.
        """
        if self.bt_target_temp is None:
            return None
        if self.bt_min_temp is None or self.bt_max_temp is None:
            return self.bt_target_temp
        # if target temp is below minimum, return minimum
        if self.bt_target_temp < self.bt_min_temp:
            return self.bt_min_temp
        # if target temp is above maximum, return maximum
        if self.bt_target_temp > self.bt_max_temp:
            return self.bt_max_temp
        return self.bt_target_temp

    @property
    def target_temperature_low(self) -> float | None:
        """Return the low target temperature."""
        if self.cooler_entity_id is None:
            return None
        return self.bt_target_temp

    @property
    def target_temperature_high(self) -> float | None:
        """Return the high target temperature."""
        if self.cooler_entity_id is None:
            return None
        return self.bt_target_cooltemp

    async def async_set_hvac_mode(self, hvac_mode: HVACMode | str) -> None:
        """Set hvac mode.

        Returns
        -------
        None
        """

        hvac_mode_norm = normalize_hvac_mode(hvac_mode)
        if hvac_mode_norm not in (HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.OFF):
            raise ServiceValidationError(
                f"Unsupported hvac_mode {hvac_mode!r} for {self.device_name}; "
                f"supported: heat, heat_cool, off"
            )
        self.bt_hvac_mode = HVACMode(get_hvac_bt_mode(self, hvac_mode_norm))
        self.async_write_ha_state()
        # During valve maintenance we must not block on the control queue (maxsize=1)
        # and must not override maintenance valve exercise.
        if getattr(self, "in_maintenance", False):
            self._control_needed_after_maintenance = True
            return

        request_control_cycle(self)

    def _seed_cool_target_from_cooler(self, log_source: str) -> bool:
        """Fill a cooling target that is still unknown from the cooler's state.

        The startup path for a cooling target read off the device;
        ``trigger_cooler_change`` is the runtime one. Here the value is read
        with the key precedence a cooler is driven through — a device that only
        supports TARGET_TEMPERATURE_RANGE reports ``temperature: None`` and
        carries its setpoint in ``target_temp_high`` — then clamped into the
        configured range and ordered above the heating target. A cooler that was
        unavailable while that range was derived contributed no bounds to it, so
        the setpoint it reports can sit outside the range and is clamped into it
        exactly like a reported one. Echo detection has nothing to compare
        against, because no setpoint is written to a cooler whose target is
        unknown.

        A target that is already known is left alone: it is either the value a
        restored preset carries, which is the user's own choice, or one this
        method took earlier.

        A device that carries both roles is the exception: the setpoint it
        reports belongs to whichever channel last wrote it, and at startup that
        is the heating one, so it says nothing about cooling. The preset's own
        cooling temperature is taken instead, which is a value the user can see
        and change and a heating setpoint read off the device is not.

        Parameters
        ----------
        log_source : str
            the reading site's own name, forwarded for logging context; the
            startup sequence reads twice, and the line that reports an
            attribute the resolution could not read names this value, so each
            caller passes the name of the site it reads from

        Returns
        -------
        bool
            whether a cooling target was seeded; False when one is already
            known, when no cooler is configured, and when the cooler has no
            state, is unavailable or publishes no readable setpoint
        """
        if self.cooler_entity_id is None or self.bt_target_cooltemp is not None:
            return False
        _shared_entity_id = dual_role_entity_id(self)
        if _shared_entity_id is not None:
            cool_temp = self._preset_cool_temperatures.get(
                self.preset_mgr.mode or PRESET_NONE
            )
            if not isinstance(cool_temp, (int, float)):
                return False
            # A stored preset pair is re-injected verbatim, so the value takes
            # the same bound every other re-injected target takes.
            self.bt_target_cooltemp = self._bound_target_to_range(float(cool_temp))
            _LOGGER.info(
                "better_thermostat %s: %s drives both channels, taking the "
                "preset cooling temperature %s as the cool target",
                self.device_name,
                _shared_entity_id,
                self.bt_target_cooltemp,
            )
            self._enforce_cool_above_heat(regardless_of_hvac_mode=True)
            return True
        cooler_state = self.hass.states.get(self.cooler_entity_id)
        if cooler_state is None or cooler_state.state in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return False
        setpoint = resolve_inbound_setpoint(
            self,
            cooler_state,
            keys=COOLER_SETPOINT_KEYS,
            known_values=(),
            step=device_setpoint_step(self, cooler_state, log_source),
            log_source=log_source,
        )
        if setpoint is None:
            return False
        self._seed_cool_target(setpoint, self.cooler_entity_id)
        return True

    def _seed_cool_target(self, setpoint: InboundSetpoint, entity_id: str) -> None:
        """Adopt a cooler's own setpoint as the cooling target.

        A cooling target that is unknown holds the cooler off on every control
        cycle, and the cooler's own setpoint is the only value available to fill
        it with. That value is an observation rather than user intent, so the
        cooling side is the one that yields when the two targets collide, and the
        heating target the user set stays where it is. The ordering is applied in
        every HVAC mode, because a target seeded while Better Thermostat is off
        is the one the first cooling cycle after switching on works with and that
        transition does not revisit the pair.

        A value the user never chose has to be traceable, because the stored
        target is written back to the cooler: this annunciates the clamp into the
        configured range, and :meth:`_enforce_cool_above_heat` annunciates a lift
        above the heating target.

        Parameters
        ----------
        setpoint : InboundSetpoint
            the setpoint the cooler reports, already resolved into BT's range
        entity_id : str
            the cooler whose setpoint is being adopted
        """
        if setpoint.clamped:
            _LOGGER.warning(
                "better_thermostat %s: Cooler %s reported setpoint %s outside of "
                "range while the cool target is unknown, taking %s as the cool "
                "target",
                self.device_name,
                entity_id,
                setpoint.raw,
                setpoint.value,
            )
        else:
            _LOGGER.info(
                "better_thermostat %s: Cooler %s reports setpoint %s while the "
                "cool target is unknown, taking it as the cool target",
                self.device_name,
                entity_id,
                setpoint.value,
            )
        self.bt_target_cooltemp = setpoint.value
        self._enforce_cool_above_heat(regardless_of_hvac_mode=True)

    def _enforce_cool_above_heat(
        self, *, regardless_of_hvac_mode: bool = False
    ) -> None:
        """Keep the cooling target strictly above the heating target.

        In HEAT_COOL mode the two setpoints must not cross. If the cool target is
        at or below the heat target, bump it up by one temperature step. The step
        is normalised to a positive value first: a configured step of zero or
        below would move the cooling target the wrong way and leave the pair it
        is meant to order inverted.

        The cooling target is reported as ``target_temperature_high`` and written
        to the cooler, so the bump is capped at the configured maximum. Where the
        heating target leaves the range no room, the two invariants collide and
        one of them decides:

        - A heating target resting on the maximum leaves no value above it inside
          the range. The range wins: the cooling target goes to the maximum, the
          closest to ordered that the range holds, and the overlap that remains
          is annunciated. Cooling is gated on the room being warmer than the
          heating target as well, so the two targets meeting does not run the
          cooler against the TRVs.
        - A heating target above the maximum is itself outside the range, so a
          cap would put the cooling target below it. The ordering wins: the bump
          is left uncapped rather than inverting the pair this method exists to
          order.

        Parameters
        ----------
        regardless_of_hvac_mode : bool
            Enforce the ordering outside HEAT_COOL as well. Callers that store a
            cooling target read off the device need this: such a value has to be
            ordered the moment it is stored, because the mode can change without
            the pair being looked at again, and the first cooling cycle after
            that would drive the room down while the TRVs heat it up.
        """
        if not regardless_of_hvac_mode and self.hvac_mode != HVACMode.HEAT_COOL:
            return
        if (
            self.bt_target_cooltemp is None
            or self.bt_target_temp is None
            or self.bt_target_cooltemp > self.bt_target_temp
        ):
            return
        step = normalize_step(self.bt_target_temp_step)
        adjusted = self.bt_target_temp + step
        maximum = self.bt_max_temp
        if maximum is not None and maximum >= self.bt_target_temp:
            adjusted = min(adjusted, maximum)
        if adjusted == self.bt_target_cooltemp:
            # The maximum and the heating target coincide and the cooling target
            # already rests on them, so the bump has nowhere to land.
            return
        if adjusted > self.bt_target_temp:
            _LOGGER.warning(
                "better_thermostat %s: cooling target %.2f adjusted to %.2f to stay above heating target %.2f",
                self.device_name,
                self.bt_target_cooltemp,
                adjusted,
                self.bt_target_temp,
            )
        else:
            _LOGGER.warning(
                "better_thermostat %s: cooling target %.2f raised to the "
                "configured maximum %.2f, which the heating target occupies as "
                "well, because the range holds no value above it",
                self.device_name,
                self.bt_target_cooltemp,
                adjusted,
            )
        self.bt_target_cooltemp = adjusted

    def _enforce_heat_below_cool(self) -> None:
        """Keep the heating target strictly below the cooling target.

        The counterpart to :meth:`_enforce_cool_above_heat`, for the case where
        the cooling target is the value that was just set: the heating target
        yields instead, down to one temperature step below the cooling target
        and never below the configured minimum. The step is normalised to a
        positive value first: a configured step of zero or below would move the
        heating target the wrong way and leave the pair it is meant to order
        inverted.

        A minimum that the cooling target does not clear by at least one step
        pins the heating target on the minimum, at or above the cooling target:
        the range bounds the value that is stored, and the overlap that remains
        is annunciated as such rather than reported as an ordered pair.
        """
        if (
            self.hvac_mode != HVACMode.HEAT_COOL
            or self.bt_target_cooltemp is None
            or self.bt_target_temp is None
            or self.bt_target_temp < self.bt_target_cooltemp
        ):
            return
        step = normalize_step(self.bt_target_temp_step)
        adjusted = self.bt_target_cooltemp - step
        if self.bt_min_temp is not None:
            adjusted = max(adjusted, self.bt_min_temp)
        if adjusted == self.bt_target_temp:
            # The minimum pins the drop on the heating target itself, so it has
            # nowhere to land.
            return
        if adjusted < self.bt_target_cooltemp:
            _LOGGER.warning(
                "better_thermostat %s: heating target %.2f adjusted to %.2f to stay below cooling target %.2f",
                self.device_name,
                self.bt_target_temp,
                adjusted,
                self.bt_target_cooltemp,
            )
        else:
            _LOGGER.warning(
                "better_thermostat %s: heating target %.2f set to the configured "
                "minimum %.2f, which is not below the cooling target %.2f",
                self.device_name,
                self.bt_target_temp,
                adjusted,
                self.bt_target_cooltemp,
            )
        self.bt_target_temp = adjusted

    def _bound_target_to_range(self, value: float) -> float:
        """Bound a re-injected target into the configured range.

        Stored targets come back into the entity without passing the range
        check the value they replace went through: a preset pair written while
        a cooler was unavailable, or a manual cooling target stashed under a
        different range, is re-injected verbatim. Both targets are published as
        ``target_temperature_low`` / ``target_temperature_high`` and written to
        the devices, so a value the configured range does not contain is not a
        setpoint BT can hold.

        The lower bound is applied first and the upper bound second, each only
        when it is known. The order is load-bearing:
        :meth:`_resolve_temperature_range` permits a non-overlapping range
        where ``bt_min_temp`` is above ``bt_max_temp``, and applying the two in
        sequence rather than exclusively lets the upper bound decide there.
        This is the sequencing :func:`resolve_inbound_setpoint` uses for the
        same reason.

        The bound is silent. The stored heating target and the active preset's
        temperature are normally the same number, so a warning here would
        repeat the one :func:`restore_target_temperature` already emitted for
        that value.

        Parameters
        ----------
        value : float
                the target being re-injected, in °C

        Returns
        -------
        float
                the target bounded into the configured range
        """
        if self.bt_min_temp is not None and value < self.bt_min_temp:
            value = self.bt_min_temp
        if self.bt_max_temp is not None and self.bt_max_temp < value:
            value = self.bt_max_temp
        return value

    def _clamp_inbound_cool_target(self, value: float) -> float:
        """Clamp a device-reported cooling setpoint above the heating target.

        A report from the cooler is authoritative for the cooling channel only.
        Rather than pulling the heating target down to make room, the reported
        value is raised onto a floor one step above the heating target, so a
        press on the air conditioner's own remote leaves the radiators alone.
        The floor is capped at the configured maximum because a bound outside
        the range is not a setpoint BT can hold, and that cap is where the
        separation gives way: a heating target resting on the maximum or above
        it puts the floor on that target or below it, so the value returned
        there no longer clears it. The residual
        :meth:`_enforce_heat_below_cool` settles that degenerate case, and it
        does so by moving the heating target.

        The bound applies whenever a cooling channel is configured rather than
        only while the live mode is HEAT_COOL, matching
        :meth:`_clamp_inbound_heat_target`: the ordering of the two targets is a
        property of the configuration, so it has to hold whatever mode the group
        happens to be in.

        The one step of separation comes from :func:`normalize_step`, because
        ``bt_target_temp_step`` can carry whatever a child entity reports: a
        negative step would put the floor below the heating target and invert
        the pair this bound exists to order, and a NaN one would make the
        comparison against it false and drop the bound altogether.

        Parameters
        ----------
        value : float
                the reported cooling setpoint in °C, already clamped into the
                configured range

        Returns
        -------
        float
                the setpoint to adopt, unchanged unless it had to be raised
                onto the floor the heating target and the configured maximum
                set
        """
        if self.cooler_entity_id is None or self.bt_target_temp is None:
            return value
        step = normalize_step(self.bt_target_temp_step)
        floor = self.bt_target_temp + step
        if self.bt_max_temp is not None:
            floor = min(floor, self.bt_max_temp)
        return max(value, floor)

    def _clamp_inbound_heat_target(self, value: float) -> float:
        """Clamp a device-reported heating setpoint below the cooling target.

        The counterpart to :meth:`_clamp_inbound_cool_target`: a TRV knob turn
        is authoritative for the heating channel only, so the reported value is
        lowered onto a ceiling one step below the cooling target instead of
        raising that target. The ceiling is held at the configured minimum, and
        that bound is where the separation gives way in the same way: a cooling
        target resting on the minimum or below it puts the ceiling on that
        target or above it, so the value returned there no longer clears it.
        The residual :meth:`_enforce_cool_above_heat` settles that degenerate
        case, and it does so by moving the cooling target. Like its counterpart
        the bound keys off the configured cooling channel rather than the live
        mode, and here that is what makes it hold at all: a valve with
        ``no_off_system_mode`` reports its knob turn while ``bt_hvac_mode`` is
        still OFF, and the same event then resolves the mode to HEAT.

        The one step of separation comes from :func:`normalize_step` for the
        same reason as in the counterpart: a step a child reports as negative or
        non-finite would either invert the pair or drop the bound.

        Parameters
        ----------
        value : float
                the reported heating setpoint in °C, already clamped into the
                configured range

        Returns
        -------
        float
                the setpoint to adopt, unchanged unless it had to be lowered
                onto the ceiling the cooling target and the configured minimum
                set
        """
        if self.cooler_entity_id is None or self.bt_target_cooltemp is None:
            return value
        step = normalize_step(self.bt_target_temp_step)
        ceiling = self.bt_target_cooltemp - step
        if self.bt_min_temp is not None:
            ceiling = max(ceiling, self.bt_min_temp)
        return min(value, ceiling)

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        _LOGGER.debug(
            "better_thermostat %s: async_set_temperature kwargs=%s, current preset=%s, hvac_mode=%s",
            self.device_name,
            kwargs,
            self.preset_mgr.mode,
            self.bt_hvac_mode,
        )

        _new_setpoint = None
        _new_setpointlow = None
        _new_setpointhigh = None

        # Validate every field before mutating any state, so a rejected
        # payload (e.g. valid hvac_mode + unparseable temperature) leaves
        # the thermostat unchanged instead of partially applied.
        _new_hvac_mode: HVACMode | None = None
        if ATTR_HVAC_MODE in kwargs:
            hvac_mode_val = kwargs.get(ATTR_HVAC_MODE, None)
            hvac_mode_norm = (
                normalize_hvac_mode(hvac_mode_val)
                if hvac_mode_val is not None
                else None
            )
            if hvac_mode_norm not in (HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.OFF):
                raise ServiceValidationError(
                    f"Unsupported hvac_mode {hvac_mode_val!r} for "
                    f"{self.device_name}; supported: heat, heat_cool, off"
                )
            # Same normalization as async_set_hvac_mode, so both service
            # entry points map HEAT/HEAT_COOL identically.
            _new_hvac_mode = HVACMode(get_hvac_bt_mode(self, hvac_mode_norm))

        def _validated_setpoint(attr: str, context: str) -> float | None:
            """Cast one temperature kwarg to float or reject the call.

            Frontend cards pass unscrutinized payloads; a present but
            unparseable value is a caller error, not something to drop
            silently.
            """
            if attr not in kwargs:
                return None
            value = convert_to_float(
                str(kwargs.get(attr, None)), self.device_name, context
            )
            if value is None:
                raise ServiceValidationError(
                    f"Invalid {attr} {kwargs.get(attr)!r} for "
                    f"{self.device_name}; must be numeric"
                )
            return value

        _new_setpoint = _validated_setpoint(
            ATTR_TEMPERATURE, "controlling.settarget_temperature()"
        )
        _new_setpointlow = _validated_setpoint(
            ATTR_TARGET_TEMP_LOW, "controlling.settarget_temperature_low()"
        )
        _new_setpointhigh = _validated_setpoint(
            ATTR_TARGET_TEMP_HIGH, "controlling.settarget_temperature_high()"
        )

        if _new_hvac_mode is not None:
            self.bt_hvac_mode = _new_hvac_mode

        if (
            _new_setpoint is None
            and _new_setpointlow is None
            and _new_setpointhigh is None
        ):
            if _new_hvac_mode is not None:
                # A mode-only payload still needs to be published and
                # applied, exactly like async_set_hvac_mode.
                self.async_write_ha_state()
                if getattr(self, "in_maintenance", False):
                    self._control_needed_after_maintenance = True
                    return
                request_control_cycle(self)
                return
            _LOGGER.debug(
                "better_thermostat %s: received a new setpoint from HA, but temperature attribute was not set, ignoring",
                self.device_name,
            )
            return

        # Validate against min/max temps
        if _new_setpoint is not None:
            _new_setpoint = min(self.max_temp, max(self.min_temp, _new_setpoint))
        if _new_setpointlow is not None:
            _new_setpointlow = min(self.max_temp, max(self.min_temp, _new_setpointlow))
        if _new_setpointhigh is not None:
            _new_setpointhigh = min(
                self.max_temp, max(self.min_temp, _new_setpointhigh)
            )

        # Preserve explicit 0.0 values (avoid Python truthiness bug)
        if _new_setpoint is not None:
            self.bt_target_temp = _new_setpoint
        elif _new_setpointlow is not None:
            self.bt_target_temp = _new_setpointlow

        if _new_setpointhigh is not None:
            self.bt_target_cooltemp = _new_setpointhigh

        # Enforce ordering: cool target should be above heat target in HEAT_COOL.
        self._enforce_cool_above_heat()

        # If a specific preset (Comfort, Eco, …) is active and the user manually
        # changes the target temperature to a value that does not match the
        # preset's stored temperature, deactivate the preset (return to
        # PRESET_NONE) while keeping the new manual temperature. The preset's
        # own Number entity also funnels through this method, but it first
        # updates the preset's stored temperature so the values match and the
        # preset stays active.
        if (
            (_new_setpoint is not None or _new_setpointlow is not None)
            and self.bt_target_temp is not None
            and self.preset_mgr.mode != PRESET_NONE
        ):
            applied = float(self.bt_target_temp)
            preset_stored = self.preset_mgr.get_temperature(self.preset_mgr.mode)
            if preset_stored is None or abs(applied - float(preset_stored)) > 1e-3:
                old_preset = self.preset_mgr.mode
                self.preset_mgr.deactivate()
                _LOGGER.debug(
                    "better_thermostat %s: Deactivated preset %s due to manual target temperature change to %s",
                    self.device_name,
                    old_preset,
                    applied,
                )

        # If the user manually changes the temperature while in PRESET_NONE (Manual),
        # record it as the stored manual temperature. Specific presets (Comfort, Eco,
        # etc.) are managed via separate Number entities and must NOT be overwritten
        # by manual setpoint changes.
        if (
            _new_setpoint is not None or _new_setpointlow is not None
        ) and self.bt_target_temp is not None:
            applied = float(self.bt_target_temp)
            old_value = self.preset_mgr.record_manual_change(applied)
            if old_value is not None:
                _LOGGER.debug(
                    "better_thermostat %s: Updated stored preset temperature for %s from %s to %s due to manual change",
                    self.device_name,
                    self.preset_mgr.mode,
                    old_value,
                    applied,
                )

        _LOGGER.debug(
            "better_thermostat %s: HA set target temperature to %s & %s",
            self.device_name,
            self.bt_target_temp,
            self.bt_target_cooltemp,
        )

        self.async_write_ha_state()
        # Only trigger control queue if thermostat is not OFF
        # When OFF, we still save the temperature but don't send it to the physical device
        if self.bt_hvac_mode != HVACMode.OFF:
            # During valve maintenance we must not block on the control queue
            # (Queue maxsize=1) and must not override maintenance.
            if getattr(self, "in_maintenance", False):
                self._control_needed_after_maintenance = True
                return
            request_control_cycle(self)

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    def _signal_config_change(self) -> None:
        """Signal a configuration change to trigger entity cleanup/recreation."""
        signal_key = f"bt_config_changed_{self._config_entry_id}"
        dispatcher_send(self.hass, signal_key, {"entry_id": self._config_entry_id})
        _LOGGER.debug(
            "better_thermostat %s: Signaled configuration change", self.device_name
        )

    async def run_valve_maintenance_service(self) -> None:
        """Entity service: run valve maintenance immediately (ignores schedule)."""
        try:
            if self.in_maintenance:
                _LOGGER.debug(
                    "better_thermostat %s: valve maintenance already running",
                    self.device_name,
                )
                return
            trvs_to_service = collect_maintenance_trvs(self.real_trvs)
            if not trvs_to_service:
                _LOGGER.debug(
                    "better_thermostat %s: valve maintenance requested, but no TRV has it enabled",
                    self.device_name,
                )
                return
            # force immediate run
            self.next_valve_maintenance = self.clock.now()
            await self._run_valve_maintenance(trvs_to_service)
        except Exception:
            _LOGGER.debug(
                "better_thermostat %s: valve maintenance service encountered an error",
                self.device_name,
            )

    @property
    def min_temp(self):
        """Return the minimum temperature.

        Returns
        -------
        float
                the minimum temperature.
        """
        if self.bt_min_temp is not None:
            return self.bt_min_temp

        # get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature.

        Returns
        -------
        float
                the maximum temperature.
        """
        if self.bt_max_temp is not None:
            return self.bt_max_temp

        # Get default temp from super class
        return super().max_temp

    @property
    def supported_features(self):
        """Return the list of supported features.

        Returns
        -------
        array
                Supported features.
        """
        if self.cooler_entity_id is not None:
            return (
                ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                | ClimateEntityFeature.PRESET_MODE
                | ClimateEntityFeature.TURN_OFF
                | ClimateEntityFeature.TURN_ON
            )
        return (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.PRESET_MODE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

    @property
    def preset_mode(self):
        """Return the current preset mode."""
        return self.preset_mgr.mode

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode (HA async API).

        NOTE:
            Home Assistant calls `async_set_preset_mode` directly when present.
            A coroutine named `set_preset_mode` (without the `async_` prefix)
            would be assumed synchronous and executed via `run_in_executor`,
            raising "set_preset_mode cannot be used with run_in_executor".
        """
        self.bt_update_lock = True
        try:
            old_preset = self.preset_mgr.mode
            new_temp = self.preset_mgr.activate(
                preset_mode, self.bt_target_temp, self.min_temp, self.max_temp
            )
            self.kernel_state = replace(
                self.kernel_state,
                mode=mode_set_preset(self.kernel_state.mode, self.preset_mgr.mode),
            )

            if new_temp is None and preset_mode not in self.preset_mgr.available_modes:
                _LOGGER.warning(
                    "better_thermostat %s: Unsupported preset mode %s",
                    self.device_name,
                    preset_mode,
                )
                return

            # Capture the manual cooling target before a preset overwrites it, so it
            # can be preserved and restored when returning to PRESET_NONE.
            previous_cooltemp = self.bt_target_cooltemp
            if new_temp is not None:
                self.bt_target_temp = new_temp
                if (
                    self.cooler_entity_id is not None
                    and preset_mode != PRESET_NONE
                    and preset_mode in self._preset_cool_temperatures
                ):
                    cool_temp = self._preset_cool_temperatures[preset_mode]
                    self.bt_target_cooltemp = min(
                        self.max_temp, max(self.min_temp, cool_temp)
                    )
                    _LOGGER.debug(
                        "better_thermostat %s: Applied preset %s cooling temperature: %s°C",
                        self.device_name,
                        preset_mode,
                        self.bt_target_cooltemp,
                    )

            if (
                old_preset == PRESET_NONE
                and preset_mode != PRESET_NONE
                and self.cooler_entity_id is not None
                and self._preset_cool_temperature is None
            ):
                self._preset_cool_temperature = previous_cooltemp
            elif (
                preset_mode == PRESET_NONE
                and self.cooler_entity_id is not None
                and self._preset_cool_temperature is not None
            ):
                self.bt_target_cooltemp = self._bound_target_to_range(
                    self._preset_cool_temperature
                )
                self._preset_cool_temperature = None

            # Both targets a preset change writes are re-injected rather than
            # chosen, so the pair is ordered the moment it is stored: the HVAC
            # mode can change without it being looked at again, and
            # async_set_hvac_mode does not re-enforce the ordering.
            self._enforce_cool_above_heat(regardless_of_hvac_mode=True)

            _LOGGER.debug(
                "better_thermostat %s: After preset change %s -> %s, bt_target_temp=%s, bt_hvac_mode=%s",
                self.device_name,
                old_preset,
                preset_mode,
                self.bt_target_temp,
                self.bt_hvac_mode,
            )

            self.async_write_ha_state()
            if (
                hasattr(self, "control_queue_task")
                and self.control_queue_task is not None
            ):
                request_control_cycle(self)
        finally:
            self.bt_update_lock = False

    # Backwards compatibility: If anything external still tries to call the old
    # (incorrect) async method name, provide a thin wrapper. This is intentionally
    # NOT async so HA will not pick it up as the implementation again.
    # type: ignore[override] # Backward compatibility wrapper
    def set_preset_mode(self, preset_mode: str) -> None:
        """Backward compatible wrapper.

        This wrapper schedules the new async method on the event loop. It should
        only be hit by external/custom code; HA core will prefer async_set_preset_mode.
        """
        if self.hass is None:
            return
        # Schedule without waiting; state updates will propagate asynchronously.
        self.hass.async_create_background_task(
            self.async_set_preset_mode(preset_mode),
            name=f"bt_set_preset_{self.device_name}",
        )

    @property
    def preset_modes(self):
        """Return the available preset modes."""
        return self.preset_mgr.available_modes

    async def reset_pid_learnings_service(
        self,
        apply_pid_defaults: bool = False,
        defaults_kp: float | None = None,
        defaults_ki: float | None = None,
        defaults_kd: float | None = None,
    ) -> None:
        """Entity service: reset learned PID state for this entity.

        - Clears all cached PIDState entries for this entity (all TRVs/buckets)
        - Schedules persistence saves for the map
        """
        try:
            state_mgr = self.state_mgr
            if state_mgr is None:
                _LOGGER.debug(
                    "better_thermostat %s: no state manager, nothing to reset",
                    self.device_name,
                )
                return
            prefix = f"{self._unique_id}:"
            count = state_mgr.reset_pid_states(prefix)
            _LOGGER.info(
                "better_thermostat %s: reset %d PID learning state entries (prefix=%s)",
                self.device_name,
                count,
                prefix,
            )
            # Schedule persistence of cleared PID states
            try:
                self.schedule_save_state()
            except Exception:
                _LOGGER.debug(
                    "better_thermostat %s: could not schedule state save after PID reset",
                    self.device_name,
                )

            # Optionally seed PID defaults for the CURRENT target bucket(s)
            if apply_pid_defaults:
                try:
                    # Use provided overrides or PIDParams defaults
                    _defs = PIDParams()
                    kp = float(defaults_kp) if defaults_kp is not None else _defs.kp
                    ki = float(defaults_ki) if defaults_ki is not None else _defs.ki
                    kd = float(defaults_kd) if defaults_kd is not None else _defs.kd

                    # Build current bucket tag based on current heat target
                    def _bucket(temp):
                        try:
                            return format_bucket(round_to_bucket(temp))
                        except TypeError, ValueError:
                            return None

                    # Build list of candidate buckets: current and ±0.5°C neighbors
                    bucket_tag = _bucket(self.bt_target_temp)
                    buckets: list[str] = []
                    try:
                        if isinstance(self.bt_target_temp, (int, float)):
                            base = round_to_bucket(self.bt_target_temp)
                            buckets = [
                                format_bucket(base),
                                format_bucket(base + 0.5),
                                format_bucket(base - 0.5),
                            ]
                        elif bucket_tag:
                            buckets = [bucket_tag]
                    except TypeError, ValueError:
                        if bucket_tag:
                            buckets = [bucket_tag]
                    uid = resolve_unique_id(self)
                    seeded = 0
                    for trv_id in self.real_trvs:
                        for b in buckets or []:
                            key = f"{uid}:{trv_id}:{b}"
                            try:
                                pid_state = state_mgr.get_pid(key)
                                pid_state.pid_kp = kp
                                pid_state.pid_ki = ki
                                pid_state.pid_kd = kd
                                state_mgr.set_pid(key, pid_state)
                                seeded += 1
                            except Exception:
                                _LOGGER.debug(
                                    "better_thermostat %s: could not seed PID gains for %s",
                                    self.device_name,
                                    key,
                                )
                    if seeded > 0:
                        _LOGGER.info(
                            "better_thermostat %s: applied PID defaults (kp=%.3f ki=%.3f kd=%.3f) to %d bucket state(s) across %d TRV(s)",
                            self.device_name,
                            kp,
                            ki,
                            kd,
                            seeded,
                            len(list(self.real_trvs.keys()) or []),
                        )
                        try:
                            self.schedule_save_state()
                        except Exception:
                            _LOGGER.debug(
                                "better_thermostat %s: could not schedule state save "
                                "after seeding PID defaults",
                                self.device_name,
                            )
                        # Kick the control loop so the new gains are used promptly
                        try:
                            request_control_cycle(self)
                        except Exception:
                            _LOGGER.debug(
                                "better_thermostat %s: could not queue control cycle "
                                "after seeding PID defaults",
                                self.device_name,
                            )
                    else:
                        _LOGGER.debug(
                            "better_thermostat %s: apply_pid_defaults did not seed any bucket (bt_target_temp=%s, buckets=%s)",
                            self.device_name,
                            self.bt_target_temp,
                            buckets,
                        )
                except Exception as e:
                    _LOGGER.debug(
                        "better_thermostat %s: apply_pid_defaults failed: %s",
                        self.device_name,
                        e,
                    )
        except Exception as e:
            _LOGGER.debug(
                "better_thermostat %s: reset_pid_learnings_service error: %s",
                self.device_name,
                e,
            )

    async def _async_update_ema_periodic(self, now=None):
        """Periodically update the EMA filter to ensure it converges even if sensor is silent."""
        # Skip if startup is still running to avoid race conditions or confusing logs
        if self.startup_running:
            return

        _LOGGER.debug(
            "better_thermostat %s: _async_update_ema_periodic triggered",
            self.device_name,
        )

        last_raw = self.last_known_external_temp
        if last_raw is not None:
            try:
                _LOGGER.debug(
                    "better_thermostat %s: updating EMA with last_raw=%s",
                    self.device_name,
                    last_raw,
                )

                # Calculate slope from EMA change
                old_ema = self.external_temp_ema
                old_ts = self._slope_periodic_last_ts
                now_ts = self.clock.monotonic()

                new_ema = _update_external_temp_ema(self, float(last_raw))

                if old_ema is not None and old_ts is not None:
                    dt_min = (now_ts - old_ts) / 60.0
                    if dt_min > 0.1:  # Avoid division by zero or tiny steps
                        delta_T = new_ema - old_ema
                        slope = delta_T / dt_min
                        self.temp_slope = slope
                        _LOGGER.debug(
                            "better_thermostat %s: periodic slope calc: old_ema=%.3f new_ema=%.3f dt=%.2fmin -> slope=%.4f K/min",
                            self.device_name,
                            old_ema,
                            new_ema,
                            dt_min,
                            slope,
                        )

                self._slope_periodic_last_ts = now_ts

                _LOGGER.debug(
                    "better_thermostat %s: periodic EMA result=%.3f",
                    self.device_name,
                    new_ema,
                )
                # If the sensor entity is listening to state changes, we should trigger an update
                # But we don't want to spam the state machine if nothing changed significantly?
                # The sensor entity reads `cur_temp_filtered` from `self`.
                # We can just write state if we want the sensor to update.
                # But `async_write_ha_state` updates the climate entity state.
                # The sensor listens to the climate entity.
                # So we should call `async_write_ha_state` if we want the sensor to see the new EMA.
                self.async_write_ha_state()
            except Exception as e:
                _LOGGER.error(
                    "better_thermostat %s: error in _async_update_ema_periodic: %s",
                    self.device_name,
                    e,
                )
        else:
            _LOGGER.debug(
                "better_thermostat %s: _async_update_ema_periodic skipped (no last_known_external_temp)",
                self.device_name,
            )

    async def async_will_remove_from_hass(self):
        """Run when entity will be removed from hass."""
        # Terminate the startup retry loop so an entity whose dependencies
        # never became available does not keep polling after unload.
        self.kernel_state = replace(
            self.kernel_state, lifecycle=lifecycle_stop(self.kernel_state.lifecycle)
        )
        if self._control_task:
            self._control_task.cancel()
            try:
                await self._control_task
            except asyncio.CancelledError:
                pass
        if self._window_task:
            self._window_task.cancel()
            try:
                await self._window_task
            except asyncio.CancelledError:
                pass
        if self._door_task:
            self._door_task.cancel()
            try:
                await self._door_task
            except asyncio.CancelledError:
                pass
        await super().async_will_remove_from_hass()
