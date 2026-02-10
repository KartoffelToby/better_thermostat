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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import (
    EntityRegistry,
    async_get as async_get_entity_registry,
)
from homeassistant.helpers.event import async_track_state_change_event

from .calibration import _get_current_solar_intensity
from .utils.const import CONF_CALIBRATION_MODE, CalibrationMode

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"

# Globale Tracking-Variablen für aktive algorithmus-spezifische Entitäten
_ACTIVE_ALGORITHM_ENTITIES = {}
_ENTITY_CLEANUP_CALLBACKS = {}
_DISPATCHER_UNSUBSCRIBES = {}  # Store unsubscribe functions

# Globale Tracking-Variablen für aktive Preset Number Entitäten
_ACTIVE_PRESET_NUMBERS = {}  # {entry_id: {unique_id: {"preset": preset_name}, ...}}
_ACTIVE_PID_NUMBERS = {}  # {entry_id: {unique_id: {"trv": trv_entity_id, "param": parameter}, ...}}
_ACTIVE_SWITCH_ENTITIES = {}  # {entry_id: {unique_id: {"trv": trv_entity_id, "type": kind}, ...}}


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
        BetterThermostatExternalTemp1hEMASensor(bt_climate),
        BetterThermostatTempSlopeSensor(bt_climate),
        BetterThermostatHeatingPowerSensor(bt_climate),
        BetterThermostatHeatLossSensor(bt_climate),
        BetterThermostatSolarIntensitySensor(bt_climate),
    ]

    # Dynamische algorithmus-spezifische Sensor-Erstellung
    algorithm_sensors = await _setup_algorithm_sensors(hass, entry, bt_climate)
    sensors.extend(algorithm_sensors)

    async_add_entities(sensors, True)

    # Registriere Callback für dynamische Entity-Updates
    await _register_dynamic_entity_callback(hass, entry, bt_climate, async_add_entities)


async def _setup_algorithm_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    bt_climate,
    algorithms_to_create: set | None = None,
) -> list:
    """Set up algorithm-specific sensors based on current configuration.

    Parameters
    ----------
    algorithms_to_create : set | None
        When provided, only sensors for these algorithms are created.
        When ``None`` (initial setup), all active algorithms are created.
    """
    algorithm_sensors = []
    entry_id = entry.entry_id
    current_algorithms = _get_active_algorithms(bt_climate)

    if algorithms_to_create is not None:
        # Only create sensors for newly added algorithms
        current_algorithms = current_algorithms & algorithms_to_create

    # Cleanup stale algorithm entities from previous configurations
    await _cleanup_stale_algorithm_entities(
        hass, entry_id, bt_climate, current_algorithms
    )

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
    previous_algorithms = (
        set(_ACTIVE_ALGORITHM_ENTITIES.get(entry_id, {}))
        if had_algorithm_entities
        else set()
    )

    # Prüfe auf Änderungen bei den Algorithmen
    algorithms_added = current_algorithms - previous_algorithms
    algorithms_removed = previous_algorithms - current_algorithms

    if algorithms_added or algorithms_removed:
        _LOGGER.info(
            "Better Thermostat %s: Algorithm configuration changed. Added: %s, Removed: %s",
            bt_climate.device_name,
            [
                alg.value if hasattr(alg, "value") else str(alg)
                for alg in algorithms_added
            ],
            [
                alg.value if hasattr(alg, "value") else str(alg)
                for alg in algorithms_removed
            ],
        )

        # Setup only newly added algorithm-specific sensors
        new_sensors = await _setup_algorithm_sensors(
            hass, entry, bt_climate, algorithms_to_create=algorithms_added
        )
        if new_sensors:
            async_add_entities(new_sensors, True)

    # Always check and cleanup entities regardless of algorithm changes
    # This ensures preset and PID number cleanup happens even when only presets change
    await _cleanup_unused_number_entities(hass, entry_id, bt_climate)


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
                entity_id = entity_registry.async_get_entity_id(
                    "sensor", DOMAIN, entity_unique_id
                )
                if entity_id:
                    try:
                        entity_registry.async_remove(entity_id)
                        removed_count += 1
                        _LOGGER.debug(
                            "Better Thermostat %s: Removed %s entity %s",
                            bt_climate.device_name,
                            algorithm.value
                            if hasattr(algorithm, "value")
                            else algorithm,
                            entity_id,
                        )
                    except Exception as e:
                        _LOGGER.warning(
                            "Better Thermostat %s: Failed to remove %s entity %s: %s",
                            bt_climate.device_name,
                            algorithm.value
                            if hasattr(algorithm, "value")
                            else algorithm,
                            entity_id,
                            e,
                        )

            if removed_count > 0:
                _LOGGER.info(
                    "Better Thermostat %s: Removed %d %s entities",
                    bt_climate.device_name,
                    removed_count,
                    algorithm.value if hasattr(algorithm, "value") else algorithm,
                )
                total_removed += removed_count

            if removed_count == len(entity_unique_ids):
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


