from importlib import import_module
import logging

_LOGGER = logging.getLogger(__name__)


def load_adapter(self):
    """Load adapter."""
    try:
        self.adapter = import_module(
            "custom_components.better_thermostat.adapters." + self.integration,
            package="better_thermostat",
        )
        _LOGGER.info(
            "better_thermostat %s: uses adapter %s", self.name, self.integration
        )
    except ImportError:
        self.adapter = import_module(
            "custom_components.better_thermostat.adapters.generic",
            package="better_thermostat",
        )
        _LOGGER.info("better_thermostat %s: uses adapter %s", self.name, "generic")


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
