"""Better Thermostat Sensor Platform."""

import logging
import time

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.entity_registry import (
    async_get as async_get_entity_registry,
    EntityRegistry,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .calibration import _get_current_solar_intensity
from .utils.const import CONF_CALIBRATION_MODE, CalibrationMode

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"

# Globale Tracking-Variablen für aktive algorithmus-spezifische Entitäten
_ACTIVE_ALGORITHM_ENTITIES = {}
_ENTITY_CLEANUP_CALLBACKS = {}
_DISPATCHER_UNSUBSCRIBES = {}  # Store unsubscribe functions


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Better Thermostat sensors."""
    bt_climate = hass.data[DOMAIN][entry.entry_id].get("climate")
    if not bt_climate:
        _LOGGER.warning(
            "Better Thermostat climate entity not found for entry %s. "
            "Sensors will not be added.",
            entry.entry_id,
        )
        return

    sensors = [
        BetterThermostatExternalTempSensor(bt_climate),
        BetterThermostatTempSlopeSensor(bt_climate),
        BetterThermostatSolarIntensitySensor(bt_climate),
    ]

    # Dynamische algorithmus-spezifische Sensor-Erstellung
    algorithm_sensors = await _setup_algorithm_sensors(hass, entry, bt_climate)
    sensors.extend(algorithm_sensors)

    async_add_entities(sensors, True)

    # Registriere Callback für dynamische Entity-Updates
    await _register_dynamic_entity_callback(hass, entry, bt_climate, async_add_entities)


async def _setup_algorithm_sensors(
    hass: HomeAssistant, entry: ConfigEntry, bt_climate
) -> list:
    """Setup algorithm-specific sensors based on current configuration."""
    algorithm_sensors = []
    entry_id = entry.entry_id
    current_algorithms = _get_active_algorithms(bt_climate)
    
    # Cleanup stale algorithm entities from previous configurations
    await _cleanup_stale_algorithm_entities(hass, entry_id, bt_climate, current_algorithms)
    
    # Setup MPC sensors
    if CalibrationMode.MPC_CALIBRATION in current_algorithms:
        mpc_sensors = [
            BetterThermostatVirtualTempSensor(bt_climate),
            BetterThermostatMpcGainSensor(bt_climate),
            BetterThermostatMpcLossSensor(bt_climate),
            BetterThermostatMpcKaSensor(bt_climate),
            BetterThermostatMpcStatusSensor(bt_climate),
        ]
        algorithm_sensors.extend(mpc_sensors)
        
        # Tracking für aktive MPC-Entitäten
        if entry_id not in _ACTIVE_ALGORITHM_ENTITIES:
            _ACTIVE_ALGORITHM_ENTITIES[entry_id] = {}
        _ACTIVE_ALGORITHM_ENTITIES[entry_id][CalibrationMode.MPC_CALIBRATION] = [
            f"{bt_climate.unique_id}_virtual_temp",
            f"{bt_climate.unique_id}_mpc_gain", 
            f"{bt_climate.unique_id}_mpc_loss",
            f"{bt_climate.unique_id}_mpc_ka",
            f"{bt_climate.unique_id}_mpc_status",
        ]
        
        _LOGGER.debug(
            "Better Thermostat %s: Created MPC sensors for entry %s",
            bt_climate.device_name,
            entry_id,
        )
    
    # TODO: Hier können weitere Algorithmen hinzugefügt werden
    # if CalibrationMode.PID_CALIBRATION in current_algorithms:
    #     pid_sensors = [...]
    #     algorithm_sensors.extend(pid_sensors)
    
    return algorithm_sensors


async def _register_dynamic_entity_callback(
    hass: HomeAssistant, entry: ConfigEntry, bt_climate, async_add_entities
) -> None:
    """Register callback for dynamic entity management."""
    
    @callback
    def _on_config_change(data):
        """Handle configuration changes that might affect entity requirements."""
        _LOGGER.debug(
            "Better Thermostat %s: Configuration change detected via signal, checking entity requirements",
            bt_climate.device_name,
        )
        hass.async_create_task(
            _handle_dynamic_entity_update(hass, entry, bt_climate, async_add_entities)
        )
    
    # Store callback für späteren Cleanup
    _ENTITY_CLEANUP_CALLBACKS[entry.entry_id] = _on_config_change
    
    # Listen to configuration change signals
    signal_key = f"bt_config_changed_{entry.entry_id}"
    unsubscribe = async_dispatcher_connect(hass, signal_key, _on_config_change)
    
    # Store unsubscribe function for cleanup
    _DISPATCHER_UNSUBSCRIBES[entry.entry_id] = unsubscribe


async def _handle_dynamic_entity_update(
    hass: HomeAssistant, entry: ConfigEntry, bt_climate, async_add_entities
) -> None:
    """Handle dynamic entity creation/removal based on configuration."""
    entry_id = entry.entry_id
    current_algorithms = _get_active_algorithms(bt_climate)
    had_algorithm_entities = entry_id in _ACTIVE_ALGORITHM_ENTITIES
    previous_algorithms = set(_ACTIVE_ALGORITHM_ENTITIES.get(entry_id, {}).keys()) if had_algorithm_entities else set()
    
    # Prüfe auf Änderungen bei den Algorithmen
    algorithms_added = current_algorithms - previous_algorithms
    algorithms_removed = previous_algorithms - current_algorithms
    
    if algorithms_added or algorithms_removed:
        _LOGGER.info(
            "Better Thermostat %s: Algorithm configuration changed. Added: %s, Removed: %s",
            bt_climate.device_name,
            [alg.value if hasattr(alg, 'value') else str(alg) for alg in algorithms_added],
            [alg.value if hasattr(alg, 'value') else str(alg) for alg in algorithms_removed],
        )
        
        # Setup neue algorithmus-spezifische Sensoren
        new_sensors = await _setup_algorithm_sensors(hass, entry, bt_climate)
        if new_sensors:
            async_add_entities(new_sensors, True)


async def _cleanup_stale_algorithm_entities(
    hass: HomeAssistant, entry_id: str, bt_climate, current_algorithms: set
) -> None:
    """Remove algorithm-specific entities that are no longer needed."""
    if entry_id not in _ACTIVE_ALGORITHM_ENTITIES:
        return
    
    entity_registry = async_get_entity_registry(hass)
    tracked_algorithms = _ACTIVE_ALGORITHM_ENTITIES[entry_id]
    
    total_removed = 0
    algorithms_to_remove = []
    
    for algorithm, entity_unique_ids in tracked_algorithms.items():
        if algorithm not in current_algorithms:
            # Dieser Algorithmus ist nicht mehr aktiv - Entitäten entfernen
            removed_count = 0
            for entity_unique_id in entity_unique_ids:
                entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, entity_unique_id)
                if entity_id:
                    try:
                        entity_registry.async_remove(entity_id)
                        removed_count += 1
                        _LOGGER.debug(
                            "Better Thermostat %s: Removed %s entity %s",
                            bt_climate.device_name,
                            algorithm.value if hasattr(algorithm, 'value') else algorithm,
                            entity_id,
                        )
                    except Exception as e:
                        _LOGGER.warning(
                            "Better Thermostat %s: Failed to remove %s entity %s: %s",
                            bt_climate.device_name,
                            algorithm.value if hasattr(algorithm, 'value') else algorithm,
                            entity_id,
                            e,
                        )
            
            if removed_count > 0:
                _LOGGER.info(
                    "Better Thermostat %s: Removed %d %s entities",
                    bt_climate.device_name,
                    removed_count,
                    algorithm.value if hasattr(algorithm, 'value') else algorithm,
                )
                total_removed += removed_count
            
            algorithms_to_remove.append(algorithm)
    
    # Cleanup tracking für entfernte Algorithmen
    for algorithm in algorithms_to_remove:
        del _ACTIVE_ALGORITHM_ENTITIES[entry_id][algorithm]
    
    # Entferne entry_id komplett wenn keine Algorithmen mehr getrackt werden
    if not _ACTIVE_ALGORITHM_ENTITIES[entry_id]:
        del _ACTIVE_ALGORITHM_ENTITIES[entry_id]


def _get_active_algorithms(bt_climate) -> set:
    """Get set of calibration algorithms currently in use by any TRV."""
    if not hasattr(bt_climate, "real_trvs") or not bt_climate.real_trvs:
        return set()
        
    active_algorithms = set()
    for trv_id, trv in bt_climate.real_trvs.items():
        advanced = trv.get("advanced", {})
        calibration_mode = advanced.get(CONF_CALIBRATION_MODE)
        if calibration_mode:
            # Konvertiere String zu Enum falls nötig
            if isinstance(calibration_mode, str):
                try:
                    calibration_mode = CalibrationMode(calibration_mode)
                except ValueError:
                    _LOGGER.warning(
                        "Better Thermostat %s: Invalid calibration mode '%s' for TRV %s",
                        bt_climate.device_name,
                        calibration_mode,
                        trv_id,
                    )
                    continue
            active_algorithms.add(calibration_mode)
    
    return active_algorithms


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload sensor entry and cleanup tracking."""
    entry_id = entry.entry_id
    
    # Unsubscribe from dispatcher signals
    unsubscribe = _DISPATCHER_UNSUBSCRIBES.pop(entry_id, None)
    if unsubscribe:
        unsubscribe()
    
    # Cleanup tracking data
    _ACTIVE_ALGORITHM_ENTITIES.pop(entry_id, None)
    _ENTITY_CLEANUP_CALLBACKS.pop(entry_id, None)
    
    return True


class BetterThermostatExternalTempSensor(SensorEntity):
    """Representation of a Better Thermostat External Temperature Sensor (EMA)."""

    _attr_has_entity_name = True
    _attr_name = "Temperature EMA"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_should_poll = False

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        # Use the climate entity's unique_id as prefix
        self._attr_unique_id = f"{bt_climate.unique_id}_external_temp_ema"
        self._attr_device_info = bt_climate.device_info

    async def async_added_to_hass(self):
        """Register callbacks."""
        # Listen to state changes of the climate entity
        if self._bt_climate.entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._bt_climate.entity_id], self._on_climate_update
                )
            )
        else:
            _LOGGER.warning(
                "Better Thermostat climate entity has no entity_id yet. "
                "Sensor update might be delayed."
            )

        # Also update initially
        self._update_state()

    @callback
    def _on_climate_update(self, event):
        """Handle climate entity update."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self):
        """Update state from climate entity."""
        # The EMA is stored in `cur_temp_filtered` attribute of the climate entity instance
        val = getattr(self._bt_climate, "cur_temp_filtered", None)
        if val is None:
            val = getattr(self._bt_climate, "external_temp_ema", None)

        if val is not None:
            try:
                self._attr_native_value = float(val)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatTempSlopeSensor(SensorEntity):
    """Representation of a Better Thermostat Temperature Slope Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Temperature Slope"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "K/min"
    _attr_should_poll = False
    _attr_icon = "mdi:chart-line"

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_temp_slope"
        self._attr_device_info = bt_climate.device_info

    async def async_added_to_hass(self):
        """Register callbacks."""
        if self._bt_climate.entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._bt_climate.entity_id], self._on_climate_update
                )
            )
        self._update_state()

    @callback
    def _on_climate_update(self, event):
        """Handle climate entity update."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self):
        """Update state from climate entity."""
        val = getattr(self._bt_climate, "temp_slope", None)
        if val is not None:
            try:
                self._attr_native_value = round(float(val), 4)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatVirtualTempSensor(SensorEntity):
    """Representation of a Better Thermostat Virtual Temperature Sensor (MPC)."""

    _attr_has_entity_name = True
    _attr_name = "Virtual Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_should_poll = False
    _attr_icon = "mdi:thermometer-auto"

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_virtual_temp"
        self._attr_device_info = bt_climate.device_info

    async def async_added_to_hass(self):
        """Register callbacks."""
        if self._bt_climate.entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._bt_climate.entity_id], self._on_climate_update
                )
            )
        self._update_state()

    @callback
    def _on_climate_update(self, event):
        """Handle climate entity update."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self):
        """Update state from climate entity."""
        # Try to find virtual temp in any TRV's calibration balance debug info
        val = None
        if hasattr(self._bt_climate, "real_trvs"):
            for trv_id, trv_data in self._bt_climate.real_trvs.items():
                cal_bal = trv_data.get("calibration_balance")
                if cal_bal and "debug" in cal_bal:
                    debug = cal_bal["debug"]
                    if "mpc_virtual_temp" in debug:
                        val = debug["mpc_virtual_temp"]
                        break

        if val is not None:
            try:
                self._attr_native_value = float(val)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatMpcGainSensor(SensorEntity):
    """Representation of a Better Thermostat MPC Gain Sensor."""

    _attr_has_entity_name = True
    _attr_name = "MPC Gain"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "K/min"
    _attr_should_poll = False
    _attr_icon = "mdi:thermometer-plus"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_mpc_gain"
        self._attr_device_info = bt_climate.device_info

    async def async_added_to_hass(self):
        """Register callbacks."""
        if self._bt_climate.entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._bt_climate.entity_id], self._on_climate_update
                )
            )
        self._update_state()

    @callback
    def _on_climate_update(self, event):
        """Handle climate entity update."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self):
        """Update state from climate entity."""
        val = None
        if hasattr(self._bt_climate, "real_trvs"):
            for trv_id, trv_data in self._bt_climate.real_trvs.items():
                cal_bal = trv_data.get("calibration_balance")
                if cal_bal and "debug" in cal_bal:
                    debug = cal_bal["debug"]
                    if "mpc_gain" in debug:
                        val = debug["mpc_gain"]
                        break

        if val is not None:
            try:
                self._attr_native_value = float(val)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatMpcLossSensor(SensorEntity):
    """Representation of a Better Thermostat MPC Loss Sensor."""

    _attr_has_entity_name = True
    _attr_name = "MPC Loss"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "K/min"
    _attr_should_poll = False
    _attr_icon = "mdi:thermometer-minus"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_mpc_loss"
        self._attr_device_info = bt_climate.device_info

    async def async_added_to_hass(self):
        """Register callbacks."""
        if self._bt_climate.entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._bt_climate.entity_id], self._on_climate_update
                )
            )
        self._update_state()

    @callback
    def _on_climate_update(self, event):
        """Handle climate entity update."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self):
        """Update state from climate entity."""
        val = None
        if hasattr(self._bt_climate, "real_trvs"):
            for trv_id, trv_data in self._bt_climate.real_trvs.items():
                cal_bal = trv_data.get("calibration_balance")
                if cal_bal and "debug" in cal_bal:
                    debug = cal_bal["debug"]
                    if "mpc_loss" in debug:
                        val = debug["mpc_loss"]
                        break

        if val is not None:
            try:
                self._attr_native_value = float(val)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatMpcKaSensor(SensorEntity):
    """Representation of a Better Thermostat MPC Ka (Insulation) Sensor."""

    _attr_has_entity_name = True
    _attr_name = "MPC Insulation (Ka)"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "K/min"
    _attr_should_poll = False
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_mpc_ka"
        self._attr_device_info = bt_climate.device_info

    async def async_added_to_hass(self):
        """Register callbacks."""
        if self._bt_climate.entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._bt_climate.entity_id], self._on_climate_update
                )
            )
        self._update_state()

    @callback
    def _on_climate_update(self, event):
        """Handle climate entity update."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self):
        """Update state from climate entity."""
        val = None
        if hasattr(self._bt_climate, "real_trvs"):
            for trv_id, trv_data in self._bt_climate.real_trvs.items():
                cal_bal = trv_data.get("calibration_balance")
                if cal_bal and "debug" in cal_bal:
                    debug = cal_bal["debug"]
                    if "mpc_ka" in debug:
                        val = debug["mpc_ka"]
                        break

        if val is not None:
            try:
                self._attr_native_value = float(val)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatSolarIntensitySensor(SensorEntity):
    """Representation of a Better Thermostat Solar Intensity Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Sun Intensity Heatup"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True  # We might need to poll the weather entity if updates are not strictly coupled
    _attr_icon = "mdi:solar-power"

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_solar_intensity"
        self._attr_device_info = bt_climate.device_info

    async def async_added_to_hass(self):
        """Register callbacks."""
        # Listen to state changes of the climate entity
        if self._bt_climate.entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._bt_climate.entity_id], self._on_climate_update
                )
            )
        self._update_state()

    @callback
    def _on_climate_update(self, event):
        """Handle climate entity update."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self):
        """Update state using utility function."""
        try:
            val = _get_current_solar_intensity(self._bt_climate)
            # Function returns 0.0-1.0, convert to %
            if val is not None:
                self._attr_native_value = round(float(val) * 100.0, 1)
            else:
                self._attr_native_value = 0.0
        except Exception:
            self._attr_native_value = None


class BetterThermostatMpcStatusSensor(SensorEntity):
    """Representation of a Better Thermostat MPC Status Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Learning Status"
    _attr_device_class = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_icon = "mdi:brain"

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_mpc_status"
        self._attr_device_info = bt_climate.device_info

    async def async_added_to_hass(self):
        """Register callbacks."""
        if self._bt_climate.entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._bt_climate.entity_id], self._on_climate_update
                )
            )
        self._update_state()

    @callback
    def _on_climate_update(self, event):
        """Handle climate entity update."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self):
        """Update state from climate entity."""
        created_ts = None
        gain = None
        loss = None
        confidence = None

        if hasattr(self._bt_climate, "real_trvs"):
            for trv_id, trv_data in self._bt_climate.real_trvs.items():
                cal_bal = trv_data.get("calibration_balance")
                if cal_bal and "debug" in cal_bal:
                    debug = cal_bal["debug"]
                    if "mpc_created_ts" in debug:
                        created_ts = debug["mpc_created_ts"]
                    if "mpc_gain" in debug:
                        gain = debug["mpc_gain"]
                    if "mpc_loss" in debug:
                        loss = debug["mpc_loss"]
                    if "trv_profile_conf" in debug:
                        confidence = debug["trv_profile_conf"]

                    if created_ts is not None:
                        break

        if created_ts is not None and created_ts > 0:
            age_seconds = time.time() - float(created_ts)
            days = age_seconds / 86400.0
            confidence_val = float(confidence) if confidence is not None else 0.0

            if days < 1.0:
                self._attr_native_value = "training"
            elif confidence_val >= 0.7:
                self._attr_native_value = "trained"
            elif confidence_val >= 0.4:
                self._attr_native_value = "optimizing"
            else:
                self._attr_native_value = "training"

            self._attr_extra_state_attributes = {
                "created_at": float(created_ts),
                "days_trained": round(days, 2),
                "mpc_gain": gain,
                "mpc_loss": loss,
                "profile_confidence": confidence,
            }
        else:
            self._attr_native_value = "unknown"
            self._attr_extra_state_attributes = {}
