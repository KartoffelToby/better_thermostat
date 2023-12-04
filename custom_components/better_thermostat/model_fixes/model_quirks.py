from importlib import import_module
import logging

_LOGGER = logging.getLogger(__name__)


def load_model_quirks(self, model, entity_id):
    """Load model."""

    # remove / from model
    model = model.replace("/", "_")

    try:
        self.model_quirks = import_module(
            "custom_components.better_thermostat.model_fixes." + model,
            package="better_thermostat",
        )
        _LOGGER.debug(
            "better_thermostat %s: uses quirks fixes for model %s for trv %s",
            self.name,
            model,
            entity_id,
        )
    except Exception:
        self.model_quirks = import_module(
            "custom_components.better_thermostat.model_fixes.default",
            package="better_thermostat",
        )
        pass

    return self.model_quirks


def fix_local_calibration(self, entity_id, offset):
    return self.real_trvs[entity_id]["model_quirks"].fix_local_calibration(
        self, entity_id, offset
    )


def fix_target_temperature_calibration(self, entity_id, temperature):
    return self.real_trvs[entity_id]["model_quirks"].fix_target_temperature_calibration(
        self, entity_id, temperature
    )


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return await self.real_trvs[entity_id]["model_quirks"].override_set_hvac_mode(
        self, entity_id, hvac_mode
    )


async def override_set_temperature(self, entity_id, temperature):
    return await self.real_trvs[entity_id]["model_quirks"].override_set_temperature(
        self, entity_id, temperature
    )
