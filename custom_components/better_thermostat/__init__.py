"""The better_thermostat component."""
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, Config
from homeassistant.config_entries import ConfigEntry
DOMAIN = "better_thermostat"
PLATFORMS = [Platform.CLIMATE]


async def async_setup(hass: HomeAssistant, config: Config):
	"""Set up this integration using YAML is not supported."""
	return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
	hass.config_entries.async_setup_platforms(entry, PLATFORMS)
	return True