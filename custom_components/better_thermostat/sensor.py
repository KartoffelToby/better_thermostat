"""Better Thermostat Sensor Platform."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import (
    EntityRegistry,
    async_get as async_get_entity_registry,
)
from homeassistant.helpers.event import async_track_state_change_event

from .calibration import _get_current_solar_intensity
from .utils.const import CONF_CALIBRATION_MODE, CalibrationMode

if TYPE_CHECKING:
    from .climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"

# Globale Tracking-Variablen für aktive algorithmus-spezifische Entitäten
_ACTIVE_ALGORITHM_ENTITIES: dict[str, dict[CalibrationMode, list[str]]] = {}
_ENTITY_CLEANUP_CALLBACKS: dict[str, Callable[..., None]] = {}
_DISPATCHER_UNSUBSCRIBES: dict[str, Callable[[], None]] = {}

# Globale Tracking-Variablen für aktive Preset Number Entitäten
_ACTIVE_PRESET_NUMBERS: dict[
    str, dict[str | None, dict[str, str]]
] = {}  # {entry_id: {unique_id: {"preset": preset_name}, ...}}
_ACTIVE_PID_NUMBERS: dict[
    str, dict[str | None, dict[str, str]]
] = {}  # {entry_id: {unique_id: {"trv": trv_entity_id, "param": parameter}, ...}}
_ACTIVE_SWITCH_ENTITIES: dict[
    str, dict[str | None, dict[str, str]]
] = {}  # {entry_id: {unique_id: {"trv": trv_entity_id, "type": kind}, ...}}


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

    sensors: list[SensorEntity] = [
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
    bt_climate: BetterThermostat,
    algorithms_to_create: set[CalibrationMode] | None = None,
) -> list[SensorEntity]:
    """Set up algorithm-specific sensors based on current configuration.

    Parameters
    ----------
    algorithms_to_create : set | None
        When provided, only sensors for these algorithms are created.
        When ``None`` (initial setup), all active algorithms are created.
    """
    algorithm_sensors: list[SensorEntity] = []
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
    hass: HomeAssistant,
    entry: ConfigEntry,
    bt_climate: BetterThermostat,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register callback for dynamic entity management."""

    @callback
    def _on_config_change(data: object) -> None:
        """Handle configuration changes that might affect entity requirements."""
        _LOGGER.debug(
            "Better Thermostat %s: Configuration change detected via signal, checking entity requirements",
            bt_climate.device_name,
        )
        hass.async_create_background_task(
            _handle_dynamic_entity_update(hass, entry, bt_climate, async_add_entities),
            name=f"bt_dynamic_entity_update_{entry.entry_id}",
        )

    # Store callback für späteren Cleanup
    _ENTITY_CLEANUP_CALLBACKS[entry.entry_id] = _on_config_change

    # Listen to configuration change signals
    signal_key = f"bt_config_changed_{entry.entry_id}"
    unsubscribe = async_dispatcher_connect(hass, signal_key, _on_config_change)

    # Store unsubscribe function for cleanup
    _DISPATCHER_UNSUBSCRIBES[entry.entry_id] = unsubscribe


