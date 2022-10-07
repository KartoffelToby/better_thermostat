"""The better_thermostat component."""
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, Config
from homeassistant.config_entries import ConfigEntry
import logging

_LOGGER = logging.getLogger(__name__)

DOMAIN = "better_thermostat"
PLATFORMS = [Platform.CLIMATE]

_LOGGER.error("Better Thermostat is not stable release yet. Please use a Beta Version.")


async def async_setup(hass: HomeAssistant, config: Config):
    """Set up this integration using YAML is not supported."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return False
