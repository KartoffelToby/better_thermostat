"""Helpers to load per-model quirks for TRVs.

This module dynamically imports model-specific quirk modules and exposes
small shim functions that delegate into the model-specific implementations.
"""

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
        re.sub(r"[^A-Za-z0-9_-]+", "_", model_str.replace("/", "_")).strip("_")
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
    """Apply model-specific local calibration fix.

    Call the configured model quirks implementation to normalize the given
    local calibration offset.
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
    """Apply model-specific setpoint calibration fix.

    Delegates to the loaded model quirks module for any adjustments to the
    requested setpoint temperature.
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
    """Invoke model-specific HVAC mode override, if implemented.

    Returns the model-quirks module's response (True if handled).
    """
    return await self.real_trvs[entity_id]["model_quirks"].override_set_hvac_mode(
        self, entity_id, hvac_mode
    )


async def override_set_temperature(self, entity_id, temperature):
    """Invoke model-specific temperature override, if implemented.

    Returns the model-quirks module's response (True if handled).
    """
    return await self.real_trvs[entity_id]["model_quirks"].override_set_temperature(
        self, entity_id, temperature
    )


async def override_set_valve(self, entity_id, percent: int):
    """Attempt model-specific valve percent write; return True if handled."""
    return await self.real_trvs[entity_id]["model_quirks"].override_set_valve(
        self, entity_id, percent
    )