async def _handle_dynamic_entity_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
    bt_climate: BetterThermostat,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Handle dynamic entity creation/removal based on configuration."""
    entry_id = entry.entry_id
    current_algorithms = _get_active_algorithms(bt_climate)
    previous_algorithms = set(_ACTIVE_ALGORITHM_ENTITIES.get(entry_id, {}))

    # Prüfe auf Änderungen bei den Algorithmen
    algorithms_added = current_algorithms - previous_algorithms
    algorithms_removed = previous_algorithms - current_algorithms

    if algorithms_added or algorithms_removed:
        _LOGGER.info(
            "Better Thermostat %s: Algorithm configuration changed. Added: %s, Removed: %s",
            bt_climate.device_name,
            [alg.value for alg in algorithms_added],
            [alg.value for alg in algorithms_removed],
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
    hass: HomeAssistant,
    entry_id: str,
    bt_climate: BetterThermostat,
    current_algorithms: set[CalibrationMode],
) -> None:
    """Remove algorithm-specific entities that are no longer needed."""
    if entry_id not in _ACTIVE_ALGORITHM_ENTITIES:
        return

    entity_registry = async_get_entity_registry(hass)
    tracked_algorithms = _ACTIVE_ALGORITHM_ENTITIES[entry_id]

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
                            algorithm.value,
                            entity_id,
                        )
                    except Exception as e:
                        _LOGGER.warning(
                            "Better Thermostat %s: Failed to remove %s entity %s: %s",
                            bt_climate.device_name,
                            algorithm.value,
                            entity_id,
                            e,
                        )

            if removed_count > 0:
                _LOGGER.info(
                    "Better Thermostat %s: Removed %d %s entities",
                    bt_climate.device_name,
                    removed_count,
                    algorithm.value,
                )

            if removed_count == len(entity_unique_ids):
                algorithms_to_remove.append(algorithm)

    # Cleanup tracking für entfernte Algorithmen
    for algorithm in algorithms_to_remove:
        del _ACTIVE_ALGORITHM_ENTITIES[entry_id][algorithm]

    # Entferne entry_id komplett wenn keine Algorithmen mehr getrackt werden
    if not _ACTIVE_ALGORITHM_ENTITIES[entry_id]:
        del _ACTIVE_ALGORITHM_ENTITIES[entry_id]


def _get_active_algorithms(bt_climate: BetterThermostat) -> set[CalibrationMode]:
    """Get set of calibration algorithms currently in use by any TRV."""
    if not bt_climate.real_trvs:
        return set()

    active_algorithms: set[CalibrationMode] = set()
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


def _get_pid_trvs(bt_climate: BetterThermostat) -> set[str]:
    """Return entity IDs of TRVs currently using PID calibration."""
    pid_trvs: set[str] = set()
    if not bt_climate.real_trvs:
        return pid_trvs
    for trv_entity_id, trv_data in bt_climate.real_trvs.items():
        advanced = trv_data.get("advanced", {})
        calibration_mode = advanced.get(CONF_CALIBRATION_MODE)
        # Normalize string values to CalibrationMode enum
        if isinstance(calibration_mode, str):
            try:
                calibration_mode = CalibrationMode(calibration_mode)
            except (ValueError, TypeError):
                continue
        if calibration_mode == CalibrationMode.PID_CALIBRATION:
            pid_trvs.add(trv_entity_id)
    return pid_trvs


async def _cleanup_unused_number_entities(
    hass: HomeAssistant, entry_id: str, bt_climate: BetterThermostat
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
    bt_climate: BetterThermostat,
    current_presets: set[str],
) -> None:
    """Remove preset number entities for disabled presets."""
    tracked_presets = _ACTIVE_PRESET_NUMBERS.get(entry_id, {})

    # Find number entities to remove
    entities_to_remove = []
    for preset_unique_id, meta in tracked_presets.items():
        preset_name = meta.get("preset")
        if preset_name and preset_name not in current_presets:
            entities_to_remove.append((preset_unique_id, preset_name))

    # Remove entities from registry – only delete tracking key on success
    removed_count = 0
    for preset_unique_id, preset_name in entities_to_remove:
        if preset_unique_id is None:
            continue
        entity_id = entity_registry.async_get_entity_id(
            "number", DOMAIN, preset_unique_id
        )
        if entity_id:
            try:
                entity_registry.async_remove(entity_id)
                removed_count += 1
                tracked_presets.pop(preset_unique_id, None)
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

    # Merge new entries for current presets without wiping failed removals
    for preset in current_presets:
        uid = f"{bt_climate.unique_id}_preset_{preset}"
        tracked_presets[uid] = {"preset": preset}
    _ACTIVE_PRESET_NUMBERS[entry_id] = tracked_presets

    if removed_count > 0:
        _LOGGER.info(
            "Better Thermostat %s: Cleaned up %d unused preset number entities",
            bt_climate.device_name,
            removed_count,
        )


async def _cleanup_pid_number_entities(
    hass: HomeAssistant,
    entity_registry: EntityRegistry,
    entry_id: str,
    bt_climate: BetterThermostat,
) -> None:
    """Remove PID number entities for TRVs no longer using PID calibration."""
    tracked_pid_numbers = _ACTIVE_PID_NUMBERS.get(entry_id, {})
    current_pid_trvs = _get_pid_trvs(bt_climate)

    # Find PID number entities to remove
    entities_to_remove = []
    for pid_unique_id, meta in tracked_pid_numbers.items():
        trv_id = meta.get("trv")
        if trv_id and trv_id not in current_pid_trvs:
            entities_to_remove.append(pid_unique_id)

    # Remove entities from registry – only delete tracking key on success
    removed_count = 0
    for pid_unique_id in entities_to_remove:
        if pid_unique_id is None:
            continue
        entity_id = entity_registry.async_get_entity_id("number", DOMAIN, pid_unique_id)
        if entity_id:
            try:
                entity_registry.async_remove(entity_id)
                removed_count += 1
                tracked_pid_numbers.pop(pid_unique_id, None)
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

    # Merge new entries for current PID TRVs without wiping failed removals
    for trv_entity_id in current_pid_trvs:
        for param in ["kp", "ki", "kd"]:
            uid = f"{bt_climate.unique_id}_{trv_entity_id}_pid_{param}"
            tracked_pid_numbers[uid] = {"trv": trv_entity_id, "param": param}
    _ACTIVE_PID_NUMBERS[entry_id] = tracked_pid_numbers

    if removed_count > 0:
        _LOGGER.info(
            "Better Thermostat %s: Cleaned up %d unused PID number entities",
            bt_climate.device_name,
            removed_count,
        )


async def _cleanup_pid_switch_entities(
    hass: HomeAssistant,
    entity_registry: EntityRegistry,
    entry_id: str,
    bt_climate: BetterThermostat,
) -> None:
    """Remove PID switch and child lock entities for TRVs that changed or were removed."""
    tracked_switches = _ACTIVE_SWITCH_ENTITIES.get(entry_id, {})
    current_pid_trvs = _get_pid_trvs(bt_climate)

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
            if not bt_climate.real_trvs or trv_id not in bt_climate.real_trvs:
                should_remove = True

        if should_remove:
            entities_to_remove.append(switch_unique_id)

    # Remove entities from registry – only delete tracking key on success
    removed_count = 0
    for switch_unique_id in entities_to_remove:
        if switch_unique_id is None:
            continue
        entity_id = entity_registry.async_get_entity_id(
            "switch", DOMAIN, switch_unique_id
        )
        if entity_id:
            try:
                entity_registry.async_remove(entity_id)
                removed_count += 1
                tracked_switches.pop(switch_unique_id, None)
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

    # Merge new entries without wiping failed removals
    # Add PID Auto-Tune switches for current PID TRVs
    for trv_entity_id in current_pid_trvs:
        uid = f"{bt_climate.unique_id}_{trv_entity_id}_pid_auto_tune"
        tracked_switches[uid] = {"trv": trv_entity_id, "type": "pid_auto_tune"}

    # Add Child Lock switches (always present for all TRVs)
    if bt_climate.real_trvs:
        for trv_entity_id in bt_climate.real_trvs:
            uid = f"{bt_climate.unique_id}_{trv_entity_id}_child_lock"
            tracked_switches[uid] = {"trv": trv_entity_id, "type": "child_lock"}

    _ACTIVE_SWITCH_ENTITIES[entry_id] = tracked_switches

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


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_filtered_temp(bt_climate: BetterThermostat) -> float | None:
    """Return cur_temp_filtered with fallback to external_temp_ema."""
    val: float | None = getattr(bt_climate, "cur_temp_filtered", None)
    if val is None:
        val = getattr(bt_climate, "external_temp_ema", None)
    return val


# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


class _BtSensorBase(SensorEntity):
    """Base class for all Better Thermostat sensors."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False
    _unique_id_suffix: str

    def __init__(self, bt_climate: BetterThermostat) -> None:
        """Initialize the sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_{self._unique_id_suffix}"
        self._attr_device_info = bt_climate.device_info

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
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
    def _on_climate_update(self, event: Event[EventStateChangedData]) -> None:
        """Handle climate entity update."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self) -> None:
        """Update state from climate entity."""
        raise NotImplementedError


