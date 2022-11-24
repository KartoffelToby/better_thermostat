import logging


import voluptuous as vol
from collections import OrderedDict

from .utils.bridge import load_adapter

from .utils.helpers import get_device_model, get_trv_intigration

from .const import (
    CONF_CALIBRATIION_ROUND,
    CONF_CALIBRATION,
    CONF_CHILD_LOCK,
    CONF_FIX_CALIBRATION,
    CONF_HEAT_AUTO_SWAPPED,
    CONF_HEATER,
    CONF_HOMATICIP,
    CONF_HUMIDITY,
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
from homeassistant.components.climate.const import HVACMode


from . import DOMAIN  # pylint:disable=unused-import

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        """Initialize the config flow."""
        self.name = ""
        self.data = None
        self.model = None
        self.heater_entity_id = None
        self.trv_bundle = []
        self.integration = None
        self.i = 0

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
        self.data[CONF_HEATER] = self.trv_bundle
        if user_input is not None:
            if self.data is not None:
                _LOGGER.debug("Confirm: %s", self.data[CONF_HEATER])
                await self.async_set_unique_id(self.data["name"])
                return self.async_create_entry(title=self.data["name"], data=self.data)
        if confirm_type is not None:
            errors["base"] = confirm_type
        _trvs = ",".join([x["trv"] for x in self.data[CONF_HEATER]])
        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            description_placeholders={"name": self.data[CONF_NAME], "trv": _trvs},
        )

    async def async_step_advanced(self, user_input=None, _trv_config=None):
        """Handle options flow."""
        if user_input is not None:

            self.trv_bundle[self.i]["advanced"] = user_input
            self.trv_bundle[self.i]["adapter"] = None

            self.i += 1
            if len(self.trv_bundle) > self.i:
                return await self.async_step_advanced(None, self.trv_bundle[self.i])

            _has_off_mode = True
            for trv in self.trv_bundle:
                if HVACMode.OFF not in self.hass.states.get(
                    trv.get("trv")
                ).attributes.get("hvac_modes"):
                    _has_off_mode = False

            if _has_off_mode is False:
                return await self.async_step_confirm(None, "no_off_mode")
            return await self.async_step_confirm()

        user_input = user_input or {}
        homematic = False
        if _trv_config.get("integration").find("homematic") != -1:
            homematic = True

        fields = OrderedDict()

        _calibration = {"target_temp_based": "Target Temperature"}
        _default_calibration = "target_temp_based"
        _adapter = _trv_config.get("adapter", None)
        if _adapter is not None:
            _info = await _adapter.get_info(self, _trv_config.get("trv"))

            if _info.get("support_offset", False):
                _calibration["local_calibration_based"] = "Local Calibration"
                _default_calibration = "local_calibration_based"

        fields[
            vol.Required(
                CONF_CALIBRATION,
                default=user_input.get(CONF_CALIBRATION, _default_calibration),
            )
        ] = vol.In(_calibration)

        has_auto = False
        trv = self.hass.states.get(_trv_config.get("trv"))
        if HVACMode.AUTO in trv.attributes.get("hvac_modes"):
            has_auto = True

        fields[
            vol.Optional(
                CONF_HEAT_AUTO_SWAPPED,
                default=user_input.get(CONF_HEAT_AUTO_SWAPPED, has_auto),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_FIX_CALIBRATION,
                default=user_input.get(CONF_FIX_CALIBRATION, False),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_CALIBRATIION_ROUND,
                default=user_input.get(CONF_CALIBRATIION_ROUND, True),
            )
        ] = bool

        if _info.get("support_valve", False):
            fields[
                vol.Optional(
                    CONF_VALVE_MAINTENANCE,
                    default=user_input.get(CONF_VALVE_MAINTENANCE, False),
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
            step_id="advanced",
            data_schema=vol.Schema(fields),
            last_step=False,
            description_placeholders={"trv": _trv_config.get("trv")},
        )

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            if self.data is None:
                self.data = user_input
            self.heater_entity_id = self.data[CONF_HEATER]
            if self.data[CONF_NAME] == "":
                errors["base"] = "no_name"
            if CONF_SENSOR_WINDOW not in self.data:
                self.data[CONF_SENSOR_WINDOW] = None
            if CONF_HUMIDITY not in self.data:
                self.data[CONF_HUMIDITY] = None
            if CONF_OUTDOOR_SENSOR not in self.data:
                self.data[CONF_OUTDOOR_SENSOR] = None
            if CONF_WEATHER not in self.data:
                self.data[CONF_WEATHER] = None
            if "base" not in errors:
                for trv in self.heater_entity_id:
                    _intigration = await get_trv_intigration(self, trv)
                    self.trv_bundle.append(
                        {
                            "trv": trv,
                            "integration": _intigration,
                            "model": await get_device_model(self, trv),
                            "adapter": load_adapter(self, _intigration, trv),
                        }
                    )

                self.data[CONF_MODEL] = "/".join([x["model"] for x in self.trv_bundle])
                return await self.async_step_advanced(None, self.trv_bundle[0])

        user_input = user_input or {}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default=user_input.get(CONF_NAME, "")): str,
                    vol.Required(CONF_HEATER): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="climate", multiple=True)
                    ),
                    vol.Required(CONF_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "number", "input_number"],
                            device_class="temperature",
                            multiple=False,
                        )
                    ),
                    vol.Optional(CONF_HUMIDITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "number", "input_number"],
                            device_class="humidity",
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
        self.i = 0
        self.trv_bundle = []
        self.name = ""
        self._last_step = False
        self.updated_config = {}

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        return await self.async_step_user()

    async def async_step_advanced(
        self, user_input=None, _trv_config=None, _update_config=None
    ):
        """Manage the advanced options."""
        if user_input is not None:

            self.trv_bundle[self.i]["advanced"] = user_input
            self.trv_bundle[self.i]["adapter"] = None

            self.i += 1
            if len(self.trv_bundle) - 1 >= self.i:
                self._last_step = True

            if len(self.trv_bundle) > self.i:
                return await self.async_step_advanced(
                    None, self.trv_bundle[self.i], _update_config
                )

            self.updated_config[CONF_HEATER] = self.trv_bundle
            _LOGGER.debug("Updated config: %s", self.updated_config)
            # self.hass.config_entries.async_update_entry(self.config_entry, data=self.updated_config)
            return self.async_create_entry(title="", data=self.updated_config)

        user_input = user_input or {}
        homematic = False
        if _trv_config.get("integration").find("homematic") != -1:
            homematic = True

        fields = OrderedDict()

        _calibration = {"target_temp_based": "Target Temperature"}
        _default_calibration = "target_temp_based"
        _adapter = _trv_config.get("adapter", None)
        if _adapter is not None:
            _info = await _adapter.get_info(self, _trv_config.get("trv"))

            if _info.get("support_offset", False):
                _calibration["local_calibration_based"] = "Local Calibration"
                _default_calibration = "local_calibration_based"

        fields[
            vol.Required(
                CONF_CALIBRATION,
                default=_trv_config["advanced"].get(
                    CONF_CALIBRATION, _default_calibration
                ),
            )
        ] = vol.In(_calibration)

        has_auto = False
        trv = self.hass.states.get(_trv_config.get("trv"))
        if HVACMode.AUTO in trv.attributes.get("hvac_modes"):
            has_auto = True

        fields[
            vol.Optional(
                CONF_HEAT_AUTO_SWAPPED,
                default=_trv_config["advanced"].get(CONF_HEAT_AUTO_SWAPPED, has_auto),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_FIX_CALIBRATION,
                default=_trv_config["advanced"].get(CONF_FIX_CALIBRATION, False),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_CALIBRATIION_ROUND,
                default=_trv_config["advanced"].get(CONF_CALIBRATIION_ROUND, True),
            )
        ] = bool

        if _info.get("support_valve", False):
            fields[
                vol.Optional(
                    CONF_VALVE_MAINTENANCE,
                    default=_trv_config["advanced"].get(CONF_VALVE_MAINTENANCE, False),
                )
            ] = bool

        fields[
            vol.Optional(
                CONF_CHILD_LOCK,
                default=_trv_config["advanced"].get(CONF_CHILD_LOCK, False),
            )
        ] = bool
        fields[
            vol.Optional(
                CONF_HOMATICIP,
                default=_trv_config["advanced"].get(CONF_HOMATICIP, homematic),
            )
        ] = bool

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(fields),
            last_step=self._last_step,
            description_placeholders={"trv": _trv_config.get("trv")},
        )

    async def async_step_user(self, user_input=None):

        if user_input is not None:
            current_config = self.config_entry.data
            self.updated_config = dict(current_config)
            self.updated_config[CONF_SENSOR] = user_input.get(CONF_SENSOR, None)
            self.updated_config[CONF_SENSOR_WINDOW] = user_input.get(
                CONF_SENSOR_WINDOW, None
            )
            self.updated_config[CONF_HUMIDITY] = user_input.get(CONF_HUMIDITY, None)
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
            self.name = user_input.get(CONF_NAME, "-")
            for trv in self.updated_config[CONF_HEATER]:
                trv["adapter"] = load_adapter(self, trv["integration"], trv["trv"])
                self.trv_bundle.append(trv)

            return await self.async_step_advanced(
                None, self.trv_bundle[0], self.updated_config
            )

        fields = OrderedDict()

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
                CONF_HUMIDITY,
                description={
                    "suggested_value": self.config_entry.data.get(CONF_HUMIDITY, "")
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "number", "input_number"],
                device_class="humidity",
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

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(fields), last_step=False
        )
