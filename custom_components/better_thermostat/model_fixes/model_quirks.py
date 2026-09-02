"""Helpers to load per-model quirks for TRVs.

This module dynamically imports model-specific quirk modules and exposes
small shim functions that delegate into the model-specific implementations.
"""

from __future__ import annotations

import logging
import re
from types import ModuleType

from homeassistant.helpers.importlib import async_import_module

from custom_components.better_thermostat.model_fixes.types import ModelFixHost

_LOGGER = logging.getLogger(__name__)

# Model strings that no quirk module of their own answers for, mapped to the
# module that drives them instead. The Eurotronic Spirit Z and the Aeotec
# ZWA021 are one device sold under two names, so one module covers both.
_QUIRK_MODULE_ALIASES = {"Spirit": "ZWA021"}


def get_model_quirks_name(model: str | None) -> str:
    """Return the name of the quirk module a device model is driven by.

    Parameters
    ----------
    model : str | None
        Model string the device registry reports, or None while the model
        is undetermined.

    Returns
    -------
    str
        Module name to load: the model itself, unless another model's
        module answers for it.
    """
    model_str = str(model) if model is not None else ""
    return _QUIRK_MODULE_ALIASES.get(model_str, model_str)


async def load_model_quirks(self, model, entity_id) -> ModuleType:
    """Load model quirks module for a given TRV model, falling back to default.

    Emits debug logs for both the success and the fallback path.
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


def quirk_writes_valve(model_quirks: ModuleType | None) -> bool:
    """Answer whether a model's own quirk drives that model's valve.

    A quirk module carrying ``override_set_valve`` reaches the valve through
    the entities its device family exposes, which is a channel of the model
    and not of the ecosystem the device happens to be paired through. So the
    answer holds for every adapter, including the generic one a device
    without an adapter of its own falls back to.

    Parameters
    ----------
    model_quirks : ModuleType | None
        Quirk module loaded for a TRV, or None where none is loaded.

    Returns
    -------
    bool
        True when the module carries a callable ``override_set_valve``.
    """
    return callable(getattr(model_quirks, "override_set_valve", None))


def trv_state_unknown_as_available(self: ModelFixHost, entity_id: str) -> bool:
    """Answer whether a TRV is operating while its state reads ``unknown``.

    A device driven through a thermostat mode its climate entity does not
    describe reports ``unknown`` for as long as that mode holds, while it
    stays reachable and takes commands. Only the model's own quirk module
    knows that, so the answer comes from there; for every other device an
    entity that says nothing leaves the device unaccounted for.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        Entity id of the TRV whose state reads ``unknown``

    Returns
    -------
    bool
        True when ``unknown`` is this model's way of reporting an
        operating device
    """
    quirks = getattr(self.real_trvs.get(entity_id), "model_quirks", None)
    # The record holds the loaded quirk module, and only a loaded module can
    # answer; anything else is read the way an unquirked device is.
    if not isinstance(quirks, ModuleType):
        return False
    if not hasattr(quirks, "trv_state_unknown_as_available"):
        return False
    return bool(quirks.trv_state_unknown_as_available(self, entity_id))


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
