import logging
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    HVACMode,
    SUPPORT_TARGET_TEMPERATURE,
)
from homeassistant.const import TEMP_CELSIUS, ATTR_TEMPERATURE

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up Better Thermostat climate platform."""
    # Fügen Sie hier Ihre Logik zum Einrichten der Klimageräte hinzu
    async_add_entities([BetterThermostatClimate(config_entry.data)])


class BetterThermostatClimate(ClimateEntity):
    """Representation of a Better Thermostat climate device."""

    def __init__(self, config):
        """Initialize the thermostat."""
        self._name = config.get("name")
        self._target_temperature = config.get("target_temp", 20)
        self._current_temperature = None
        self._hvac_mode = HVACMode.HEAT

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return SUPPORT_TARGET_TEMPERATURE

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return TEMP_CELSIUS

    @property
    def hvac_mode(self):
        """Return current operation mode."""
        return self._hvac_mode

    @property
    def hvac_modes(self):
        """Return the list of available operation modes."""
        return [HVACMode.HEAT, HVACMode.OFF]

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._current_temperature

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temperature

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        if ATTR_TEMPERATURE in kwargs:
            self._target_temperature = kwargs[ATTR_TEMPERATURE]
            self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new target hvac mode."""
        self._hvac_mode = hvac_mode
        self.async_write_ha_state()
