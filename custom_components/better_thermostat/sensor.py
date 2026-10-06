"""Better Thermostat Sensor Platform."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial
import logging
import math
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import (
    EntityRegistry,
    async_get as async_get_entity_registry,
)
from homeassistant.helpers.event import async_track_state_change_event

from . import BetterThermostatConfigEntry
from .calibration import _get_current_solar_intensity
from .entity import remove_unclaimed_registry_entries
from .utils.const import CONF_CALIBRATION_MODE, DOMAIN, CalibrationMode
from .utils.helpers import async_normalize_bt_entity_ids

if TYPE_CHECKING:
    from .climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

# Global tracking variables for active algorithm-specific entities
_ACTIVE_ALGORITHM_ENTITIES: dict[str, dict[CalibrationMode, list[str]]] = {}
_ENTITY_CLEANUP_CALLBACKS: dict[str, Callable[..., None]] = {}
_DISPATCHER_UNSUBSCRIBES: dict[str, Callable[[], None]] = {}

# Global tracking variables for active preset number entities
_ACTIVE_PRESET_NUMBERS: dict[
    str, dict[str | None, dict[str, str | bool]]
] = {}  # {entry_id: {unique_id: {"preset": preset_name, "cool": True}, ...}}
_ACTIVE_PID_NUMBERS: dict[
    str, dict[str | None, dict[str, str]]
] = {}  # {entry_id: {unique_id: {"trv": trv_entity_id, "param": parameter}, ...}}
_ACTIVE_SWITCH_ENTITIES: dict[
    str, dict[str | None, dict[str, str]]
] = {}  # {entry_id: {unique_id: {"trv": trv_entity_id, "type": kind}, ...}}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterThermostatConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Better Thermostat sensors."""
    bt_climate = entry.runtime_data.climate
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

    # No algorithm sensor of this entry is live before its platform is set
    # up, so a record left by a setup that failed part way is dropped;
    # otherwise the sensors it names would never be created.
    _ACTIVE_ALGORITHM_ENTITIES.pop(entry.entry_id, None)
    algorithm_sensors = await _setup_algorithm_sensors(hass, entry, bt_climate)
    sensors.extend(algorithm_sensors)
    remove_unclaimed_registry_entries(
        async_get_entity_registry(hass),
        entry,
        Platform.SENSOR,
        (sensor.unique_id for sensor in sensors),
        bt_climate,
    )

    async_normalize_bt_entity_ids(hass, entry, Platform.SENSOR)
    async_add_entities(sensors, True)

    # Register callback for dynamic entity updates
    await _register_dynamic_entity_callback(hass, entry, bt_climate, async_add_entities)


