"""Better Thermostat Binary Sensor Platform.

The MPC v2 observer measures the heat flow the radiator does not explain: the
sun through the window, a fireplace in the next room, a draught at the door.
The sensors here turn that measurement into the two statements a user can
act on: an additional heat source is warming the room, or an unexpected heat
loss is cooling it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, override

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BetterThermostatConfigEntry
from .entity import FollowsThermostat, remove_unclaimed_registry_entries
from .utils.helpers import async_normalize_bt_entity_ids

if TYPE_CHECKING:
    from .climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

# Every entity is pushed and none polls; actions are not limited per platform.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterThermostatConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Better Thermostat binary sensors."""
    bt_climate = entry.runtime_data.climate
    if not bt_climate:
        _LOGGER.warning(
            "Better Thermostat climate entity not found for entry %s. "
            "Binary sensors will not be added.",
            entry.entry_id,
        )
        return

    sensors: list[BinarySensorEntity] = [
        BetterThermostatHeatSourceBinarySensor(bt_climate),
        BetterThermostatHeatLossBinarySensor(bt_climate),
    ]

    remove_unclaimed_registry_entries(
        er.async_get(hass),
        entry,
        Platform.BINARY_SENSOR,
        (sensor.unique_id for sensor in sensors),
        bt_climate,
    )
    async_normalize_bt_entity_ids(hass, entry, Platform.BINARY_SENSOR)
    async_add_entities(sensors, True)


class _BtSourceBinarySensor(FollowsThermostat, BinarySensorEntity):
    """Base class for the sensors that report a source of heat in the room.

    The observer's disturbance is measured against the room's own baseline,
    so what the sensor reports is the heat flow that is there now and does
    not belong to the room's usual behaviour. Without an observer reading
    the sensor is unavailable: nothing measures the room, so nothing is
    known about it.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _unique_id_suffix: str

    def __init__(self, bt_climate: BetterThermostat) -> None:
        """Initialize the binary sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_{self._unique_id_suffix}"

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_thermostat()

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    @override
    def available(self) -> bool:
        """Return whether an observer reading is there to report."""
        if not self._bt_climate._available:
            return False
        return (
            self._bt_climate.anomaly_evidence.fresh_observer_disturbance(
                self._bt_climate.clock.monotonic()
            )
            is not None
        )


class BetterThermostatHeatSourceBinarySensor(_BtSourceBinarySensor):
    """Sensor that reports a heat gain the radiator does not explain."""

    _attr_translation_key = "heat_source_detected"
    _unique_id_suffix = "heat_source_detected"

    @property
    @override
    def is_on(self) -> bool:
        """Return whether a heat source is warming the room."""
        return self._bt_climate.anomaly_evidence.heat_source_active


class BetterThermostatHeatLossBinarySensor(_BtSourceBinarySensor):
    """Sensor that reports a heat loss the radiator does not explain."""

    _attr_translation_key = "heat_loss_detected"
    _unique_id_suffix = "heat_loss_detected"

    @property
    @override
    def is_on(self) -> bool:
        """Return whether a heat loss is cooling the room."""
        return self._bt_climate.anomaly_evidence.cool_source_active
