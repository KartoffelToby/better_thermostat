"""Special support for AI thermostat units."""
import asyncio
import logging

import voluptuous as vol

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
)

from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    HVAC_MODE_AUTO,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    PRESET_AWAY,
    PRESET_NONE,
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_TEMPERATURE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    EVENT_HOMEASSISTANT_START,
    SERVICE_TURN_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, CoreState, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity

from . import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "AI Thermostat"
DEFAULT_TYPE = "SPZB"

CONF_HEATER = "thermostat"
CONF_TYPE = "type"
CONF_SENSOR = "temperature_sensor"
CONF_SENSOR_WINDOW = "window_sensors"

CONF_TARGET_TEMP = "target_temp"
CONF_INITIAL_HVAC_MODE = "initial_hvac_mode"
CONF_AWAY_TEMP = "away_temp"
SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HEATER): cv.entity_id,
        vol.Required(CONF_SENSOR): cv.entity_id,
        vol.Optional(CONF_SENSOR_WINDOW): cv.entity_id,
        vol.Required(CONF_TYPE, default=DEFAULT_TYPE): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In([HVAC_MODE_HEAT, HVAC_MODE_OFF]),
        vol.Optional(CONF_AWAY_TEMP): vol.Coerce(float),
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the AI thermostat platform."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    name = config.get(CONF_NAME)
    heater_entity_id = config.get(CONF_HEATER)
    temperature_temperature_sensor_entity_id = config.get(CONF_SENSOR)
    window_sensors_entity_ids = config.get(CONF_SENSOR_WINDOW)
    thermostat_type = config.get(CONF_TYPE)
    min_temp = 5.0  # SPZB: hard coded temperature for EUROTRONIC thermostats due to the implementation in deCONZ
    max_temp = 30.0  # SPZB: hard coded temperature for EUROTRONIC thermostats due to the implementation in deCONZ
    target_temp = config.get(CONF_TARGET_TEMP)
    initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)
    away_temp = config.get(CONF_AWAY_TEMP)
    precision = 0.5  # SPZB: hard coded precision for EUROTRONIC thermostats due to the implementation in deCONZ
    unit = hass.config.units.temperature_unit

    async_add_entities(
        [
            AIThermostat(
                name,
                heater_entity_id,
                temperature_temperature_sensor_entity_id,
                window_sensors_entity_ids,
                thermostat_type,
                min_temp,
                max_temp,
                target_temp,
                initial_hvac_mode,
                away_temp,
                precision,
                unit,
            )
        ]
    )