async def _setup_algorithm_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    bt_climate: BetterThermostat,
    algorithms_to_create: set[CalibrationMode] | None = None,
) -> list[SensorEntity]:
    """Set up algorithm-specific sensors based on current configuration.

    The entities of every algorithm no TRV uses any more are removed first,
    whichever algorithms are being created. A sensor already tracked for its
    algorithm is live and is not created a second time, so an algorithm
    whose cleanup removed only some of its sensors gets exactly the missing
    ones back when it is used again.

    Parameters
    ----------
    hass : HomeAssistant
        Home Assistant instance.
    entry : ConfigEntry
        Config entry the sensors belong to.
    bt_climate : BetterThermostat
        Better Thermostat climate entity the sensors report on.
    algorithms_to_create : set | None
        When provided, only sensors for these algorithms are created.
        When ``None`` (initial setup), all active algorithms are created.

    Returns
    -------
    list[SensorEntity]
        The sensors not yet tracked, for the caller to add.
    """
    algorithm_sensors: list[SensorEntity] = []
    entry_id = entry.entry_id
    active_algorithms = _get_active_algorithms(bt_climate)

    await _cleanup_stale_algorithm_entities(
        hass, entry_id, bt_climate, active_algorithms
    )

    current_algorithms = active_algorithms
    if algorithms_to_create is not None:
        current_algorithms = current_algorithms & algorithms_to_create

    # Setup MPC sensors
    if CalibrationMode.MPC_CALIBRATION in current_algorithms:
        mpc_sensors = _track_algorithm_sensors(
            entry_id,
            CalibrationMode.MPC_CALIBRATION,
            [
                BetterThermostatVirtualTempSensor(bt_climate),
                BetterThermostatMpcGainSensor(bt_climate),
                BetterThermostatMpcLossSensor(bt_climate),
                BetterThermostatMpcKaSensor(bt_climate),
            ],
        )
        algorithm_sensors.extend(mpc_sensors)

        _LOGGER.debug(
            "Better Thermostat %s: Created MPC sensors for entry %s",
            bt_climate.device_name,
            entry_id,
        )

    # Keep the MPC v1 diagnostic surface available for the dashboard, but map
    # it to the corresponding MPC v2 controller diagnostics.  These are only
    # observations; they never feed back into the controller.
    if CalibrationMode.MPC_V2_CALIBRATION in current_algorithms:
        mpc_v2_sensors = _track_algorithm_sensors(
            entry_id,
            CalibrationMode.MPC_V2_CALIBRATION,
            [
                BetterThermostatMpcV2VirtualTempSensor(bt_climate),
                BetterThermostatMpcV2CouplingSensor(bt_climate),
                BetterThermostatMpcV2DisturbanceSensor(bt_climate),
                BetterThermostatMpcV2RoomTimeConstantSensor(bt_climate),
            ],
        )
        algorithm_sensors.extend(mpc_v2_sensors)
        _adopt_shared_mpc_v2_registry_entries(hass, mpc_v2_sensors)

        _LOGGER.debug(
            "Better Thermostat %s: Created MPC v2 diagnostic sensors for entry %s",
            bt_climate.device_name,
            entry_id,
        )

    # Setup PID sensors
    if CalibrationMode.PID_CALIBRATION in current_algorithms:
        pid_sensors = _track_algorithm_sensors(
            entry_id,
            CalibrationMode.PID_CALIBRATION,
            [
                BetterThermostatPidKpSensor(bt_climate),
                BetterThermostatPidKiSensor(bt_climate),
                BetterThermostatPidKdSensor(bt_climate),
                BetterThermostatPidOutputSensor(bt_climate),
                BetterThermostatPidErrorSensor(bt_climate),
            ],
        )
        algorithm_sensors.extend(pid_sensors)

        _LOGGER.debug(
            "Better Thermostat %s: Created PID sensors for entry %s",
            bt_climate.device_name,
            entry_id,
        )

    return algorithm_sensors


def _track_algorithm_sensors(
    entry_id: str, algorithm: CalibrationMode, sensors: Sequence[SensorEntity]
) -> list[SensorEntity]:
    """Record the unique_ids of one algorithm's sensors, return the untracked ones.

    The stale-entity cleanup removes exactly what is recorded here, so the
    record is taken from the sensors themselves. A sensor whose unique_id is
    already recorded is live, and adding it again would register a second
    entity under the same unique_id; only the others come back.
    """
    tracked = _ACTIVE_ALGORITHM_ENTITIES.setdefault(entry_id, {}).setdefault(
        algorithm, []
    )
    untracked: list[SensorEntity] = []
    for sensor in sensors:
        unique_id = sensor.unique_id
        if unique_id is not None and unique_id not in tracked:
            tracked.append(unique_id)
            untracked.append(sensor)
    return untracked