async def _cleanup_unused_number_entities(
    hass: HomeAssistant, entry_id: str, bt_climate
) -> None:
    """Clean up unused preset and PID number entities."""
    entity_registry = async_get_entity_registry(hass)

    # Get current enabled presets from climate entity (guard against None)
    current_presets = set(bt_climate.preset_modes or [])
    current_presets.discard("none")  # Remove "none" as it doesn't have a number entity

    # Cleanup unused preset number entities
    await _cleanup_preset_number_entities(
        hass, entity_registry, entry_id, bt_climate, current_presets
    )

    # Cleanup unused PID number entities
    await _cleanup_pid_number_entities(hass, entity_registry, entry_id, bt_climate)

    # Cleanup unused switch entities (PID Auto-Tune switches)
    await _cleanup_pid_switch_entities(hass, entity_registry, entry_id, bt_climate)


async def _cleanup_preset_number_entities(
    hass: HomeAssistant,
    entity_registry: EntityRegistry,
    entry_id: str,
    bt_climate,
    current_presets: set,
) -> None:
    """Remove preset number entities for disabled presets."""
    tracked_presets = _ACTIVE_PRESET_NUMBERS.get(entry_id, {})

    # Find number entities to remove
    entities_to_remove = []
    for preset_unique_id, meta in tracked_presets.items():
        preset_name = meta.get("preset")
        if preset_name and preset_name not in current_presets:
            entities_to_remove.append((preset_unique_id, preset_name))

    # Remove entities from registry
    removed_count = 0
    for preset_unique_id, preset_name in entities_to_remove:
        entity_id = entity_registry.async_get_entity_id(
            "number", DOMAIN, preset_unique_id
        )
        if entity_id:
            try:
                entity_registry.async_remove(entity_id)
                removed_count += 1
                _LOGGER.debug(
                    "Better Thermostat %s: Removed unused preset number entity %s (preset: %s)",
                    bt_climate.device_name,
                    entity_id,
                    preset_name,
                )
            except Exception as e:
                _LOGGER.warning(
                    "Better Thermostat %s: Failed to remove preset number entity %s: %s",
                    bt_climate.device_name,
                    entity_id,
                    e,
                )

    # Update tracking to reflect current preset configuration
    _ACTIVE_PRESET_NUMBERS[entry_id] = {
        f"{bt_climate.unique_id}_preset_{preset}": {"preset": preset}
        for preset in current_presets
    }

    if removed_count > 0:
        _LOGGER.info(
            "Better Thermostat %s: Cleaned up %d unused preset number entities",
            bt_climate.device_name,
            removed_count,
        )


async def _cleanup_pid_number_entities(
    hass: HomeAssistant, entity_registry: EntityRegistry, entry_id: str, bt_climate
) -> None:
    """Remove PID number entities for TRVs no longer using PID calibration."""
    tracked_pid_numbers = _ACTIVE_PID_NUMBERS.get(entry_id, {})

    # Get current TRVs using PID calibration - consistent with switch cleanup
    current_pid_trvs = set()
    if hasattr(bt_climate, "real_trvs") and bt_climate.real_trvs:
        for trv_entity_id, trv_data in bt_climate.real_trvs.items():
            advanced = trv_data.get("advanced", {})
            calibration_mode = advanced.get(CONF_CALIBRATION_MODE)

            # Normalize string values to CalibrationMode enum
            try:
                if isinstance(calibration_mode, str):
                    calibration_mode = CalibrationMode(calibration_mode)
            except (ValueError, TypeError):
                # Invalid or unknown calibration mode, skip
                continue

            if calibration_mode == CalibrationMode.PID_CALIBRATION:
                current_pid_trvs.add(trv_entity_id)

    # Find PID number entities to remove
    entities_to_remove = []
    for pid_unique_id, meta in tracked_pid_numbers.items():
        trv_id = meta.get("trv")
        if trv_id and trv_id not in current_pid_trvs:
            entities_to_remove.append(pid_unique_id)

    # Remove entities from registry
    removed_count = 0
    for pid_unique_id in entities_to_remove:
        entity_id = entity_registry.async_get_entity_id("number", DOMAIN, pid_unique_id)
        if entity_id:
            try:
                entity_registry.async_remove(entity_id)
                removed_count += 1
                _LOGGER.debug(
                    "Better Thermostat %s: Removed unused PID number entity %s",
                    bt_climate.device_name,
                    entity_id,
                )
            except Exception as e:
                _LOGGER.warning(
                    "Better Thermostat %s: Failed to remove PID number entity %s: %s",
                    bt_climate.device_name,
                    entity_id,
                    e,
                )

    # Update tracking to reflect current PID configuration
    current_pid_map = {}
    for trv_entity_id in current_pid_trvs:
        for param in ["kp", "ki", "kd"]:
            uid = f"{bt_climate.unique_id}_{trv_entity_id}_pid_{param}"
            current_pid_map[uid] = {"trv": trv_entity_id, "param": param}

    _ACTIVE_PID_NUMBERS[entry_id] = current_pid_map

    if removed_count > 0:
        _LOGGER.info(
            "Better Thermostat %s: Cleaned up %d unused PID number entities",
            bt_climate.device_name,
            removed_count,
        )


