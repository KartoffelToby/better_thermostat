"""Better Thermostat Sensor Platform."""

import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback, HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"


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
    ]
    async_add_entities(sensors)


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