def _adopt_shared_mpc_v2_registry_entries(
    hass: HomeAssistant, sensors: Sequence[SensorEntity]
) -> None:
    """Move MPC v2 registry entries off the unique_ids MPC v1 sensors carry.

    A registry entry under an MPC v1 unique_id that carries an MPC v2
    translation key belongs to the MPC v2 sensor and moves to that sensor's
    unique_id, keeping its entity_id and history. An entry under an MPC v1
    translation key stays where it is.
    """
    registry = async_get_entity_registry(hass)
    for sensor in sensors:
        if not isinstance(sensor, _BtMpcV2SensorBase) or sensor.unique_id is None:
            continue
        shared_unique_id = (
            f"{sensor._bt_climate.unique_id}_{sensor._shared_unique_id_suffix}"
        )
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, shared_unique_id)
        if entity_id is None:
            continue
        if registry.async_get_entity_id("sensor", DOMAIN, sensor.unique_id):
            continue
        reg_entry = registry.async_get(entity_id)
        if reg_entry is None or reg_entry.translation_key != sensor.translation_key:
            continue
        registry.async_update_entity(entity_id, new_unique_id=sensor.unique_id)


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
        # Scoped to the entry, so an unload cancels an update still pending
        # instead of letting it add entities to an entry that is gone.
        entry.async_create_background_task(
            hass,
            _handle_dynamic_entity_update(hass, entry, bt_climate, async_add_entities),
            name=f"bt_dynamic_entity_update_{entry.entry_id}",
        )

    # Store callback for later cleanup
    _ENTITY_CLEANUP_CALLBACKS[entry.entry_id] = _on_config_change

    # Listen to configuration change signals
    signal_key = f"bt_config_changed_{entry.entry_id}"
    unsubscribe = async_dispatcher_connect(hass, signal_key, _on_config_change)

    # Store unsubscribe function for cleanup
    _DISPATCHER_UNSUBSCRIBES[entry.entry_id] = unsubscribe
    entry.async_on_unload(partial(_release_entry, entry.entry_id))


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

    # Check for changes in the algorithms
    algorithms_added = current_algorithms - previous_algorithms
    algorithms_removed = previous_algorithms - current_algorithms

    if algorithms_added or algorithms_removed:
        _LOGGER.info(
            "Better Thermostat %s: Algorithm configuration changed. Added: %s, Removed: %s",
            bt_climate.device_name,
            [alg.value for alg in algorithms_added],
            [alg.value for alg in algorithms_removed],
        )

    # Set up for every active algorithm, not only the added ones: one whose
    # earlier cleanup removed some of its sensors is still tracked, so it is
    # not "added" when a TRV uses it again, and only its missing sensors are
    # created.
    new_sensors = await _setup_algorithm_sensors(hass, entry, bt_climate)
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
            # This algorithm is no longer active - remove its entities. An id
            # the registry no longer holds is already gone; only the ones whose
            # removal fails stay tracked for the next cleanup.
            removed_count = 0
            remaining_unique_ids = []
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
                        remaining_unique_ids.append(entity_unique_id)
                        _LOGGER.warning(
                            "Better Thermostat %s: Failed to remove %s entity %s: %s",
                            bt_climate.device_name,
                            algorithm.value,
                            entity_id,
                            e,
                            exc_info=True,
                        )

            if removed_count > 0:
                _LOGGER.info(
                    "Better Thermostat %s: Removed %d %s entities",
                    bt_climate.device_name,
                    removed_count,
                    algorithm.value,
                )

            if remaining_unique_ids:
                tracked_algorithms[algorithm] = remaining_unique_ids
            else:
                algorithms_to_remove.append(algorithm)

    # Clean up tracking for removed algorithms
    for algorithm in algorithms_to_remove:
        del _ACTIVE_ALGORITHM_ENTITIES[entry_id][algorithm]

    # Remove the entry_id entirely once no algorithms are tracked anymore
    if not _ACTIVE_ALGORITHM_ENTITIES[entry_id]:
        del _ACTIVE_ALGORITHM_ENTITIES[entry_id]