async def _cleanup_pid_switch_entities(
    hass: HomeAssistant, entity_registry: EntityRegistry, entry_id: str, bt_climate
) -> None:
    """Remove PID switch entities for TRVs no longer using PID calibration and child lock switches for removed TRVs."""
    tracked_switches = _ACTIVE_SWITCH_ENTITIES.get(entry_id, {})

    # Get current TRVs using PID calibration
    current_pid_trvs = set()
    if hasattr(bt_climate, "real_trvs") and bt_climate.real_trvs:
        for trv_entity_id, trv_data in bt_climate.real_trvs.items():
            advanced = trv_data.get("advanced", {})
            calibration_mode = advanced.get(CONF_CALIBRATION_MODE)

            # Normalize string values to CalibrationMode enum
            try:
                if isinstance(calibration_mode, str):
                    calibration_mode = CalibrationMode(calibration_mode)
            except (ValueError, TypeError):
                # Invalid or unknown calibration mode, skip
                continue

            if calibration_mode == CalibrationMode.PID_CALIBRATION:
                current_pid_trvs.add(trv_entity_id)

    # Find switch entities to remove using stored metadata
    entities_to_remove = []
    for switch_unique_id, meta in tracked_switches.items():
        trv_id = meta.get("trv")
        kind = meta.get("type")
        should_remove = False

        if kind == "pid_auto_tune":
            if trv_id not in current_pid_trvs:
                should_remove = True
        elif kind == "child_lock":
            # Remove child lock switches for TRVs that no longer exist
            if (
                not hasattr(bt_climate, "real_trvs")
                or not bt_climate.real_trvs
                or trv_id not in bt_climate.real_trvs
            ):
                should_remove = True

        if should_remove:
            entities_to_remove.append(switch_unique_id)

    # Remove entities from registry
    removed_count = 0
    for switch_unique_id in entities_to_remove:
        entity_id = entity_registry.async_get_entity_id(
            "switch", DOMAIN, switch_unique_id
        )
        if entity_id:
            try:
                entity_registry.async_remove(entity_id)
                removed_count += 1
                _LOGGER.debug(
                    "Better Thermostat %s: Removed unused switch entity %s",
                    bt_climate.device_name,
                    entity_id,
                )
            except Exception as e:
                _LOGGER.warning(
                    "Better Thermostat %s: Failed to remove switch entity %s: %s",
                    bt_climate.device_name,
                    entity_id,
                    e,
                )

    # Update tracking to reflect current switch configuration
    current_switch_map = {}
    # Add PID Auto-Tune switches for current PID TRVs
    for trv_entity_id in current_pid_trvs:
        uid = f"{bt_climate.unique_id}_{trv_entity_id}_pid_auto_tune"
        current_switch_map[uid] = {"trv": trv_entity_id, "type": "pid_auto_tune"}

    # Add Child Lock switches (always present for all TRVs)
    if hasattr(bt_climate, "real_trvs") and bt_climate.real_trvs:
        for trv_entity_id in bt_climate.real_trvs:
            uid = f"{bt_climate.unique_id}_{trv_entity_id}_child_lock"
            current_switch_map[uid] = {"trv": trv_entity_id, "type": "child_lock"}

    _ACTIVE_SWITCH_ENTITIES[entry_id] = current_switch_map

    if removed_count > 0:
        _LOGGER.info(
            "Better Thermostat %s: Cleaned up %d unused switch entities",
            bt_climate.device_name,
            removed_count,
        )


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
    _ACTIVE_PRESET_NUMBERS.pop(entry_id, None)
    _ACTIVE_PID_NUMBERS.pop(entry_id, None)
    _ACTIVE_SWITCH_ENTITIES.pop(entry_id, None)

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


