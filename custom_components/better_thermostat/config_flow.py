import logging

import voluptuous as vol

from .helpers import get_device_model

from .const import CONF_CALIBRATIION_ROUND, CONF_CHILD_LOCK, CONF_HEAT_AUTO_SWAPPED, CONF_HEATER, CONF_LOCAL_CALIBRATION, CONF_MODEL, CONF_OFF_TEMPERATURE, CONF_OUTDOOR_SENSOR, CONF_SENSOR, CONF_SENSOR_WINDOW, CONF_VALVE_MAINTENANCE, CONF_WEATHER, CONF_WINDOW_TIMEOUT
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback


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

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)
        """Get the options flow for this handler."""

    async def async_step_user(self, user_input=None):

        if user_input is not None:
            if self.data is None:
                self.data = user_input
            self.heater_entity_id = self.data[CONF_HEATER]
            if self.data[CONF_WEATHER] is None and self.data[CONF_OUTDOOR_SENSOR] is None:
                return self.async_error(reason="no_outside_temp")
            device_model = await get_device_model(self)
            self.data[CONF_MODEL] = device_model or "generic"
            await self.async_set_unique_id(self.data["name"])
            return self.async_create_entry(title=self.data["name"], data=self.data)


        errors = {}
        user_input = user_input or {}
        local_calibration = {}
        local_calibration[None] = "-"
        thermostat_name = {}
        temp_sensor_name = {}
        outdoor_sensor_name = {}
        window_sensor_name = {}
        weather_name = {}

        weather_name[None] = "-"
        outdoor_sensor_name[None] = "-"
        window_sensor_name[None] = "-"

        entity_registry = self.hass.helpers.entity_registry.async_get(self.hass)
        for entity_id, entry in entity_registry.entities.items():
                    if entity_id.find("climate.") != -1:
                        thermostat_name[entity_id] = entity_id
                    if entity_id.find("number.") != -1 or entity_id.find("input_number.") != -1:
                        local_calibration[entity_id] = entity_id
                    if entity_id.find("sensor.") != -1 or entity_id.find("input_number.") != -1:
                        temp_sensor_name[entity_id] = entity_id
                    if entity_id.find("sensor.") != -1 or entity_id.find("input_number.") != -1:
                        outdoor_sensor_name[entity_id] = entity_id
                    if (entity_id.find("sensor.") != -1 or entity_id.find("group.") != -1) or entity_id.find("input_boolean.") != -1:
                        window_sensor_name[entity_id] = entity_id
                    if entity_id.find("weather.") != -1 or entity_id.find("input_number.") != -1:
                        weather_name[entity_id] = entity_id

        if not thermostat_name:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional(CONF_NAME, default=user_input.get(CONF_NAME, "")): str,
                vol.Required(CONF_HEATER): vol.In(thermostat_name),
                vol.Required(CONF_LOCAL_CALIBRATION,default=None): vol.In(local_calibration),
                vol.Required(CONF_SENSOR): vol.In(temp_sensor_name),
                vol.Optional(CONF_OUTDOOR_SENSOR, default=None): vol.In(outdoor_sensor_name),
                vol.Required(CONF_SENSOR_WINDOW,default=None): vol.In(window_sensor_name),
                vol.Optional(CONF_WEATHER, default=None): vol.In(weather_name),
                vol.Optional(CONF_WINDOW_TIMEOUT, default=user_input.get(CONF_WINDOW_TIMEOUT, 0)): int,
                vol.Optional(CONF_OFF_TEMPERATURE, default=user_input.get(CONF_OFF_TEMPERATURE, 25)): int,
                vol.Optional(CONF_CALIBRATIION_ROUND, default=user_input.get(CONF_CALIBRATIION_ROUND, True)): bool,
                vol.Optional(CONF_VALVE_MAINTENANCE, default=user_input.get(CONF_VALVE_MAINTENANCE, False)): bool,
                vol.Optional(CONF_HEAT_AUTO_SWAPPED, default=user_input.get(CONF_HEAT_AUTO_SWAPPED, False)): bool,
                vol.Optional(CONF_CHILD_LOCK, default=user_input.get(CONF_CHILD_LOCK, False)): bool,
            }),
            errors=errors,
        )

