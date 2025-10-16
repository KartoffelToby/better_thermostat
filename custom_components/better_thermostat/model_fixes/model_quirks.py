from homeassistant.helpers.importlib import async_import_module
import re
import logging

_LOGGER = logging.getLogger(__name__)


async def load_model_quirks(self, model, entity_id):
    """Load model quirks module for a given TRV model, falling back to default.

    Adds explicit debug logs for both success and fallback paths to make it
    visible why nothing appeared previously.
    """

    # Normalize model to a safe module suffix
    model_str = str(model) if model is not None else ""
    # Replace path separators and any non-alphanumeric/underscore with underscore
    model_sanitized = (
        re.sub(r"[^A-Za-z0-9_]+", "_", model_str.replace("/", "_")).strip("_")
        or "default"
    )
    module_path = f"custom_components.better_thermostat.model_fixes.{model_sanitized}"

    try:
        self.model_quirks = await async_import_module(self.hass, module_path)
        _LOGGER.debug(
            "better_thermostat %s: using quirks module '%s' for model '%s' (trv %s)",
            self.device_name,
            module_path,
            model_str or "<none>",
            entity_id,
        )
    except ImportError as e:
        # Fallback to default and log the reason
        default_module = "custom_components.better_thermostat.model_fixes.default"
        try:
            self.model_quirks = await async_import_module(self.hass, default_module)
            _LOGGER.debug(
                "better_thermostat %s: quirks module '%s' not available for model '%s' (trv %s): %s; using default",
                self.device_name,
                module_path,
                model_str or "<none>",
                entity_id,
                e,
            )
        except ImportError as e2:
            # This should never happen, but make it visible if it does
            _LOGGER.error(
                "better_thermostat %s: failed to import default quirks module '%s' after error loading '%s' for model '%s' (trv %s): %s",
                self.device_name,
                default_module,
                module_path,
                model_str or "<none>",
                entity_id,
                e2,
            )
            raise

    return self.model_quirks


def fix_local_calibration(self, entity_id, offset):
    """Modifies the input local calibration offset, based on the TRV's model quirks,
    to achieve the desired heating behavior.

    Returns
    -------
    float
          new local calibration offset, if the TRV model has any quirks/fixes.
    """

    _new_offset = self.real_trvs[entity_id]["model_quirks"].fix_local_calibration(
        self, entity_id, offset
    )

    _new_offset = round(_new_offset, 1)

    if offset != _new_offset:
        _LOGGER.debug(
            "better_thermostat %s: %s - calibration offset model fix: %s to %s",
            self.device_name,
            entity_id,
            offset,
            _new_offset,
        )

    return _new_offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Modifies the input setpoint temperature, based on the TRV's model quirks,
    to achieve the desired heating behavior.

    Returns
    -------
    float
          new setpoint temperature, if the TRV model has any quirks/fixes.
    """

    _new_temperature = self.real_trvs[entity_id][
        "model_quirks"
    ].fix_target_temperature_calibration(self, entity_id, temperature)

    if temperature != _new_temperature:
        _LOGGER.debug(
            "better_thermostat %s: %s - temperature offset model fix: %s to %s",
            self.device_name,
            entity_id,
            temperature,
            _new_temperature,
        )

    return _new_temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return await self.real_trvs[entity_id]["model_quirks"].override_set_hvac_mode(
        self, entity_id, hvac_mode
    )


async def override_set_temperature(self, entity_id, temperature):
    return await self.real_trvs[entity_id]["model_quirks"].override_set_temperature(
        self, entity_id, temperature
    )


async def override_set_valve(self, entity_id, percent: int):
    """Attempt model-specific valve percent write; return True if handled."""
    return await self.real_trvs[entity_id]["model_quirks"].override_set_valve(
        self, entity_id, percent
    )