class BetterThermostatExternalTemp1hEMASensor(SensorEntity):
    """Representation of a Better Thermostat External Temperature 1h EMA Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Temperature EMA 1h"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_should_poll = False
    _attr_suggested_display_precision = 2

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_external_temp_ema_1h"
        self._attr_device_info = bt_climate.device_info
        # EMA state
        self._ema_value = None
        self._last_update_ts = None
        self._tau_s = 3600.0  # 1 hour

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
        """Handle update from the climate entity."""
        self._update_state()
        self.async_write_ha_state()

    def _update_ema(self, new_value):
        """Update the 1h EMA with a new value."""
        import math
        from time import monotonic

        now = monotonic()
        prev_ts = self._last_update_ts
        prev_ema = self._ema_value

        if prev_ts is None or prev_ema is None:
            ema = float(new_value)
        else:
            dt_s = max(0.0, now - prev_ts)
            alpha = 1.0 - math.exp(-dt_s / self._tau_s) if dt_s > 0 else 0.0
            ema = prev_ema + alpha * (new_value - prev_ema)

        self._ema_value = ema
        self._last_update_ts = now

    def _update_state(self):
        """Update state from internal EMA."""
        # Prefer filtered EMA from climate, fall back to external_temp_ema
        val = getattr(self._bt_climate, "cur_temp_filtered", None)
        if val is None:
            val = getattr(self._bt_climate, "external_temp_ema", None)

        if val is not None:
            try:
                self._update_ema(float(val))
                self._attr_native_value = round(float(self._ema_value), 2)
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


class BetterThermostatHeatingPowerSensor(SensorEntity):
    """Representation of a Better Thermostat Heating Power Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Heating Power"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "K/min"
    _attr_should_poll = False
    _attr_icon = "mdi:thermometer-plus"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_heating_power"
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
        val = getattr(self._bt_climate, "heating_power", None)
        if val is not None:
            try:
                self._attr_native_value = float(val)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatHeatLossSensor(SensorEntity):
    """Representation of a Better Thermostat Heat Loss Sensor."""

    _attr_has_entity_name = True
    _attr_name = "Heat Loss"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "K/min"
    _attr_should_poll = False
    _attr_icon = "mdi:thermometer-minus"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, bt_climate):
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_heat_loss"
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
        val = getattr(self._bt_climate, "heat_loss_rate", None)
        if val is not None:
            try:
                self._attr_native_value = float(val)
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

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Follow HA guidelines: return False when entity should be unavailable
        # This prevents "unknown" states and properly shows "unavailable"
        if (
            not hasattr(self._bt_climate, "_available")
            or not self._bt_climate._available
        ):
            return False
        if getattr(self._bt_climate, "window_open", False):
            return False
        if (
            hasattr(self._bt_climate, "hvac_mode")
            and self._bt_climate.hvac_mode == "off"
        ):
            return False
        return True

    def _update_state(self):
        """Update state from climate entity."""
        # Try to find virtual temp in any TRV's calibration balance debug info
        val = None
        if hasattr(self._bt_climate, "real_trvs"):
            for _, trv_data in self._bt_climate.real_trvs.items():
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

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Follow HA guidelines: return False when entity should be unavailable
        # This prevents "unknown" states and properly shows "unavailable"
        if (
            not hasattr(self._bt_climate, "_available")
            or not self._bt_climate._available
        ):
            return False
        if getattr(self._bt_climate, "window_open", False):
            return False
        if (
            hasattr(self._bt_climate, "hvac_mode")
            and self._bt_climate.hvac_mode == "off"
        ):
            return False
        return True

    def _update_state(self):
        """Update state from climate entity."""
        val = None
        if hasattr(self._bt_climate, "real_trvs"):
            for _, trv_data in self._bt_climate.real_trvs.items():
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

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Follow HA guidelines: return False when entity should be unavailable
        # This prevents "unknown" states and properly shows "unavailable"
        if (
            not hasattr(self._bt_climate, "_available")
            or not self._bt_climate._available
        ):
            return False
        if getattr(self._bt_climate, "window_open", False):
            return False
        if (
            hasattr(self._bt_climate, "hvac_mode")
            and self._bt_climate.hvac_mode == "off"
        ):
            return False
        return True

    def _update_state(self):
        """Update state from climate entity."""
        val = None
        if hasattr(self._bt_climate, "real_trvs"):
            for _, trv_data in self._bt_climate.real_trvs.items():
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
    _attr_native_unit_of_measurement = "1/min"
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

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Follow HA guidelines: return False when entity should be unavailable
        # This prevents "unknown" states and properly shows "unavailable"
        if (
            not hasattr(self._bt_climate, "_available")
            or not self._bt_climate._available
        ):
            return False
        if getattr(self._bt_climate, "window_open", False):
            return False
        if (
            hasattr(self._bt_climate, "hvac_mode")
            and self._bt_climate.hvac_mode == "off"
        ):
            return False
        return True

    def _update_state(self):
        """Update state from climate entity."""
        val = None
        if hasattr(self._bt_climate, "real_trvs"):
            for _, trv_data in self._bt_climate.real_trvs.items():
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
            for _, trv_data in self._bt_climate.real_trvs.items():
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
