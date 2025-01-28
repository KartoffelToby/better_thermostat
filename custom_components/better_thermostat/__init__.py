"""Better Thermostat Integration."""
import logging
import voluptuous as vol
from . import config_flow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import config_validation as cv
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Konfigurationsparameter
CONF_WINDOW_ID = "window_id"
CONF_WINDOW_DELAY = "window_delay"
CONF_DOOR_ID = "door_id"
CONF_DOOR_DELAY = "door_delay"

DEFAULT_DELAY = 30

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_WINDOW_ID): cv.entity_ids,  # Fenster-Sensoren
                vol.Optional(CONF_WINDOW_DELAY, default=DEFAULT_DELAY): cv.positive_int,
                vol.Optional(CONF_DOOR_ID): cv.entity_ids,  # Tür-Sensoren
                vol.Optional(CONF_DOOR_DELAY, default=DEFAULT_DELAY): cv.positive_int,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Better Thermostat integration from configuration.yaml."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Better Thermostat from a config entry."""
    # Fenster-Sensoren
    window_id = entry.options.get(CONF_WINDOW_ID)
    window_delay = entry.options.get(CONF_WINDOW_DELAY, DEFAULT_DELAY)

    # Tür-Sensoren
    door_id = entry.options.get(CONF_DOOR_ID)
    door_delay = entry.options.get(CONF_DOOR_DELAY, DEFAULT_DELAY)

    # Integration initialisieren
    hass.data[DOMAIN][entry.entry_id] = {
        CONF_WINDOW_ID: window_id,
        CONF_WINDOW_DELAY: window_delay,
        CONF_DOOR_ID: door_id,
        CONF_DOOR_DELAY: door_delay,
    }

    # Lade die Climate-Platform (BetterThermostat-Klasse wird hier eingebunden)
    hass.async_create_task(
        hass.config_entries.async_forward_entry_setup(entry, "climate")
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, "climate")
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
