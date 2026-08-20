"""Helpers to load per-model quirks for TRVs.

This module dynamically imports model-specific quirk modules and exposes
small shim functions that delegate into the model-specific implementations.
"""

from __future__ import annotations

import logging
import re

from homeassistant.helpers.importlib import async_import_module

_LOGGER = logging.getLogger(__name__)


def get_model_quirks_name(model):
    """Return the model quirks module name for a TRV model.

    Parameters
    ----------
    model : object or None
        TRV model identifier. The ``Spirit`` model is mapped to ``ZWA021``;
        other values are converted to strings.

    Returns
    -------
    str
        Model quirks module name, or an empty string when ``model`` is
        ``None``.
    """
    if model is not None:
        model_str = str(model)
        match model_str:
            case "Spirit":
                model_quirks_name = "ZWA021"
            case _:
                model_quirks_name = model_str
    else:
        model_quirks_name = ""
    return model_quirks_name


async def load_model_quirks(self, model, entity_id):
    """Load model quirks module for a given TRV model, falling back to default.

    Emits debug logs for both the success and the fallback path.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    model : object or None
        TRV model identifier used to select the quirks module.
    entity_id : str
        TRV entity identifier used in diagnostic log messages.

    Returns
    -------
    module
        Imported model quirks module, or the default quirks module when the
        model-specific module is unavailable.

    Raises
    ------
    ImportError
        If the model-specific module is unavailable and the default module
        cannot be imported.
    """

    # Normalize model to a safe module suffix
    model_str = get_model_quirks_name(model)
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


def trv_state_unknown_as_available(self, entity_id):
    """Return True if this TRV is operating when its Climate entity state is STATE_UNKNOWN.

    Call the configured model quirks implementation to determine it.
    Some TRVs have a Climate Entity specific Manufacturer Mode for direct valve control
    that leads to having TRV Climate entity STATE_UNKNOWN even when the device is actually
    available and controllable.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity identifier of the TRV to check.

    Returns
    -------
    bool
        Whether the TRV should be treated as available while its Climate
        entity state is ``STATE_UNKNOWN``. Returns ``False`` when no
        configured model quirks implementation provides this policy.
    """
    _trv = self.real_trvs.get(entity_id)
    if _trv is not None and hasattr(_trv, "model_quirks"):
        quirks = _trv.model_quirks
        if hasattr(quirks, "trv_state_unknown_as_available"):
            return quirks.trv_state_unknown_as_available(self, entity_id)
    return False


def fix_local_calibration(self, entity_id, offset):
    """Apply model-specific local calibration fix.

    Call the configured model quirks implementation to normalize the given
    local calibration offset.
    """

    _new_offset = self.real_trvs[entity_id].model_quirks.fix_local_calibration(
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


def fix_valve_calibration(self, entity_id, valve):
    """Apply model-specific valve calibration fix.

    Call the configured model quirks implementation to normalize the given
    valve calibration value.
    """

    quirks = self.real_trvs[entity_id].model_quirks
    if hasattr(quirks, "fix_valve_calibration"):
        _new_valve = quirks.fix_valve_calibration(self, entity_id, valve)
    else:
        _new_valve = valve

    if valve != _new_valve:
        _LOGGER.debug(
            "better_thermostat %s: %s - valve calibration model fix: %s to %s",
            self.device_name,
            entity_id,
            valve,
            _new_valve,
        )

    return _new_valve


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Apply model-specific setpoint calibration fix.

    Delegates to the loaded model quirks module for any adjustments to the
    requested setpoint temperature.
    """

    _new_temperature = self.real_trvs[
        entity_id
    ].model_quirks.fix_target_temperature_calibration(self, entity_id, temperature)

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
    return await self.real_trvs[entity_id].model_quirks.override_set_hvac_mode(
        self, entity_id, hvac_mode
    )


async def override_set_temperature(self, entity_id, temperature):
    """Invoke model-specific temperature override, if implemented.

    Returns the model-quirks module's response (True if handled).
    """
    return await self.real_trvs[entity_id].model_quirks.override_set_temperature(
        self, entity_id, temperature
    )


async def initial_tweak(self, entity_id):
    """Run initial tweaks for the device."""
    quirks = self.real_trvs[entity_id].model_quirks
    if hasattr(quirks, "initial_tweak"):
        await quirks.initial_tweak(self, entity_id)
