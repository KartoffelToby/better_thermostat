"""Helpers to load per-model quirks for TRVs.

This module dynamically imports model-specific quirk modules and exposes
small shim functions that delegate into the model-specific implementations.
"""

from __future__ import annotations

import logging
import re

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.helpers.importlib import async_import_module

from custom_components.better_thermostat.model_fixes.types import (
    InitialTweakQuirk,
    LowestSetpointQuirk,
    ModelFixHost,
    ModelQuirks,
    QuirkLoaderHost,
    ReversedOffsetQuirk,
    SetpointOffsetQuirk,
    UnknownStateQuirk,
    ValveQuirk,
)

_LOGGER = logging.getLogger(__name__)

# Model strings that no quirk module of their own answers for, mapped to the
# module that drives them instead. The Eurotronic Spirit Z and the Aeotec
# ZWA021 are one device sold under two names, so one module covers both.
# ZVIDAR Z-TRV-V01 is a clone of the same device and drives its valve the same
# way, so it resolves onto that module too.
_QUIRK_MODULE_ALIASES = {"Spirit": "ZWA021", "Z-TRV-V01": "ZWA021"}


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
    model_str = model if model is not None else ""
    return _QUIRK_MODULE_ALIASES.get(model_str, model_str)


async def _import_quirks(self: QuirkLoaderHost, module_path: str) -> ModelQuirks:
    """Import a quirk module and hold it to the :class:`ModelQuirks` surface.

    Parameters
    ----------
    self : QuirkLoaderHost
        Caller whose Home Assistant core runs the import.
    module_path : str
        Dotted path of the quirk module.

    Returns
    -------
    ModelQuirks
        The imported module.

    Raises
    ------
    ImportError
        When the module cannot be imported, or lacks part of the surface
        every quirk module provides.
    """
    module: object = await async_import_module(self.hass, module_path)
    if not isinstance(module, ModelQuirks):
        raise ImportError(
            f"quirks module '{module_path}' lacks part of the model quirk surface"
        )
    return module


