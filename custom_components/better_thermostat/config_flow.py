import logging


import voluptuous as vol
from collections import OrderedDict

from .helpers import (
    find_local_calibration_entity,
    get_device_model,
    get_trv_intigration,
)

from .const import (
    CONF_CALIBRATIION_ROUND,
    CONF_CALIBRATION,
    CONF_CHILD_LOCK,
    CONF_HEAT_AUTO_SWAPPED,
    CONF_HEATER,
    CONF_HOMATICIP,
    CONF_MODEL,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
)
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.components.climate.const import HVAC_MODE_OFF


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
        self._intigration = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)
        """Get the options flow for this handler."""

    async def async_step_confirm(self, user_input=None, confirm_type=None):
        """Handle user-confirmation of discovered node."""
        errors = {}
        _LOGGER.debug(user_input)
        if user_input is not None:
            _LOGGER.debug(self.data)
            if self.data is not None:
                await self.async_set_unique_id(self.data["name"])
                return self.async_create_entry(title=self.data["name"], data=self.data)
        if confirm_type is not None:
            errors["base"] = confirm_type

        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            description_placeholders={
                "name": self.data[CONF_NAME],
                "trv": self.data[CONF_HEATER],
                "calibration_mode": self.data[CONF_CALIBRATION],
            },
        )

    async def async_step_advanced(self, user_input=None):
        """Handle options flow."""
        if user_input is not None:
            self.data = self.data | user_input
            trv = self.hass.states.get(self.heater_entity_id)
            if HVAC_MODE_OFF not in trv.attributes.get("hvac_modes"):
                return await self.async_step_confirm(None, "no_off_mode")
            return await self.async_step_confirm()

        user_input = user_input or {}
        homematic = False
        calibration = {"target_temp_based": "Target Temperature"}
        default_calibration = "target_temp_based"
        if self._intigration.find("homematic") != -1:
            homematic = True

        if self._intigration.find("mqtt") != -1:
            if (await find_local_calibration_entity(self)) is not None:
                calibration["local_calibration_based"] = "Local Calibration"
                default_calibration = "local_calibration_based"

        fields = OrderedDict()

        fields[
            vol.Required(
                CONF_CALIBRATION,
                default=user_input.get(CONF_CALIBRATION, default_calibration),
            )
        ] = vol.In(calibration)

        fields[
            vol.Optional(
                CONF_CALIBRATIION_ROUND,
                default=user_input.get(CONF_CALIBRATIION_ROUND, True),
            )
        ] = bool
        fields[
            vol.Optional(
                CONF_VALVE_MAINTENANCE,
                default=user_input.get(CONF_VALVE_MAINTENANCE, False),
            )
        ] = bool
        fields[
            vol.Optional(
                CONF_HEAT_AUTO_SWAPPED,
                default=user_input.get(CONF_HEAT_AUTO_SWAPPED, False),
            )
        ] = bool
        fields[
            vol.Optional(
                CONF_CHILD_LOCK, default=user_input.get(CONF_CHILD_LOCK, False)
            )
        ] = bool
        fields[
            vol.Optional(
                CONF_HOMATICIP, default=user_input.get(CONF_HOMATICIP, homematic)
            )
        ] = bool

        return self.async_show_form(
            step_id="advanced", data_schema=vol.Schema(fields), last_step=False
        )

    async def async_step_user(self, user_input=None):

        if user_input is not None:
            if self.data is None:
                self.data = user_input
            self.heater_entity_id = self.data[CONF_HEATER]
            if self.data[CONF_NAME] == "":
                return self.async_error(reason="no_name")
            if CONF_SENSOR_WINDOW not in self.data:
                self.data[CONF_SENSOR_WINDOW] = None
            if CONF_OUTDOOR_SENSOR not in self.data:
                self.data[CONF_OUTDOOR_SENSOR] = None
            if CONF_WEATHER not in self.data:
                self.data[CONF_WEATHER] = None
            self._intigration = await get_trv_intigration(self)
            device_model = await get_device_model(self)
            self.data[CONF_MODEL] = device_model or "generic"
            return await self.async_step_advanced()

        errors = {}
        user_input = user_input or {}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default=user_input.get(CONF_NAME, "")): str,
                    vol.Required(CONF_HEATER): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="climate", multiple=False)
                    ),
                    vol.Required(CONF_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "number", "input_number"],
                            device_class="temperature",
                            multiple=False,
                        )
                    ),
                    vol.Optional(CONF_OUTDOOR_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "input_number", "number"],
                            device_class="temperature",
                            multiple=False,
                        )
                    ),
                    vol.Optional(CONF_SENSOR_WINDOW): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=[
                                "group",
                                "sensor",
                                "input_boolean",
                                "binary_sensor",
                            ],
                            multiple=False,
                        )
                    ),
                    vol.Optional(CONF_WEATHER): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="weather", multiple=False)
                    ),
                    vol.Optional(
                        CONF_WINDOW_TIMEOUT,
                        default=user_input.get(CONF_WINDOW_TIMEOUT, 0),
                    ): int,
                    vol.Optional(
                        CONF_OFF_TEMPERATURE,
                        default=user_input.get(CONF_OFF_TEMPERATURE, 20),
                    ): int,
                }
            ),
            errors=errors,
            last_step=False,
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
            self.updated_config[CONF_SENSOR] = user_input.get(CONF_SENSOR, None)
            self.updated_config[CONF_SENSOR_WINDOW] = user_input.get(
                CONF_SENSOR_WINDOW, None
            )
            self.updated_config[CONF_OUTDOOR_SENSOR] = user_input.get(
                CONF_OUTDOOR_SENSOR, None
            )
            self.updated_config[CONF_WEATHER] = user_input.get(CONF_WEATHER, None)
            self.updated_config[CONF_WINDOW_TIMEOUT] = user_input.get(
                CONF_WINDOW_TIMEOUT
            )
            self.updated_config[CONF_OFF_TEMPERATURE] = user_input.get(
                CONF_OFF_TEMPERATURE
            )
            self.updated_config[CONF_CALIBRATIION_ROUND] = user_input.get(
                CONF_CALIBRATIION_ROUND
            )
            self.updated_config[CONF_VALVE_MAINTENANCE] = user_input.get(
                CONF_VALVE_MAINTENANCE
            )
            self.updated_config[CONF_HEAT_AUTO_SWAPPED] = user_input.get(
                CONF_HEAT_AUTO_SWAPPED
            )
            self.updated_config[CONF_CALIBRATION] = user_input.get(CONF_CALIBRATION)
            self.updated_config[CONF_CHILD_LOCK] = user_input.get(CONF_CHILD_LOCK)
            self.updated_config[CONF_HOMATICIP] = user_input.get(CONF_HOMATICIP)

            _LOGGER.debug("Updated config: %s", self.updated_config)

            self.hass.config_entries.async_update_entry(
                self.config_entry, data=self.updated_config
            )
            return self.async_create_entry(title="", data=None)

        calibration = {"target_temp_based": "Target Temperature"}
        default_calibration = "target_temp_based"
        self.heater_entity_id = self.config_entry.data.get(CONF_HEATER, "")
        self._intigration = await get_trv_intigration(self)
        if self._intigration.find("mqtt") != -1:
            if (await find_local_calibration_entity(self)) is not None:
                calibration["local_calibration_based"] = "Local Calibration"
                default_calibration = "local_calibration_based"

        fields = OrderedDict()

        fields[
            vol.Required(
                CONF_CALIBRATION,
                default=self.config_entry.data.get(
                    CONF_CALIBRATION, default_calibration
                ),
            )
        ] = vol.In(calibration)

        fields[
            vol.Optional(
                CONF_SENSOR,
                description={
                    "suggested_value": self.config_entry.data.get(CONF_SENSOR, "")
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "number", "input_number"],
                device_class="temperature",
                multiple=False,
            )
        )

        fields[
            vol.Optional(
                CONF_SENSOR_WINDOW,
                description={
                    "suggested_value": self.config_entry.data.get(
                        CONF_SENSOR_WINDOW, ""
                    )
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["group", "sensor", "input_boolean", "binary_sensor"],
                multiple=False,
            )
        )

        fields[
            vol.Optional(
                CONF_OUTDOOR_SENSOR,
                description={
                    "suggested_value": self.config_entry.data.get(
                        CONF_OUTDOOR_SENSOR, ""
                    )
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "input_number", "number"],
                device_class="temperature",
                multiple=False,
            )
        )

        fields[
            vol.Optional(
                CONF_WEATHER,
                description={
                    "suggested_value": self.config_entry.data.get(CONF_WEATHER, "")
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="weather", multiple=False)
        )

        fields[
            vol.Optional(
                CONF_WINDOW_TIMEOUT,
                default=self.config_entry.data.get(CONF_WINDOW_TIMEOUT, 30),
            )
        ] = int

        fields[
            vol.Optional(
                CONF_OFF_TEMPERATURE,
                default=self.config_entry.data.get(CONF_OFF_TEMPERATURE, 5),
            )
        ] = int

        fields[
            vol.Optional(
                CONF_CALIBRATIION_ROUND,
                default=self.config_entry.data.get(CONF_CALIBRATIION_ROUND, True),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_VALVE_MAINTENANCE,
                default=self.config_entry.data.get(CONF_VALVE_MAINTENANCE, False),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_HEAT_AUTO_SWAPPED,
                default=self.config_entry.data.get(CONF_HEAT_AUTO_SWAPPED, False),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_CHILD_LOCK,
                default=self.config_entry.data.get(CONF_CHILD_LOCK, False),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_HOMATICIP,
                default=self.config_entry.data.get(CONF_HOMATICIP, False),
            )
        ] = bool

        return self.async_show_form(step_id="user", data_schema=vol.Schema(fields))