def _get_active_algorithms(bt_climate: BetterThermostat) -> set[CalibrationMode]:
    """Get set of calibration algorithms currently in use by any TRV."""
    if not bt_climate.real_trvs:
        return set()

    active_algorithms: set[CalibrationMode] = set()
    for trv_entity_id, trv in bt_climate.real_trvs.items():
        advanced = trv.advanced or {}
        calibration_mode = advanced.get(CONF_CALIBRATION_MODE)
        if calibration_mode:
            # Convert string to enum if needed
            if isinstance(calibration_mode, str):
                try:
                    calibration_mode = CalibrationMode(calibration_mode)
                except ValueError:
                    _LOGGER.warning(
                        "Better Thermostat %s: Invalid calibration mode '%s' for TRV %s",
                        bt_climate.device_name,
                        calibration_mode,
                        trv_entity_id,
                    )
                    continue
            active_algorithms.add(calibration_mode)

    return active_algorithms


def _get_pid_trvs(bt_climate: BetterThermostat) -> set[str]:
    """Return entity IDs of TRVs currently using PID calibration."""
    pid_trvs: set[str] = set()
    if not bt_climate.real_trvs:
        return pid_trvs
    for trv_entity_id, trv in bt_climate.real_trvs.items():
        advanced = trv.advanced or {}
        calibration_mode = advanced.get(CONF_CALIBRATION_MODE)
        # Normalize string values to CalibrationMode enum
        if isinstance(calibration_mode, str):
            try:
                calibration_mode = CalibrationMode(calibration_mode)
            except ValueError, TypeError:
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
                    exc_info=True,
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
        trv_entity_id = meta.get("trv")
        if trv_entity_id and trv_entity_id not in current_pid_trvs:
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
                    exc_info=True,
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
        trv_entity_id = meta.get("trv")
        kind = meta.get("type")
        should_remove = False

        if kind == "pid_auto_tune":
            if trv_entity_id not in current_pid_trvs:
                should_remove = True
        elif kind == "child_lock":
            # Remove child lock switches for TRVs that no longer exist
            if not bt_climate.real_trvs or trv_entity_id not in bt_climate.real_trvs:
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
                    exc_info=True,
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


def _release_entry(entry_id: str) -> None:
    """Drop the dispatcher subscription and the entity tracking of one entry.

    Home Assistant does not call a platform module's unload hook, so the
    platform setup registers this with the config entry's own unload.
    """
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


# Helper


def _get_filtered_temp(bt_climate: BetterThermostat) -> float | None:
    """Return room_temperature_filtered with fallback to external_temp_ema."""
    value = bt_climate.room_temperature_filtered
    if value is None:
        value = bt_climate.external_temp_ema
    return value


