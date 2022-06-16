import logging

import voluptuous as vol

from custom_components.better_thermostat.models.models import get_device_model, load_device_config
from .const import CONF_HEATER, CONF_LOCAL_CALIBRATION, CONF_OFF_TEMPERATURE, CONF_OUTDOOR_SENSOR, CONF_SENSOR, CONF_SENSOR_WINDOW, CONF_VALVE_MAINTENANCE, CONF_WEATHER, CONF_WINDOW_TIMEOUT

from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_NAME
import homeassistant.helpers.config_validation as cv
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN


from . import DOMAIN  # pylint:disable=unused-import

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        """Initialize the config flow."""
        self.name = ""
        self.data = None
        self.model = None
        self.heater_entity_id = None
        self._config = None

    async def async_step_selectdevice(self, user_input=None):
        errors = {}
        user_input = user_input or {}

        local_calibration = {}
        local_calibration[None] = "-"
        entity_registry = self.hass.helpers.entity_registry.async_get(self.hass)
        for entity_id, entry in entity_registry.entities.items():
            if self.hass.states.get(entity_id) is not None:
                if self.hass.states.get(entity_id).state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                    if entity_id.find("number.") != -1:
                        local_calibration[entity_id] = entity_id

        if not local_calibration:
            return self.async_error(reason="no_calibration_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_LOCAL_CALIBRATION,default=None): vol.In(local_calibration)
            }),
            errors=errors,
        )

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""

        if user_input is not None:
            if self.data is None:
                self.data = user_input
            self.heater_entity_id = self.data[CONF_HEATER]
            self.model = await get_device_model(self)
            load_device_config(self)
            if self._config is not None:
                if self._config.get("calibration_type") == 0:
                    if CONF_LOCAL_CALIBRATION not in user_input:
                        return await self.async_step_selectdevice(user_input)
                    self.data[CONF_LOCAL_CALIBRATION] = user_input[CONF_LOCAL_CALIBRATION] or None
                else:
                    self.data[CONF_LOCAL_CALIBRATION] = None
            else:
                self.data[CONF_LOCAL_CALIBRATION] = None
            if self.data[CONF_WEATHER] is None and self.data[CONF_OUTDOOR_SENSOR] is not None:
                return self.async_error(reason="no_outside_temp")
            self.data["MODEL"] = self.model
            await self.async_set_unique_id(self.data["name"])
            return self.async_create_entry(title=self.data["name"], data=self.data)


        errors = {}
        user_input = user_input or {}

        thermostat_name = {}
        temp_sensor_name = {}
        outdoor_sensor_name = {}
        window_sensor_name = {}
        weather_name = {}

        weather_name[None] = "-"
        outdoor_sensor_name[None] = "-"

        entity_registry = self.hass.helpers.entity_registry.async_get(self.hass)
        for entity_id, entry in entity_registry.entities.items():
            if self.hass.states.get(entity_id) is not None:
                if self.hass.states.get(entity_id).state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                    if entity_id.find("climate.") != -1:
                        thermostat_name[entity_id] = entity_id
                    if entity_id.find("sensor.") != -1:
                        temp_sensor_name[entity_id] = entity_id
                    if entity_id.find("sensor.") != -1:
                        outdoor_sensor_name[entity_id] = entity_id
                    if (entity_id.find("sensor.") != -1 or entity_id.find("group.") != -1):
                        window_sensor_name[entity_id] = entity_id
                    if entity_id.find("weather.") != -1:
                        weather_name[entity_id] = entity_id

        if not thermostat_name:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional(CONF_NAME, default=user_input.get(CONF_NAME, "")): str,
                vol.Required(CONF_HEATER): vol.In(thermostat_name),
                vol.Required(CONF_SENSOR): vol.In(temp_sensor_name),
                vol.Optional(CONF_OUTDOOR_SENSOR, default=None): vol.In(outdoor_sensor_name),
                vol.Required(CONF_SENSOR_WINDOW): vol.In(window_sensor_name),
                vol.Optional(CONF_WEATHER, default=None): vol.In(weather_name),
                vol.Optional(CONF_WINDOW_TIMEOUT, default=user_input.get(CONF_WINDOW_TIMEOUT, 0)): int,
                vol.Optional(CONF_OFF_TEMPERATURE, default=user_input.get(CONF_OFF_TEMPERATURE, 20)): int,
                vol.Optional(CONF_VALVE_MAINTENANCE, default=user_input.get(CONF_VALVE_MAINTENANCE, False)): bool,
            }),
            errors=errors,
        )