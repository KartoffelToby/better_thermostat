from importlib import import_module
import logging

_LOGGER = logging.getLogger(__name__)


def load_adapter(self):
    """Load adapter."""
    if self.integration == "generic_thermostat":
        self.integration = "generic"

    try:
        self.adapter = import_module(
            "custom_components.better_thermostat.adapters." + self.integration,
            package="better_thermostat",
        )
        _LOGGER.debug(
            "better_thermostat %s: uses adapter %s", self.name, self.integration
        )
    except Exception:
        self.adapter = import_module(
            "custom_components.better_thermostat.adapters.generic",
            package="better_thermostat",
        )
        _LOGGER.warning(
            "better_thermostat %s: intigration: %s isn't native supported, feel free to open an issue, fallback adapter %s",
            self.name,
            self.integration,
            "generic",
        )
        pass
    return self.adapter


async def init(self):
    """Init adapter."""
    return await self.adapter.init(self)


async def get_info(self):
    return await self.adapter.get_info(self)


async def set_temperature(self, temperature):
    """Set new target temperature."""
    return await self.adapter.set_temperature(self, temperature)


async def set_hvac_mode(self, hvac_mode):
    """Set new target hvac mode."""
    return await self.adapter.set_hvac_mode(self, hvac_mode)


async def set_offset(self, offset):
    """Set new target offset."""
    return await self.adapter.set_offset(self, offset)


async def set_valve(self, valve):
    """Set new target valve."""
    return await self.adapter.set_valve(self, valve)