# Base classes


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
    """Base class for calibration debug sensors (MPC and PID).

    Reads a single key from the ``calibration_balance['debug']`` payload,
    iterating all TRVs of the climate entity; the first TRV whose payload
    contains the key wins.
    """

    _debug_key: str

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Follow HA guidelines: return False when entity should be unavailable.
        This prevents "unknown" states and properly shows "unavailable".
        """
        if not self._bt_climate._available:
            return False
        if self._bt_climate.contact_open:
            return False
        if self._bt_climate.hvac_mode == "off":
            return False
        return True

    def _update_state(self) -> None:
        """Update state from calibration_balance debug data."""
        value = None
        if self._bt_climate.real_trvs:
            for trv in self._bt_climate.real_trvs.values():
                cal_bal = trv.calibration_balance
                if cal_bal and "debug" in cal_bal:
                    debug = cal_bal["debug"]
                    if self._debug_key in debug:
                        value = debug[self._debug_key]
                        break

        if value is not None:
            try:
                self._attr_native_value = float(value)
            except ValueError, TypeError:
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class _BtSimpleAttributeSensor(_BtSensorBase):
    """Base class for sensors reading a single climate attribute."""

    _climate_attr: str
    _rounding: int | None = None

    def _update_state(self) -> None:
        """Update state from a climate entity attribute."""
        value: object = getattr(self._bt_climate, self._climate_attr, None)
        if value is not None:
            try:
                fval = float(value)  # type: ignore[arg-type]
                self._attr_native_value = (
                    round(fval, self._rounding) if self._rounding is not None else fval
                )
            except ValueError, TypeError:
                self._attr_native_value = None
        else:
            self._attr_native_value = None


# Concrete sensor classes


class BetterThermostatExternalTempSensor(_BtSensorBase):
    """Representation of a Better Thermostat External Temperature Sensor (EMA)."""

    _attr_translation_key = "external_temp_ema"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _unique_id_suffix = "external_temp_ema"

    def _update_state(self) -> None:
        """Update state from climate entity."""
        value = _get_filtered_temp(self._bt_climate)
        if value is not None:
            try:
                self._attr_native_value = float(value)
            except ValueError, TypeError:
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatExternalTemp1hEMASensor(_BtSensorBase):
    """Representation of a Better Thermostat External Temperature 1h EMA Sensor."""

    _attr_translation_key = "external_temp_ema_1h"
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
        value = _get_filtered_temp(self._bt_climate)
        if value is not None:
            try:
                self._update_ema(float(value))
                assert self._ema_value is not None  # set by _update_ema
                self._attr_native_value = round(self._ema_value, 2)
            except ValueError, TypeError:
                self._attr_native_value = None
        else:
            self._attr_native_value = None


class BetterThermostatTempSlopeSensor(_BtSimpleAttributeSensor):
    """Representation of a Better Thermostat Temperature Slope Sensor."""

    _attr_translation_key = "temp_slope"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _climate_attr = "temp_slope"
    _rounding = 4
    _unique_id_suffix = "temp_slope"


class BetterThermostatHeatingPowerSensor(_BtSimpleAttributeSensor):
    """Representation of a Better Thermostat Heating Power Sensor."""

    _attr_translation_key = "heating_power"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _climate_attr = "heating_power"
    _rounding = 4
    _unique_id_suffix = "heating_power"


class BetterThermostatHeatLossSensor(_BtSimpleAttributeSensor):
    """Representation of a Better Thermostat Heat Loss Sensor."""

    _attr_translation_key = "heat_loss"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _climate_attr = "heat_loss_rate"
    _rounding = 5
    _unique_id_suffix = "heat_loss"


class BetterThermostatVirtualTempSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat Virtual Temperature Sensor (MPC)."""

    _attr_translation_key = "virtual_temp"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _debug_key = "mpc_virtual_temp"
    _unique_id_suffix = "virtual_temp"


class BetterThermostatMpcGainSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat MPC Gain Sensor."""

    _attr_translation_key = "mpc_gain"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "mpc_gain"
    _unique_id_suffix = "mpc_gain"


class BetterThermostatMpcLossSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat MPC Loss Sensor."""

    _attr_translation_key = "mpc_loss"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K/min"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "mpc_loss"
    _unique_id_suffix = "mpc_loss"


class BetterThermostatMpcKaSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat MPC Ka (Insulation) Sensor."""

    _attr_translation_key = "mpc_ka"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "1/min"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "mpc_ka"
    _unique_id_suffix = "mpc_ka"


class _BtMpcV2SensorBase(_BtMpcSensorBase):
    """Base class for MPC v2 diagnostic sensors.

    MPC v2 publishes a typed diagnostic payload instead of the MPC v1 keys.
    Its sensors carry unique_ids of their own, so heads calibrated by MPC v1
    and MPC v2 in one room each get their sensors. ``_shared_unique_id_suffix``
    names the MPC v1 sensor whose unique_id an MPC v2 registry entry may still
    hold.
    """

    _v2_debug_key: str
    _shared_unique_id_suffix: str

    def _update_state(self) -> None:
        """Update state from the MPC v2 debug payload."""
        value = None
        if self._bt_climate.real_trvs:
            for trv in self._bt_climate.real_trvs.values():
                cal_bal = trv.calibration_balance
                debug = cal_bal.get("debug") if cal_bal else None
                if (
                    isinstance(debug, dict)
                    and str(debug.get("controller_version")).lower() == "v2"
                    and self._v2_debug_key in debug
                ):
                    value = debug[self._v2_debug_key]
                    break

        try:
            self._attr_native_value = float(value) if value is not None else None
        except ValueError, TypeError:
            self._attr_native_value = None


class BetterThermostatMpcV2VirtualTempSensor(_BtMpcV2SensorBase):
    """Representation of the MPC v2 estimated room temperature."""

    _attr_translation_key = "mpc_v2_virtual_temp"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _v2_debug_key = "T_room_hat"
    _unique_id_suffix = "mpc_v2_virtual_temp"
    _shared_unique_id_suffix = "virtual_temp"


class BetterThermostatMpcV2CouplingSensor(_BtMpcV2SensorBase):
    """Representation of the MPC v2 radiator-to-room coupling."""

    _attr_translation_key = "mpc_v2_coupling"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _v2_debug_key = "coupling_rad_room"
    _unique_id_suffix = "mpc_v2_coupling"
    _shared_unique_id_suffix = "mpc_gain"


class BetterThermostatMpcV2DisturbanceSensor(_BtMpcV2SensorBase):
    """Representation of the MPC v2 estimated unmodelled heat disturbance."""

    _attr_translation_key = "mpc_v2_disturbance"
    _attr_native_unit_of_measurement = "K/min"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _v2_debug_key = "D_hat_K_per_min"
    _unique_id_suffix = "mpc_v2_disturbance"
    _shared_unique_id_suffix = "mpc_loss"


class BetterThermostatMpcV2RoomTimeConstantSensor(_BtMpcV2SensorBase):
    """Representation of the MPC v2 room time constant."""

    _attr_translation_key = "mpc_v2_room_time_constant"
    _attr_native_unit_of_measurement = "min"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _v2_debug_key = "tau_room_min"
    _unique_id_suffix = "mpc_v2_room_time_constant"
    _shared_unique_id_suffix = "mpc_ka"


class BetterThermostatPidKpSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat PID Kp (proportional gain) Sensor."""

    _attr_translation_key = "pid_kp"
    _attr_device_class = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "kp"
    _unique_id_suffix = "pid_kp"


class BetterThermostatPidKiSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat PID Ki (integral gain) Sensor."""

    _attr_translation_key = "pid_ki"
    _attr_device_class = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "ki"
    _unique_id_suffix = "pid_ki"


class BetterThermostatPidKdSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat PID Kd (derivative gain) Sensor."""

    _attr_translation_key = "pid_kd"
    _attr_device_class = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "kd"
    _unique_id_suffix = "pid_kd"


class BetterThermostatPidOutputSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat PID Output (valve command) Sensor."""

    _attr_translation_key = "pid_output"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "%"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "u"
    _unique_id_suffix = "pid_output"


class BetterThermostatPidErrorSensor(_BtMpcSensorBase):
    """Representation of a Better Thermostat PID Error (setpoint deviation) Sensor."""

    _attr_translation_key = "pid_error"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "K"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _debug_key = "e_K"
    _unique_id_suffix = "pid_error"


class BetterThermostatSolarIntensitySensor(_BtSensorBase):
    """Representation of a Better Thermostat Solar Intensity Sensor."""

    _attr_translation_key = "solar_intensity"
    _attr_device_class = None
    _attr_native_unit_of_measurement = "%"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unique_id_suffix = "solar_intensity"

    async def async_added_to_hass(self) -> None:
        """Follow the weather entity as well as the thermostat.

        The weather changes on its own schedule, not with the thermostat's
        state.
        """
        await super().async_added_to_hass()
        weather_entity_id = self._bt_climate.weather_entity_id
        if weather_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [weather_entity_id], self._on_climate_update
                )
            )

    def _update_state(self) -> None:
        """Update state using utility function."""
        value = _get_current_solar_intensity(self._bt_climate)
        if value is not None:
            # Function returns 0.0-1.0, convert to %
            self._attr_native_value = round(float(value) * 100.0, 1)
        else:
            self._attr_native_value = 0.0
