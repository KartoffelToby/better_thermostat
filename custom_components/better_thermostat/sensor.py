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

from .calibration import _get_current_solar_intensity
from .utils.const import CONF_CALIBRATION_MODE, CalibrationMode

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
        BetterThermostatHeatingPowerSensor(bt_climate),
        BetterThermostatHeatLossSensor(bt_climate),
        BetterThermostatSolarIntensitySensor(bt_climate),
    ]

    has_mpc = False
    if hasattr(bt_climate, "all_trvs"):
        for trv in bt_climate.all_trvs:
            advanced = trv.get("advanced", {})
            if advanced.get(CONF_CALIBRATION_MODE) == CalibrationMode.MPC_CALIBRATION:
                has_mpc = True
                break

    if has_mpc:
        sensors.extend(
            [
                BetterThermostatVirtualTempSensor(bt_climate),
                BetterThermostatMpcGainSensor(bt_climate),
                BetterThermostatMpcLossSensor(bt_climate),
                BetterThermostatMpcKaSensor(bt_climate),
                BetterThermostatMpcStatusSensor(bt_climate),
            ]
        )

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
    """Representation of a Better Thermostat MPC Insulation (Ka) Sensor."""

    _attr_has_entity_name = True
    _attr_name = "MPC Insulation"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "1/min"
    _attr_should_poll = False
    _attr_icon = "mdi:home-thermometer"
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
