from importlib import import_module
import logging

_LOGGER = logging.getLogger(__name__)


def load_adapter(self, integration, entity_id, get_name=False):
    """Load adapter."""
    if get_name:
        self.name = "-"

    if integration == "generic_thermostat":
        integration = "generic"

    try:
        self.adapter = import_module(
            "custom_components.better_thermostat.adapters." + integration,
            package="better_thermostat",
        )
        _LOGGER.debug(
            "better_thermostat %s: uses adapter %s for trv %s",
            self.name,
            integration,
            entity_id,
        )
    except Exception:
        self.adapter = import_module(
            "custom_components.better_thermostat.adapters.generic",
            package="better_thermostat",
        )
        _LOGGER.info(
            "better_thermostat %s: intigration: %s isn't native supported, feel free to open an issue, fallback adapter %s",
            self.name,
            integration,
            "generic",
        )
        pass

    if get_name:
        return integration
    return self.adapter


async def init(self, entity_id):
    """Init adapter."""
    return await self.real_trvs[entity_id]["adapter"].init(self, entity_id)


async def get_info(self, entity_id):
    return await self.real_trvs[entity_id]["adapter"].get_info(self, entity_id)


async def get_current_offset(self, entity_id):
    """Get current offset."""
    return await self.real_trvs[entity_id]["adapter"].get_current_offset(
        self, entity_id
    )


async def get_offset_steps(self, entity_id):
    """get offset setps."""
    return await self.real_trvs[entity_id]["adapter"].get_offset_steps(self, entity_id)


async def get_min_offset(self, entity_id):
    """Get min offset."""
    return await self.real_trvs[entity_id]["adapter"].get_min_offset(self, entity_id)


async def get_max_offset(self, entity_id):
    """Get max offset."""
    return await self.real_trvs[entity_id]["adapter"].get_max_offset(self, entity_id)


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await self.real_trvs[entity_id]["adapter"].set_temperature(
        self, entity_id, temperature
    )


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    return await self.real_trvs[entity_id]["adapter"].set_hvac_mode(
        self, entity_id, hvac_mode
    )


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    return await self.real_trvs[entity_id]["adapter"].set_offset(
        self, entity_id, offset
    )


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    return await self.real_trvs[entity_id]["adapter"].set_valve(self, entity_id, valve)