class _BtMpcSensorBase(_BtSensorBase):
    """Base class for MPC algorithm sensors."""

    _debug_key: str

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Follow HA guidelines: return False when entity should be unavailable.
        This prevents "unknown" states and properly shows "unavailable".
        """
        if not self._bt_climate._available:
            return False
        if self._bt_climate.window_open:
            return False
        if self._bt_climate.hvac_mode == "off":
            return False
        return True

    def _update_state(self) -> None:
        """Update state from calibration_balance debug data."""
        val = None
        if self._bt_climate.real_trvs:
            for trv_data in self._bt_climate.real_trvs.values():
                cal_bal = trv_data.get("calibration_balance")
                if cal_bal and "debug" in cal_bal:
                    debug = cal_bal["debug"]
                    if self._debug_key in debug:
                        val = debug[self._debug_key]
                        break

        if val is not None:
            try:
                self._attr_native_value = float(val)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class _BtSimpleAttributeSensor(_BtSensorBase):
    """Base class for sensors reading a single climate attribute."""

    _climate_attr: str
    _rounding: int | None = None

    def _update_state(self) -> None:
        """Update state from a climate entity attribute."""
        val: object = getattr(self._bt_climate, self._climate_attr, None)
        if val is not None:
            try:
                fval = float(val)  # type: ignore[arg-type]
                self._attr_native_value = (
                    round(fval, self._rounding) if self._rounding is not None else fval
                )
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


# ---------------------------------------------------------------------------
# Concrete sensor classes
# ---------------------------------------------------------------------------


class BetterThermostatExternalTempSensor(_BtSensorBase):
    """Representation of a Better Thermostat External Temperature Sensor (EMA)."""

    _attr_name = "Temperature EMA"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _unique_id_suffix = "external_temp_ema"

    def _update_state(self) -> None:
        """Update state from climate entity."""
        val = _get_filtered_temp(self._bt_climate)
        if val is not None:
            try:
                self._attr_native_value = float(val)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatExternalTemp1hEMASensor(_BtSensorBase):
    """Representation of a Better Thermostat External Temperature 1h EMA Sensor."""

    _attr_name = "Temperature EMA 1h"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 2
    _unique_id_suffix = "external_temp_ema_1h"

    def __init__(self, bt_climate: BetterThermostat) -> None:
        """Initialize the sensor."""
        super().__init__(bt_climate)
        self._ema_value: float | None = None
        self._last_update_ts: float | None = None
        self._tau_s: float = 3600.0  # 1 hour

    def _update_ema(self, new_value: float) -> None:
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

    def _update_state(self) -> None:
        """Update state from internal EMA."""
        val = _get_filtered_temp(self._bt_climate)
        if val is not None:
            try:
                self._update_ema(float(val))
                assert self._ema_value is not None  # set by _update_ema
                self._attr_native_value = round(self._ema_value, 2)
            except (ValueError, TypeError):
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatTempSlopeSensor(_BtSimpleAttributeSensor):
    """Representation of a Better Thermostat Temperature Slope Sensor."""

    _attr_name = "Temperature Slope"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _attr_icon = "mdi:chart-line"
    _climate_attr = "temp_slope"
    _rounding = 4
    _unique_id_suffix = "temp_slope"


class BetterThermostatHeatingPowerSensor(_BtSimpleAttributeSensor):
    """Representation of a Better Thermostat Heating Power Sensor."""

    _attr_name = "Heating Power"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _attr_icon = "mdi:thermometer-plus"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _climate_attr = "heating_power"
    _unique_id_suffix = "heating_power"


class BetterThermostatHeatLossSensor(_BtSimpleAttributeSensor):
    """Representation of a Better Thermostat Heat Loss Sensor."""

    _attr_name = "Heat Loss"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _attr_icon = "mdi:thermometer-minus"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _climate_attr = "heat_loss_rate"
    _unique_id_suffix = "heat_loss"


class BetterThermostatVirtualTempSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat Virtual Temperature Sensor (MPC)."""

    _attr_name = "Virtual Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermometer-auto"
    _debug_key = "mpc_virtual_temp"
    _unique_id_suffix = "virtual_temp"


class BetterThermostatMpcGainSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat MPC Gain Sensor."""

    _attr_name = "MPC Gain"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _attr_icon = "mdi:thermometer-plus"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "mpc_gain"
    _unique_id_suffix = "mpc_gain"


class BetterThermostatMpcLossSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat MPC Loss Sensor."""

    _attr_name = "MPC Loss"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _attr_icon = "mdi:thermometer-minus"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "mpc_loss"
    _unique_id_suffix = "mpc_loss"


class BetterThermostatMpcKaSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat MPC Ka (Insulation) Sensor."""

    _attr_name = "MPC Insulation (Ka)"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "1/min"
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "mpc_ka"
    _unique_id_suffix = "mpc_ka"


class BetterThermostatSolarIntensitySensor(_BtSensorBase):
    """Representation of a Better Thermostat Solar Intensity Sensor."""

    _attr_name = "Sun Intensity Heatup"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "%"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = (
        True  # Weather entity updates not strictly coupled to climate state
    )
    _attr_icon = "mdi:solar-power"
    _unique_id_suffix = "solar_intensity"

    def _update_state(self) -> None:
        """Update state using utility function."""
        try:
            val = _get_current_solar_intensity(self._bt_climate)
            if val is not None:
                # Function returns 0.0-1.0, convert to %
                self._attr_native_value = round(float(val) * 100.0, 1)
            else:
                self._attr_native_value = 0.0
        except Exception:
            self._attr_native_value = None