class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle a option flow for a config entry."""
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        return await self.async_step_user()


    async def async_step_user(self, user_input=None):

        if user_input is not None:
            current_config = self.config_entry.data
            self.updated_config = dict(current_config)
            self.updated_config[CONF_WINDOW_TIMEOUT] =  user_input.get(CONF_WINDOW_TIMEOUT)
            self.updated_config[CONF_OFF_TEMPERATURE] =  user_input.get(CONF_OFF_TEMPERATURE)
            self.updated_config[CONF_CALIBRATIION_ROUND] =  user_input.get(CONF_CALIBRATIION_ROUND)
            self.updated_config[CONF_VALVE_MAINTENANCE] =  user_input.get(CONF_VALVE_MAINTENANCE)
            self.updated_config[CONF_HEAT_AUTO_SWAPPED] =  user_input.get(CONF_HEAT_AUTO_SWAPPED)
            self.updated_config[CONF_CHILD_LOCK] =  user_input.get(CONF_CHILD_LOCK)
            _LOGGER.debug("OptionsFlowHandler async_step_init %s", self.updated_config)

            self.hass.config_entries.async_update_entry(
                self.config_entry, data=self.updated_config
            )
            return self.async_create_entry(title="", data=None)

        local_calibration = {}
        local_calibration[None] = "-"
        temp_sensor_name = {}
        outdoor_sensor_name = {}
        window_sensor_name = {}
        weather_name = {}

        weather_name[None] = "-"
        outdoor_sensor_name[None] = "-"
        window_sensor_name[None] = "-"

        entity_registry = self.hass.helpers.entity_registry.async_get(self.hass)
        for entity_id, entry in entity_registry.entities.items():
                    if entity_id.find("number.") != -1 or entity_id.find("input_number.") != -1:
                        local_calibration[entity_id] = entity_id
                    if entity_id.find("sensor.") != -1 or entity_id.find("input_number.") != -1:
                        temp_sensor_name[entity_id] = entity_id
                    if entity_id.find("sensor.") != -1 or entity_id.find("input_number.") != -1:
                        outdoor_sensor_name[entity_id] = entity_id
                    if (entity_id.find("sensor.") != -1 or entity_id.find("group.") != -1) or entity_id.find("input_boolean.") != -1:
                        window_sensor_name[entity_id] = entity_id
                    if entity_id.find("weather.") != -1 or entity_id.find("input_number.") != -1:
                        weather_name[entity_id] = entity_id

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LOCAL_CALIBRATION,
                        default=self.config_entry.data.get(
                            CONF_LOCAL_CALIBRATION, "-"
                        ),
                    ): vol.In(local_calibration),
                    vol.Required(
                        CONF_SENSOR,
                        default=self.config_entry.data.get(
                            CONF_SENSOR, "-"
                        ),
                    ): vol.In(temp_sensor_name),
                    vol.Optional(
                        CONF_OUTDOOR_SENSOR,
                        default=self.config_entry.data.get(
                            CONF_OUTDOOR_SENSOR, "-"
                        ),
                    ): vol.In(outdoor_sensor_name),
                    vol.Required(
                        CONF_SENSOR_WINDOW,
                        default=self.config_entry.data.get(
                            CONF_SENSOR_WINDOW, "-"
                        ),
                    ): vol.In(window_sensor_name),
                    vol.Optional(
                        CONF_WEATHER,
                        default=self.config_entry.data.get(
                            CONF_WEATHER, "-"
                        ),                    
                    ): vol.In(weather_name),
                    vol.Optional(
                        CONF_WINDOW_TIMEOUT,
                        default=self.config_entry.data.get(
                            CONF_WINDOW_TIMEOUT, 0
                        ),
                    ): int,
                    vol.Optional(
                        CONF_OFF_TEMPERATURE,
                        default=self.config_entry.data.get(
                            CONF_OFF_TEMPERATURE, 25
                        ),
                    ): int,
                    vol.Optional(
                        CONF_CALIBRATIION_ROUND,
                        default=self.config_entry.data.get(
                            CONF_CALIBRATIION_ROUND, True
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_VALVE_MAINTENANCE,
                        default=self.config_entry.data.get(
                            CONF_VALVE_MAINTENANCE, False
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_HEAT_AUTO_SWAPPED,
                        default=self.config_entry.data.get(
                            CONF_HEAT_AUTO_SWAPPED, False
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_CHILD_LOCK,
                        default=self.config_entry.data.get(
                            CONF_CHILD_LOCK, False
                        ),
                    ): bool,
                }
            ),
        )