class AIThermostat(ClimateEntity, RestoreEntity):
    """Representation of a SPZB0001 Thermostat device."""

    def __init__(
        self,
        name,
        heater_entity_id,
        temperature_sensor_entity_id,
        min_temp,
        max_temp,
        target_temp,
        initial_hvac_mode,
        away_temp,
        precision,
        unit,
    ):
        """Initialize the thermostat."""
        self._name = name
        self.heater_entity_id = heater_entity_id
        self.temperature_sensor_entity_id = temperature_sensor_entity_id
        self._hvac_mode = initial_hvac_mode
        self._saved_target_temp = target_temp or away_temp
        self._temp_precision = precision
        self._hvac_list = [HVAC_MODE_HEAT, HVAC_MODE_OFF]
        self._active = False
        self._cur_temp = None
        self._temp_lock = asyncio.Lock()
        self._min_temp = min_temp
        self._max_temp = max_temp
        self._target_temp = target_temp
        self._unit = unit
        self._support_flags = SUPPORT_FLAGS
        if away_temp:
            self._support_flags = SUPPORT_FLAGS | SUPPORT_PRESET_MODE
        self._away_temp = away_temp
        self._is_away = False
        self.startup = True  # SPZB: introduced to be able to shutdown EUROTRONIC thermostats after HA restart to avoid inconsistant states

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.temperature_sensor_entity_id], self._async_sensor_changed
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.heater_entity_id], self._async_switch_changed
            )
        )

        @callback
        def _async_startup(*_):
            """Init on startup."""
            sensor_state = self.hass.states.get(self.temperature_sensor_entity_id)
            #if sensor_state and sensor_state.state not in (
            #    STATE_UNAVAILABLE,
            #    STATE_UNKNOWN,
            #):
                #self._async_update_temp(sensor_state)
                #self.async_write_ha_state()

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

        # Check If we have an old state
        old_state = await self.async_get_last_state()
        if old_state is not None:
            # If we have no initial temperature, restore
            if self._target_temp is None:
                # If we have a previously saved temperature
                if old_state.attributes.get(ATTR_TEMPERATURE) is None:
                    self._target_temp = self.min_temp
                    _LOGGER.warning(
                        "Undefined target temperature, falling back to %s",
                        self._target_temp,
                    )
                else:
                    self._target_temp = float(old_state.attributes[ATTR_TEMPERATURE])
            if old_state.attributes.get(ATTR_PRESET_MODE) == PRESET_AWAY:
                self._is_away = True
            if not self._hvac_mode and old_state.state:
                self._hvac_mode = old_state.state

        else:
            # No previous state, try and restore defaults
            if self._target_temp is None:
                self._target_temp = self.min_temp
            _LOGGER.warning(
                "No previously saved temperature, setting to %s", self._target_temp
            )

        # Set default state to off
        if not self._hvac_mode:
            self._hvac_mode = HVAC_MODE_OFF

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def precision(self):
        """Return the precision of the system."""
        if self._temp_precision is not None:
            return self._temp_precision
        return super().precision

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        # Since this integration does not yet have a step size parameter
        # we have to re-use the precision as the step size for now.
        return self.precision

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_temperature(self):
        """Return the sensor temperature."""
        return self._cur_temp

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be one of CURRENT_HVAC_*.
        """
        if self._hvac_mode == HVAC_MODE_OFF:
            return CURRENT_HVAC_OFF
        if not self._is_device_active:
            return CURRENT_HVAC_IDLE
        return CURRENT_HVAC_HEAT

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temp

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        return self._hvac_list

    @property
    def preset_mode(self):
        """Return the current preset mode, e.g., home, away, temp."""
        return PRESET_AWAY if self._is_away else PRESET_NONE

    @property
    def preset_modes(self):
        """Return a list of available preset modes or PRESET_NONE if _away_temp is undefined."""
        return [PRESET_NONE, PRESET_AWAY] if self._away_temp else PRESET_NONE

    async def async_set_hvac_mode(self, hvac_mode):
        """Set hvac mode."""
        if hvac_mode == HVAC_MODE_HEAT:
            self._hvac_mode = HVAC_MODE_HEAT
            #await self._async_control_heating()
        elif hvac_mode == HVAC_MODE_OFF:
            self._hvac_mode = HVAC_MODE_OFF
            #if self._is_device_active:
             #   await self._async_heater_turn_off()
        else:
            _LOGGER.error("Unrecognized hvac mode: %s", hvac_mode)
            return
        # Ensure we update the current operation after changing the mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        self._target_temp = temperature
        #await self._async_control_heating()
        self.async_write_ha_state()

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        if self._min_temp is not None:
            return self._min_temp

        # get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        if self._max_temp is not None:
            return self._max_temp

        # Get default temp from super class
        return super().max_temp

    async def _async_sensor_changed(self, event):
        """Handle temperature changes."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        _LOGGER.debug("_async_sensor_changed runs for %s with state %s", new_state.name, new_state) #SPZB: log for debugging
        _LOGGER.debug("_async_sensor_changed runs for %s", new_state.name) #SPZB: log for debugging
        self._async_update_temp(new_state)
        await self._async_control_heating()
        self.async_write_ha_state()

    @callback
    # SPZB: made async to be able to call async functions for EUROTRONIC thermostat
    async def _async_switch_changed(self, event):
        """Handle heater switch state changes."""
        # SPZB: also get old state for handling EUROTRONIC thermostat HVAC modes
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        # SPZB: also check if old state is ok
        if new_state is None or old_state is None:
            return
        # SPZB: handle EUROTRONIC SPIRIT ZIGBEE thermostat
        # SPZB: Service set HVAC mode back to auto if set from auto to heat (e.g. manually)
        # if old_state.state != new_state.state: #SPZB: log for debugging (needs this and next line to work properly)
        _LOGGER.debug("Changed state from %s to %s for %s.", old_state.state, new_state.state, new_state.name) #SPZB: log for debugging
        if old_state.state == "auto" and new_state.state == "heat":
            data_auto = {
                ATTR_ENTITY_ID: self.heater_entity_id,
                ATTR_HVAC_MODE: HVAC_MODE_AUTO,
            }
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
                data_auto,
                blocking=True,
            )
            await asyncio.sleep(
                5
            )  # SPZB: wait for 5 seconds due to issues with sending command too fast
            # SPZB: Service set temperature to max_temp
            data_temp = {
                ATTR_ENTITY_ID: self.heater_entity_id,
                ATTR_TEMPERATURE: self.max_temp,
            }
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_TEMPERATURE,
                data_temp,
                blocking=True,
            )
            await asyncio.sleep(
                25
            )  # SPZB: wait for 25 seconds to let the thermostat finish before sending another command
            _LOGGER.debug("Something tried to switch from auto to heat for %s, so we revert HVAC mode to auto", self.heater_entity_id) #SPZB: log for debugging
        # SPZB: Service set HVAC mode back to auto if set from off to heat (e.g. manually)
        elif old_state.state == "off" and new_state.state == "heat":
            data_auto = {
                ATTR_ENTITY_ID: self.heater_entity_id,
                ATTR_HVAC_MODE: HVAC_MODE_AUTO,
            }
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
                data_auto,
                blocking=True,
            )
            _LOGGER.debug("data_auto: %s", data_auto) # SPZB: log for debugging
            await asyncio.sleep(
                5
            )  # SPZB: wait for 5 seconds due to issues with sending command too fast
            # SPZB: Service set temperature to max_temp
            data_temp = {
                ATTR_ENTITY_ID: self.heater_entity_id,
                ATTR_TEMPERATURE: self.max_temp,
            }
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_TEMPERATURE,
                data_temp,
                blocking=True,
            )
            await asyncio.sleep(
                25
            )  # SPZB: wait for 25 seconds to let the thermostat finish before sending another command
            _LOGGER.debug("data_temp: %s", data_temp) #SPZB: log for debugging
            _LOGGER.debug("Something tried to switch from off to heat for %s, so we change HVAC mode to auto", self.heater_entity_id) #SPZB: log for debugging
        self.async_write_ha_state()

    @callback
    def _async_update_temp(self, state):
        """Update thermostat with latest state from sensor."""
        try:
            self._cur_temp = float(state.state)
            _LOGGER.debug("_async_update_temp: %s for %s", self._cur_temp, self.heater_entity_id) #SPZB: log for debugging
        except ValueError as ex:
            _LOGGER.error("Unable to update from sensor: %s", ex)

    async def _async_control_heating(self):
        """Check if we need to turn heating on or off."""
        if self.startup == True:  # SPZB: check if HA was freshly initialized
            await self._async_init_shutdown_thermostat()  # SPZB: turn of the corresponding EUROTRONIC thermostat on startup
        _LOGGER.debug("_async_control_heating running for %s", self.heater_entity_id) #SPZB: log for debugging
        async with self._temp_lock:
            if not self._active and None not in (self._cur_temp, self._target_temp):
                self._active = True
                _LOGGER.debug(
                    "Obtained current and target temperature. "
                    "SPZB0001 thermostat active. %s, %s",
                    self._cur_temp,
                    self._target_temp,
                )

            if not self._active or self._hvac_mode == HVAC_MODE_OFF:
                self._async_heater_turn_off()
                return

            """
            too_cold = self._target_temp >= self._cur_temp
            too_hot = self._cur_temp >= self._target_temp
            # SPZB: log for debugging
            if self._is_device_active:
                if too_hot:
                    _LOGGER.debug("Turning off heater %s", self.heater_entity_id)
                    await self._async_heater_turn_off()
            else:
                if too_cold:
                    _LOGGER.debug("Turning on heater %s", self.heater_entity_id)
                    await self._async_heater_turn_on()
            """

    @property
    def _is_device_active(self):
        """If the toggleable device is currently active."""
        # SPZB: check for state == "heat"/"auto"/"off" instead of STATE_ON for EUROTRONIC Thermostat ...
        # SPZB: also check set temperature if device is set to "auto", if it is set to 5°C then it's off
        state_off = self.hass.states.is_state(self.heater_entity_id, "off")
        state_heat = self.hass.states.is_state(self.heater_entity_id, "heat")
        state_auto = self.hass.states.is_state(self.heater_entity_id, "auto")
        state_temp = self.hass.states.get(self.heater_entity_id)
        _LOGGER.debug("%s.state = %s", self.heater_entity_id, state_temp) #SPZB: log for debugging
        _LOGGER.debug("%s.SetPointTemp = %s", self.heater_entity_id, state_temp.attributes[ATTR_TEMPERATURE] if state_temp != None else None) #SPZB: log for debugging
        if (state_auto and state_temp.attributes[ATTR_TEMPERATURE] == 5.0) or state_off:
            _LOGGER.debug("state_auto: %s and %s.SetPointTemp = %s", state_auto, self.heater_entity_id, state_temp.attributes[ATTR_TEMPERATURE]) #SPZB: log for debugging
            return False
        elif state_heat:
            _LOGGER.debug("state_heat: %s for %s", state_heat, self.heater_entity_id) #SPZB: log for debugging
            return state_heat
        elif state_auto:
            _LOGGER.debug("state_auto: %s for %s", state_auto, self.heater_entity_id) #SPZB: log for debugging
            return state_auto

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    async def _async_heater_turn_on(self):
        """Turn heater toggleable device on."""
        # SPZB: handle EUROTRONIC SPIRIT ZIGBEE thermostat
        # SPZB: Service set HVAC mode to auto
        """
        data_auto = {
            ATTR_ENTITY_ID: self.heater_entity_id,
            ATTR_HVAC_MODE: HVAC_MODE_AUTO,
        }
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            data_auto,
            blocking=True,
        )
        await asyncio.sleep(
            5
        )  # SPZB: wait for 5 seconds due to issues with sending command too fast
        _LOGGER.debug("data_auto: %s for %s", data_auto, self.heater_entity_id) #SPZB: log for debugging
        # SPZB: Service set temperature to max_temp
        data_temp = {
            ATTR_ENTITY_ID: self.heater_entity_id,
            ATTR_TEMPERATURE: self.max_temp,
        }
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            data_temp,
            blocking=True,
        )
        await asyncio.sleep(
            25
        )  # SPZB: wait for 25 seconds to let the thermostat finish before sending another command
        """
        _LOGGER.debug("data_temp: %s for %s", data_temp, self.heater_entity_id) #SPZB: log for debugging
        _LOGGER.debug("_async_heater_turn_on executed for %s", self.heater_entity_id) #SPZB: log for debugging

    async def _async_heater_turn_off(self):
        """Turn heater toggleable device off."""
        # SPZB: handle EUROTRONIC SPIRIT ZIGBEE thermostat
        # SPZB: Service set temperature to min_temp
        """
        data_temp = {
            ATTR_ENTITY_ID: self.heater_entity_id,
            ATTR_TEMPERATURE: self.min_temp,
        }
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            data_temp,
            blocking=True,
        )
        await asyncio.sleep(
            25
        )  # SPZB: wait for 25 seconds due to issues with sending command too fast
        # SPZB: Service set HVAC mode to off
        data_off = {
            ATTR_ENTITY_ID: self.heater_entity_id,
            ATTR_HVAC_MODE: HVAC_MODE_OFF,
        }
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            data_off,
            blocking=True,
        )
        await asyncio.sleep(
            5
        )  # SPZB: wait for 5 seconds due to issues with sending command too fast
        # SPZB: Service send off to thermostat
        data = {ATTR_ENTITY_ID: self.heater_entity_id}
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_OFF, data, context=self._context
        )
        await asyncio.sleep(
            30
        )  # SPZB: wait for 30 seconds to let the thermostat finish before sending another command
        """
        _LOGGER.debug("_async_heater_turn_off executed for %s", self.heater_entity_id) #SPZB: log for debugging

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        if preset_mode == PRESET_AWAY and not self._is_away:
            self._is_away = True
            self._saved_target_temp = self._target_temp
            self._target_temp = self._away_temp
            await self._async_control_heating()
        elif preset_mode == PRESET_NONE and self._is_away:
            self._is_away = False
            self._target_temp = self._saved_target_temp
            await self._async_control_heating()

        self.async_write_ha_state()

    async def _async_init_shutdown_thermostat(
        self,
    ):  # SPZB: new function for avoiding inconsistency on startup
        """Shutdown the connected SPZB0001 thermostat after restart of HA to prevent wrong state"""
        await self._async_heater_turn_off()  # turn of the corresponding EUROTRONIC thermostat on startup
        self.startup = False
        _LOGGER.debug("_async_init_shutdown_thermostat running for %s", self.heater_entity_id) #SPZB: log for debugging