async def load_model_quirks(
    self: QuirkLoaderHost, model: str | None, entity_id: str
) -> ModelQuirks:
    """Load model quirks module for a given TRV model, falling back to default.

    A module that imports but lacks part of the :class:`ModelQuirks`
    surface is passed over like one that does not import. Emits debug
    logs for both the success and the fallback path.
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
        model_quirks = await _import_quirks(self, module_path)
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
            model_quirks = await _import_quirks(self, default_module)
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

    return model_quirks


def _quirks(self: ModelFixHost, entity_id: str) -> ModelQuirks:
    quirks = self.real_trvs[entity_id].model_quirks
    if quirks is None:
        raise AttributeError(f"no model quirks loaded for {entity_id}")
    return quirks


def quirk_writes_valve(model_quirks: object) -> bool:
    """Answer whether a model's own quirk drives that model's valve.

    A quirk module carrying ``override_set_valve`` reaches the valve through
    the entities its device family exposes, which is a channel of the model
    and not of the ecosystem the device happens to be paired through. So the
    answer holds for every adapter, including the generic one a device
    without an adapter of its own falls back to.

    Parameters
    ----------
    model_quirks : object
        Quirk module loaded for a TRV, or None where none is loaded.

    Returns
    -------
    bool
        True when the module carries ``override_set_valve``.
    """
    return isinstance(model_quirks, ValveQuirk)


def local_calibration_shifts_setpoint(self: ModelFixHost, entity_id: str) -> bool:
    """Answer whether a TRV applies its calibration offset to the setpoint.

    Most devices add the offset to the temperature they measure and report
    that sum. A device that adds it to its setpoint instead keeps reporting
    the bare reading, and an offset raises its effective setpoint where it
    would lower the reading of the others, so the offset acts with the
    opposite sign. Only the model's own quirk module knows which kind a
    device is; a device without that answer offsets its reading.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        Entity id of the TRV the offset is written to

    Returns
    -------
    bool
        True when the device adds the offset to its setpoint and reports
        its reading without it
    """
    trv = self.real_trvs.get(entity_id)
    quirks: object = trv.model_quirks if trv is not None else None
    if not isinstance(quirks, SetpointOffsetQuirk):
        return False
    return quirks.local_calibration_shifts_setpoint(self, entity_id)


def local_calibration_reverses_sign(self: ModelFixHost, entity_id: str) -> bool:
    """Answer whether a TRV applies its calibration offset with the opposite sign.

    Most devices add the offset to the temperature they measure and report
    that sum. Some subtract it instead and report the difference, so an
    offset meant to lower the reading raises it. Such a device reads the
    offset back in its own sign as well. Only the model's own quirk module
    knows which kind a device is; a device without that answer adds the
    offset to its reading.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        Entity id of the TRV the offset is written to

    Returns
    -------
    bool
        True when the device subtracts the offset from its reading
    """
    trv = self.real_trvs.get(entity_id)
    quirks: object = trv.model_quirks if trv is not None else None
    if not isinstance(quirks, ReversedOffsetQuirk):
        return False
    return quirks.local_calibration_reverses_sign(self, entity_id)


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
    trv = self.real_trvs.get(entity_id)
    quirks: object = trv.model_quirks if trv is not None else None
    # Only a record whose quirks define the function can answer. The check
    # reads attributes statically, so a mock that would make the function
    # up on lookup is read the way an unquirked device is.
    if not isinstance(quirks, UnknownStateQuirk):
        return False
    return quirks.trv_state_unknown_as_available(self, entity_id)


def trv_report_is_unreadable(
    self: ModelFixHost, entity_id: str, state: State | None
) -> bool:
    """Answer whether a TRV state carries nothing the inbound handler can read.

    A missing state and an unavailable one carry nothing. ``unknown`` carries
    nothing either, unless the model reports an operating device that way.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        Entity id of the TRV the state belongs to
    state : State | None
        The state the TRV publishes

    Returns
    -------
    bool
        True when the state is to be read as the device being gone
    """
    return (
        state is None
        or state.state == STATE_UNAVAILABLE
        or (
            state.state == STATE_UNKNOWN
            and not trv_state_unknown_as_available(self, entity_id)
        )
    )


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Apply model-specific local calibration fix.

    Call the configured model quirks implementation to normalize the given
    local calibration offset.
    """

    _new_offset = _quirks(self, entity_id).fix_local_calibration(
        self, entity_id, calibration_offset
    )

    _new_offset = round(_new_offset, 1)

    if calibration_offset != _new_offset:
        _LOGGER.debug(
            "better_thermostat %s: %s - calibration offset model fix: %s to %s",
            self.device_name,
            entity_id,
            calibration_offset,
            _new_offset,
        )

    return _new_offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Apply model-specific setpoint calibration fix.

    Delegates to the loaded model quirks module for any adjustments to the
    requested setpoint temperature.
    """

    _new_temperature = _quirks(self, entity_id).fix_target_temperature_calibration(
        self, entity_id, temperature
    )

    if temperature != _new_temperature:
        _LOGGER.debug(
            "better_thermostat %s: %s - temperature offset model fix: %s to %s",
            self.device_name,
            entity_id,
            temperature,
            _new_temperature,
        )

    return _new_temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: HVACMode | str
) -> bool:
    """Invoke model-specific HVAC mode override, if implemented.

    Returns the model-quirks module's response (True if handled).
    """
    return await _quirks(self, entity_id).override_set_hvac_mode(
        self, entity_id, hvac_mode
    )


async def override_set_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Invoke model-specific temperature override, if implemented.

    Returns the model-quirks module's response (True if handled).
    """
    return await _quirks(self, entity_id).override_set_temperature(
        self, entity_id, temperature
    )


async def initial_tweak(self: ModelFixHost, entity_id: str) -> None:
    """Run initial tweaks for the device."""
    quirks: object = self.real_trvs[entity_id].model_quirks
    if isinstance(quirks, InitialTweakQuirk):
        await quirks.initial_tweak(self, entity_id)


def lowest_setpoint(self: ModelFixHost, entity_id: str, min_temp: float) -> float:
    """Return the lowest setpoint Better Thermostat writes to a TRV.

    That is the minimum the TRV publishes, unless the model's quirk module
    knows a setpoint there means something else to the device.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        Entity id of the TRV
    min_temp : float
        Minimum setpoint the TRV publishes, in Celsius

    Returns
    -------
    float
        The lowest setpoint to write, in Celsius
    """
    quirks: object = self.real_trvs[entity_id].model_quirks
    if not isinstance(quirks, LowestSetpointQuirk):
        return min_temp
    lowest = quirks.lowest_setpoint(self, entity_id, min_temp)
    if lowest != min_temp:
        _LOGGER.debug(
            "better_thermostat %s: %s - lowest setpoint model fix: %s to %s",
            self.device_name,
            entity_id,
            min_temp,
            lowest,
        )
    return lowest
