import logging
from platform import platform
from typing import Any, cast

import voluptuous as vol

from .helpers import get_device_model

from .const import CONF_CALIBRATIION_ROUND, CONF_CHILD_LOCK, CONF_HEAT_AUTO_SWAPPED, CONF_HEATER, CONF_LOCAL_CALIBRATION, CONF_MODEL, CONF_OFF_TEMPERATURE, CONF_OUTDOOR_SENSOR, CONF_SENSOR, CONF_SENSOR_WINDOW, CONF_VALVE_MAINTENANCE, CONF_WEATHER, CONF_WINDOW_TIMEOUT
from homeassistant import config_entries
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, selector
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaConfigFlowHandler,
    SchemaFlowFormStep,
    SchemaFlowMenuStep,
    SchemaOptionsFlowHandler,
    entity_selector_without_own_entities,
)

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
            if self.data[CONF_NAME] is "":
                return self.async_error(reason="no_name")

            if CONF_SENSOR_WINDOW not in self.data:
                self.data[CONF_SENSOR_WINDOW] = None
            if CONF_LOCAL_CALIBRATION not in self.data:
                self.data[CONF_LOCAL_CALIBRATION] = None
            if CONF_OUTDOOR_SENSOR not in self.data:
                self.data[CONF_OUTDOOR_SENSOR] = None
            if CONF_WEATHER not in self.data:
                self.data[CONF_WEATHER] = None
            

            device_model = await get_device_model(self)
            self.data[CONF_MODEL] = device_model or "generic"
            await self.async_set_unique_id(self.data["name"])
            return self.async_create_entry(title=self.data["name"], data=self.data)


        errors = {}
        user_input = user_input or {}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional(CONF_NAME, default=user_input.get(CONF_NAME, "")): str,
                vol.Required(CONF_HEATER): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="climate", multiple=False),
                ),
                vol.Optional(CONF_LOCAL_CALIBRATION):  selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["number","input_number"], multiple=False),
                ),
                vol.Required(CONF_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["sensor","number","input_number"], multiple=False),
                ),
                vol.Optional(CONF_OUTDOOR_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["sensor","input_number","number"], multiple=False),
                ),
                vol.Optional(CONF_SENSOR_WINDOW): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["group","sensor","input_binary","binary_sensor"], multiple=False),
                ),
                vol.Optional(CONF_WEATHER): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="weather", multiple=False),
                ),
                vol.Optional(CONF_WINDOW_TIMEOUT, default=user_input.get(CONF_WINDOW_TIMEOUT, 0)): int,
                vol.Optional(CONF_OFF_TEMPERATURE, default=user_input.get(CONF_OFF_TEMPERATURE, 20)): int,
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
            if CONF_SENSOR_WINDOW not in user_input:
                user_input[CONF_SENSOR_WINDOW] = None
            if CONF_LOCAL_CALIBRATION not in user_input:
                user_input[CONF_LOCAL_CALIBRATION] = None
            if CONF_OUTDOOR_SENSOR not in user_input:
                user_input[CONF_OUTDOOR_SENSOR] = None
            if CONF_WEATHER not in user_input:
                self.data[CONF_WEATHER] = None
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

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LOCAL_CALIBRATION,
                        default=self.config_entry.data.get(
                            CONF_LOCAL_CALIBRATION, ""
                        ),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["number","input_number"], multiple=False),
                    ),
                    vol.Required(
                        CONF_SENSOR,
                        default=self.config_entry.data.get(
                            CONF_SENSOR, ""
                        ),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["sensor","number","input_number"], multiple=False),
                    ),
                    vol.Optional(
                        CONF_OUTDOOR_SENSOR,
                        default=self.config_entry.data.get(
                            CONF_OUTDOOR_SENSOR, ""
                        ),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["sensor","input_number","number"], multiple=False),
                    ),
                    vol.Optional(
                        CONF_SENSOR_WINDOW,
                        default=self.config_entry.data.get(
                            CONF_SENSOR_WINDOW, ""
                        ),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["group","sensor","input_binary","binary_sensor"], multiple=False),
                    ),
                    vol.Optional(
                        CONF_WEATHER,
                        default=self.config_entry.data.get(
                            CONF_WEATHER, ""
                        ),                    
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="weather", multiple=False),
                    ),
                    vol.Optional(
                        CONF_WINDOW_TIMEOUT,
                        default=self.config_entry.data.get(
                            CONF_WINDOW_TIMEOUT, 0
                        ),
                    ): int,
                    vol.Optional(
                        CONF_OFF_TEMPERATURE,
                        default=self.config_entry.data.get(
                            CONF_OFF_TEMPERATURE, 20
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