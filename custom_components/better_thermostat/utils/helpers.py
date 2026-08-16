"""Helper functions for the Better Thermostat component."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
import logging
import math
import re
from typing import Any, NamedTuple

from homeassistant.components.climate.const import (
    ATTR_TARGET_TEMP_STEP,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_registry import async_entries_for_config_entry
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.unit_conversion import TemperatureConverter

from custom_components.better_thermostat.utils.const import (
    CONF_HEAT_AUTO_SWAPPED,
    DOMAIN,
    MAX_HEATING_POWER,
    MAX_REASONABLE_TEMPERATURE,
    MIN_HEATING_POWER,
    MIN_REASONABLE_TEMPERATURE,
    VALVE_MIN_BASE,
    VALVE_MIN_OPENING_LARGE_DIFF,
    VALVE_MIN_PROPORTIONAL_SLOPE,
    VALVE_MIN_SMALL_DIFF_THRESHOLD,
    VALVE_MIN_THRESHOLD_TEMP_DIFF,
    CalibrationMode,
)

_LOGGER = logging.getLogger(__name__)


def find_device_entity(
    entity_registry: er.EntityRegistry,
    device_id: str,
    domains: Iterable[str],
    keywords: Iterable[str],
) -> str | None:
    """Return the entity_id of the first matching entity on a device.

    A match is any entity belonging to ``device_id`` whose domain is in
    ``domains`` and whose name, unique_id or object-id contains any of
    ``keywords`` (case-insensitive). Returns ``None`` if nothing matches.
    """
    domains = tuple(domains)
    keywords = tuple(k.lower() for k in keywords)
    for ent in entity_registry.entities.values():
        if ent.device_id != device_id or ent.domain not in domains:
            continue
        name = (getattr(ent, "original_name", "") or "").lower()
        uid = (ent.unique_id or "").lower()
        # Match keywords against the object-id only; the "<domain>." prefix
        # would otherwise let a keyword such as "lock" match every lock-domain
        # entity, not just the intended child-lock one.
        object_id = (ent.entity_id or "").lower().split(".", 1)[-1]

        if (
            any(k in name for k in keywords)
            or any(k in uid for k in keywords)
            or any(k in object_id for k in keywords)
        ):
            return ent.entity_id
    return None


@callback
def async_normalize_bt_entity_ids(
    hass: HomeAssistant, entry: ConfigEntry, domain: str
) -> None:
    """Rename stale BT registry entries so their entity_id tracks the name.

    HA's entity registry reuses the existing entry on reload (unique id ==
    config entry id), so the entity_id is frozen at first creation while only
    the friendly name follows the device. Blueprints/automations that reference
    ``<domain>.bt_<room>`` then miss. Rename each existing entry to the id HA
    would generate from the current name, before the platform re-adds the
    entities (which reuse the now-correct id).

    For the climate entity the device does not exist yet at this point in
    setup, so the desired id is derived directly from the configured name.
    For the auxiliary platforms the device already exists (climate set it up
    first), so HA's own ``async_regenerate_entity_id`` is used.
    """
    registry = er.async_get(hass)
    # The registry is populated lazily on first load; with a mocked hass
    # (unit tests) it is an unloaded shell without ``.entities``, so there is
    # nothing to rename.
    if not hasattr(registry, "entities"):
        return
    for reg_entry in registry.entities.get_entries_for_config_entry_id(entry.entry_id):
        if reg_entry.platform != DOMAIN or reg_entry.domain != domain:
            continue
        if domain == Platform.CLIMATE:
            object_id = slugify(entry.data.get(CONF_NAME) or "better_thermostat")
            desired = registry.async_get_available_entity_id(
                domain, object_id, current_entity_id=reg_entry.entity_id
            )
        else:
            desired = registry.async_regenerate_entity_id(reg_entry)
        if desired == reg_entry.entity_id:
            continue
        try:
            registry.async_update_entity(reg_entry.entity_id, new_entity_id=desired)
        except ValueError as err:
            _LOGGER.warning(
                "better_thermostat %s: could not rename %s to %s: %s",
                entry.data.get(CONF_NAME),
                reg_entry.entity_id,
                desired,
                err,
            )


def normalize_calibration_mode(
    mode: CalibrationMode | str | None,
) -> CalibrationMode | str | None:
    """Normalize a calibration_mode field from TRV advanced data."""

    # Backwards compatibility: older configs stored numeric calibration modes
    # (e.g. 0 for DEFAULT). Only map known values.
    if isinstance(mode, (int, float)):
        try:
            numeric = int(mode)
        except TypeError, ValueError:
            numeric = None
        if numeric == 0:
            return CalibrationMode.DEFAULT
        return None

    if isinstance(mode, CalibrationMode):
        return mode
    if isinstance(mode, str):
        value = mode.strip().lower()
        try:
            return CalibrationMode(value)
        except ValueError:
            return value
    return None


def is_calibration_mode(
    mode: CalibrationMode | str | None, expected: CalibrationMode
) -> bool:
    """Return True if ``mode`` is the expected CalibrationMode."""

    normalized = normalize_calibration_mode(mode)
    if isinstance(normalized, CalibrationMode):
        return normalized == expected
    if isinstance(normalized, str):
        return normalized == expected.value
    return False


def entity_uses_calibration_mode(bt, entity_id: str, expected: CalibrationMode) -> bool:
    """Check if the given TRV has ``expected`` calibration mode configured."""

    try:
        _trv = bt.real_trvs.get(entity_id)
        advanced = (_trv.advanced if _trv is not None else {}) or {}
    except AttributeError:
        return False
    mode = advanced.get("calibration_mode")
    return is_calibration_mode(mode, expected)


def entity_uses_mpc_calibration(bt, entity_id: str) -> bool:
    """Check if entity uses MPC calibration mode."""
    return entity_uses_calibration_mode(bt, entity_id, CalibrationMode.MPC_CALIBRATION)


def get_hvac_bt_mode(self, mode: str) -> str:
    """Return the main HVAC mode mapping for the Better Thermostat.

    The function handles simple mapping from HVACMode.HEAT to configured
    internal modes used by the integration.
    """
    if mode == HVACMode.HEAT:
        mode = self.map_on_hvac_mode
    elif mode == HVACMode.HEAT_COOL:
        mode = HVACMode.HEAT
    return mode


def normalize_hvac_mode(value: HVACMode | str) -> HVACMode | str:
    """Normalize a hvac_mode value to a proper HVACMode enum when possible.

    Accepts
    -------
    value : HVACMode | str
        - HVACMode enum: returned as-is
        - Strings like 'heat', 'off', 'heat_cool', 'auto', 'dry', 'fan_only'
        - Strings like 'HVACMode.HEAT' (will be converted to HVACMode.HEAT)

    Returns
    -------
    HVACMode | str
        HVACMode if recognized, otherwise the lowercased string without prefix.
    """
    if isinstance(value, HVACMode):
        return value
    if isinstance(value, str):
        raw = value.strip()
        # Strip enum-like prefix if present
        if raw.lower().startswith("hvacmode."):
            raw = raw.split(".", 1)[1]
        key = raw.lower()
        mapping = {
            "off": HVACMode.OFF,
            "heat": HVACMode.HEAT,
            "cool": HVACMode.COOL,
            "heat_cool": HVACMode.HEAT_COOL,
            "auto": HVACMode.AUTO,
            "dry": HVACMode.DRY,
            "fan_only": HVACMode.FAN_ONLY,
        }
        return mapping.get(key, key)
    return value


def _device_offers_mode(trv_modes: Iterable[str], hvac_mode: str) -> bool:
    """Whether a device's reported mode list contains a given HVAC mode.

    Both sides are normalized so a list carrying ``HVACMode`` members,
    plain strings or ``"HVACMode.HEAT"`` spellings compares equal.

    Parameters
    ----------
    trv_modes : Iterable[str]
        HVAC modes the device reports.
    hvac_mode : str
        HVAC mode to look for.

    Returns
    -------
    bool
        ``True`` when the device offers ``hvac_mode``.
    """
    target = normalize_hvac_mode(hvac_mode)
    return any(normalize_hvac_mode(mode) == target for mode in trv_modes)


def offered_mode_signature(trv_modes: Iterable[Any] | None) -> frozenset[str]:
    """Reduce a reported mode list to the set of modes it offers.

    Parameters
    ----------
    trv_modes : Iterable[Any] | None
        HVAC modes a device reports, in any spelling.

    Returns
    -------
    frozenset[str]
        Normalized mode names, so two lists that differ only in order, in
        capitalization or in ``HVACMode`` versus ``"HVACMode.HEAT"``
        spelling reduce to the same value.
    """
    if not trv_modes:
        return frozenset()
    return frozenset(str(normalize_hvac_mode(mode)) for mode in trv_modes)


def adopt_reported_hvac_modes(trv, reported_modes: Any) -> None:
    """Cache the HVAC modes a device reports on its state.

    An absent or empty list keeps the cached one: it means the device
    published no capabilities in this event, not that it lost them. The
    modes already annunciated as not offered are forgotten only when the
    offered set really changes, so a republication in a different order or
    a different spelling does not repeat the annunciation.

    Parameters
    ----------
    trv : Trv
        Per-TRV state holding the cached mode list.
    reported_modes : Any
        Value of the device's ``hvac_modes`` attribute.
    """
    if not isinstance(reported_modes, list) or not reported_modes:
        return
    if offered_mode_signature(reported_modes) != offered_mode_signature(trv.hvac_modes):
        trv.unsupported_modes_logged.clear()
    trv.hvac_modes = reported_modes


def _unsupported_mode_hint(trv) -> str:
    """Name the remedy for a device that does not offer the mode BT wants.

    The heat auto swapped option is what decides whether BT writes ``heat``
    or ``auto``, so which way to turn it depends on its current setting: a
    device without ``auto`` is only asked for ``auto`` because the option is
    already on.

    Parameters
    ----------
    trv : Trv
        Per-TRV state carrying the advanced configuration.

    Returns
    -------
    str
        A sentence naming the setting that resolves the situation.
    """
    if (trv.advanced or {}).get(CONF_HEAT_AUTO_SWAPPED, False):
        return (
            "Disable the heat auto swapped option unless 'auto' really is this "
            "device's heating mode."
        )
    return (
        "Switch the device to its heating mode, or enable the heat auto swapped "
        "option if 'auto' means 'heat' on this device."
    )


def _clamp_to_offered_mode(
    self, trv, entity_id, hvac_mode: str, inbound: bool, fallback: str | None = None
) -> str | None:
    """Drop an outbound HVAC mode the device does not offer.

    Writing a mode outside the device's own list makes
    ``climate.set_hvac_mode`` fail, which aborts the whole control cycle
    and leaves the setpoint unwritten as well. Returning ``None`` means
    "write no mode this cycle", so the setpoint still reaches the device.
    ``OFF`` is exempt because the no-off handling downstream substitutes
    the minimum temperature for it.

    A ``fallback`` is the mode a quirk translation started from. When the
    translated mode is unwritable but the original one is offered, writing
    the original still puts the device into the state BT asked for.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    trv : Trv
        Per-TRV state of the device the mode is written to.
    entity_id : str
        Entity id of that TRV, used for the log message.
    hvac_mode : str
        HVAC mode about to be written.
    inbound : bool
        True if the mode is coming from the device, False if it is coming from the HA.
    fallback : str | None
        Mode to write instead when the device offers it but not
        ``hvac_mode``.

    Returns
    -------
    str | None
        ``hvac_mode`` when it may be written, ``fallback`` when only that
        one is offered, ``None`` when the device offers neither.
    """
    trv_modes = trv.hvac_modes
    if inbound or not trv_modes:
        return hvac_mode
    if normalize_hvac_mode(hvac_mode) == HVACMode.OFF or _device_offers_mode(
        trv_modes, hvac_mode
    ):
        return hvac_mode

    _mode_key = str(normalize_hvac_mode(hvac_mode))

    if fallback is not None and _device_offers_mode(trv_modes, fallback):
        _fallback_key = f"{_mode_key}->{normalize_hvac_mode(fallback)}"
        if _fallback_key not in trv.unsupported_modes_logged:
            trv.unsupported_modes_logged.add(_fallback_key)
            _LOGGER.warning(
                "better_thermostat %s: %s does not offer HVAC mode %s, it offers %s. "
                "Writing %s instead. %s",
                self.device_name,
                entity_id,
                hvac_mode,
                trv_modes,
                fallback,
                _unsupported_mode_hint(trv),
            )
        return fallback

    if _mode_key not in trv.unsupported_modes_logged:
        trv.unsupported_modes_logged.add(_mode_key)
        _LOGGER.error(
            "better_thermostat %s: %s does not offer HVAC mode %s, it offers %s. "
            "The device mode is left untouched and only the setpoint is written. %s",
            self.device_name,
            entity_id,
            hvac_mode,
            trv_modes,
            _unsupported_mode_hint(trv),
        )
    return None


def mode_remap(self, entity_id, hvac_mode: str, inbound: bool = False) -> str | None:
    """Remap HVAC mode to correct mode if nessesary.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id :
            entity id of the TRV whose mode is being remapped
    hvac_mode : str
            HVAC mode to be remapped

    inbound : bool
            True if the mode is coming from the device, False if it is coming from the HA.

    Returns
    -------
    str | None
            remapped mode according to device's quirks, or None for an
            outbound mode the device does not offer, meaning the device's
            mode is left untouched
    """
    trv = self.real_trvs.get(entity_id)
    if trv is None:
        return hvac_mode

    _heat_auto_swapped = (trv.advanced or {}).get(CONF_HEAT_AUTO_SWAPPED, False)

    if _heat_auto_swapped:
        if hvac_mode == HVACMode.HEAT and not inbound:
            return _clamp_to_offered_mode(
                self, trv, entity_id, HVACMode.AUTO, inbound, fallback=HVACMode.HEAT
            )
        if hvac_mode == HVACMode.AUTO and inbound:
            return HVACMode.HEAT
        return _clamp_to_offered_mode(self, trv, entity_id, hvac_mode, inbound)

    trv_modes = trv.hvac_modes
    if not trv_modes:
        return hvac_mode
    if HVACMode.HEAT not in trv_modes and HVACMode.HEAT_COOL in trv_modes:
        # entity only supports HEAT_COOL, but not HEAT - need to translate
        if not inbound and hvac_mode == HVACMode.HEAT:
            return HVACMode.HEAT_COOL
        if inbound and hvac_mode == HVACMode.HEAT_COOL:
            return HVACMode.HEAT
    if HVACMode.HEAT_COOL not in trv_modes and HVACMode.HEAT in trv_modes:
        # entity only supports HEAT, but not HEAT_COOL - need to translate
        if not inbound and hvac_mode == HVACMode.HEAT_COOL:
            return HVACMode.HEAT
        if inbound and hvac_mode == HVACMode.HEAT:
            return HVACMode.HEAT_COOL

    if hvac_mode == HVACMode.AUTO:
        _LOGGER.error(
            "better_thermostat %s: %s HVAC mode %s is not supported by this device, "
            "is it possible that you forgot to set the heat auto swapped option?",
            self.device_name,
            entity_id,
            hvac_mode,
        )
        return HVACMode.OFF

    return _clamp_to_offered_mode(self, trv, entity_id, hvac_mode, inbound)


def group_all_members_off(self) -> bool:
    """Whether every available group member is effectively off.

    Gates group-wide "switch off" adoptions so a single valve entering frost
    protection (reported as ``off`` in HA) or a single ``no_off_system_mode``
    valve dropping to its minimum temperature cannot turn the whole room off.
    Single-TRV instances always agree, preserving historical behavior.

    A member counts as off when its reported HVAC state is ``off`` or, for a
    ``no_off_system_mode`` device (which never reports ``off``), when its
    current setpoint has dropped to that device's minimum temperature. The
    setpoint is read via :func:`attr_to_celsius` (with ``target_temp_low`` as
    fallback attribute) so it is compared in Celsius, like ``min_temp``.
    """
    trv_ids = list(self.real_trvs.keys())
    if len(trv_ids) <= 1:
        return True

    saw_member = False
    for entity_id in trv_ids:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            continue
        saw_member = True
        if state.state == HVACMode.OFF:
            continue
        member = self.real_trvs.get(entity_id)
        if member is not None and (member.advanced or {}).get(
            "no_off_system_mode", False
        ):
            setpoint_key = (
                "temperature"
                if "temperature" in state.attributes
                else "target_temp_low"
            )
            setpoint = attr_to_celsius(
                self, state, setpoint_key, None, "group_all_members_off()"
            )
            if (
                setpoint is not None
                and member.min_temp is not None
                and setpoint <= member.min_temp
            ):
                continue
        return False
    return saw_member


def heating_power_valve_position(self, entity_id):
    """Compute an expected valve position from the heating power.

    Given the global `heating_power` estimate and the target/current
    temperature, a heuristic mapping to valve opening percentage is
    returned (between 0.0 and 1.0).

    Examples (resulting valve_pos for a given temp_diff and heating_power):

    | temp_diff | hp=0.02 | hp=0.01 | hp=0.005 |
    |-----------|---------|---------|----------|
    | 0.1       | 0.0871  | 0.1678  | 0.3232   |
    | 0.2       | 0.1678  | 0.3232  | 0.6227   |
    | 0.3       | 0.2462  | 0.4744  | 0.9139   |
    | 0.4       | 0.3232  | 0.6227  | 1.0000   |
    | 0.5       | 0.3992  | 0.7691  | 1.0000   |
    """
    _temp_diff = float(float(self.bt_target_temp) - float(self.cur_temp))

    # Guard against negative temp_diff (room warmer than target)
    # This can occur in TRV override edge case when temperature rises
    # above target but TRV still reports heating (delayed update)
    if _temp_diff <= 0:
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: {entity_id} "
            f"cur_temp >= target_temp ({self.cur_temp} >= {self.bt_target_temp}), "
            f"setting valve to 0%"
        )
        return 0.0

    # Ensure heating_power is bounded to realistic values
    # This protects against incorrectly learned high values
    heating_power = max(
        MIN_HEATING_POWER, min(MAX_HEATING_POWER, float(self.heating_power))
    )

    # Original formula with improved robustness
    a = 0.019
    b = 0.946
    valve_pos = a * (_temp_diff / heating_power) ** b

    # Apply minimum valve position when heating is actively needed
    # If temp_diff > threshold, ensure minimum valve opening
    # This prevents the system from getting stuck with too-low valve positions
    if _temp_diff > VALVE_MIN_THRESHOLD_TEMP_DIFF:
        valve_pos = max(VALVE_MIN_OPENING_LARGE_DIFF, valve_pos)
    elif _temp_diff >= VALVE_MIN_SMALL_DIFF_THRESHOLD:
        # For smaller differences, use a proportional minimum
        min_valve = (
            VALVE_MIN_BASE
            + (_temp_diff - VALVE_MIN_SMALL_DIFF_THRESHOLD)
            * VALVE_MIN_PROPORTIONAL_SLOPE
        )
        valve_pos = max(min_valve, valve_pos)

    # Bound to valid range
    valve_pos = max(0.0, min(1.0, valve_pos))

    _LOGGER.debug(
        "better_thermostat %s: %s / heating_power_valve_position - temp diff: %s - heating power: %s (bounded) - expected valve position: %s%%",
        self.device_name,
        entity_id,
        round(_temp_diff, 1),
        round(heating_power, 4),
        round(valve_pos * 100),
    )
    return valve_pos


def is_reasonable_temperature(value: float | None) -> bool:
    """Return ``True`` iff ``value`` is a plausible indoor temperature in °C.

    Rejects ``None`` and any value outside ``MIN_REASONABLE_TEMPERATURE`` ..
    ``MAX_REASONABLE_TEMPERATURE``. Out-of-range values are typically
    marker / garbage readings produced by upstream integrations (for
    example, AVM Fritz!DECT exposes 126.5 / 127 °C when the thermostat is
    in OFF / ON mode).
    """
    if value is None:
        return False
    return MIN_REASONABLE_TEMPERATURE <= value <= MAX_REASONABLE_TEMPERATURE


def convert_to_float(
    value: str | int | float | None, instance_name: str, context: str
) -> float | None:
    """Convert value to float or print error message.

    Non-finite readings fail the conversion and yield None: the step rounding
    goes through an integer number of steps, so ``inf`` raises ``OverflowError``
    and ``nan`` raises ``ValueError``.

    Parameters
    ----------
    value : str | int | float | None
            the value to convert to float
    instance_name : str
            the name of the instance thermostat
    context : str
            the name of the function which is using this, for printing an error message

    Returns
    -------
    float | None
            the converted value, or None if conversion failed
    """
    if value is None or value == "None":
        return None
    try:
        # Use 0.01 step (2 decimal places) to preserve sensor precision.
        # Rounding to 0.1 can turn 19.97 into 20.0, leading to incorrect
        # HVAC action decisions.
        return round_by_step(float(value), 0.01)
    except ValueError, TypeError, AttributeError, KeyError, OverflowError:
        _LOGGER.debug(
            "better thermostat %s: Could not convert '%s' to float in %s",
            instance_name,
            value,
            context,
        )
        return None


def convert_to_float_celsius(
    value: str | int | float | None,
    instance_name: str,
    context: str,
    unit_of_measurement: str | None = None,
) -> float | None:
    """Convert value to float and ensure it is in Celsius.

    If *unit_of_measurement* indicates Fahrenheit the value is converted to
    Celsius after the initial float conversion.

    Parameters
    ----------
    value : str | int | float | None
            the value to convert
    instance_name : str
            thermostat instance name (for logging)
    context : str
            calling function context (for logging)
    unit_of_measurement : str | None
            the unit of the incoming value (e.g. ``UnitOfTemperature.FAHRENHEIT``)

    Returns
    -------
    float | None
            the value in Celsius, or None if conversion failed
    """
    result = convert_to_float(value, instance_name, context)
    if result is not None and unit_of_measurement == UnitOfTemperature.FAHRENHEIT:
        result = TemperatureConverter.convert(
            result, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
        result = round(result, 2)
    return result


def state_temperature_unit(
    attributes: Mapping[str, object] | None, system_unit: str | None
) -> str | None:
    """Resolve the temperature unit of a state's attributes.

    ``climate`` entities report their temperatures in the Home Assistant system
    unit and do not expose a ``temperature_unit`` / ``unit_of_measurement``
    attribute. ``system_unit`` (``hass.config.units.temperature_unit``) is used
    as the fallback so the values are interpreted in the right unit. Sensors
    that carry an explicit ``unit_of_measurement`` keep it.

    Parameters
    ----------
    attributes : Mapping[str, object] | None
            the state attributes to inspect, or None when the state is missing
    system_unit : str | None
            the configured system temperature unit, used as fallback

    Returns
    -------
    str | None
            the resolved temperature unit, or ``system_unit`` when no explicit
            unit attribute is present
    """
    if not attributes:
        return system_unit
    for attr in ("temperature_unit", "unit_of_measurement"):
        unit = attributes.get(attr)
        if isinstance(unit, str):
            return unit
    return system_unit


def celsius_to_system_temperature(hass: HomeAssistant, temperature: float) -> float:
    """Convert a Celsius temperature to the Home Assistant system unit.

    The outbound counterpart to :func:`attr_to_celsius`: Better Thermostat
    works in Celsius internally, while ``climate`` service payloads must
    carry the system unit. On Fahrenheit installs the value is converted
    and rounded to one decimal; otherwise it is returned unchanged.

    Parameters
    ----------
    hass : HomeAssistant
            the Home Assistant instance supplying the configured system unit
    temperature : float
            the temperature in Celsius

    Returns
    -------
    float
            the temperature expressed in the system unit
    """
    if hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT:
        return round(
            TemperatureConverter.convert(
                temperature, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
            ),
            1,
        )
    return temperature


def supports_temperature_range(state: State | None) -> bool:
    """Check whether a climate state advertises TARGET_TEMPERATURE_RANGE.

    Centralizes the supported_features bitmask check so write paths
    (model quirks) and read/confirmation paths (control_trv,
    check_target_temperature) stay in sync if the detection logic
    ever needs to change.

    Parameters
    ----------
    state : State | None
            the climate entity state to inspect

    Returns
    -------
    bool
            True if the range feature bit is set, False otherwise
            (including when state is None)
    """
    if state is None:
        return False
    supported_features = state.attributes.get("supported_features", 0)
    return bool(supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)


def supports_single_target_temperature(state: State | None) -> bool:
    """Check whether a climate state advertises TARGET_TEMPERATURE.

    The counterpart to :func:`supports_temperature_range`. Home Assistant
    rejects a ``set_temperature`` call carrying ``temperature`` when the entity
    does not advertise this feature, so write paths need both bits to pick the
    payload a device accepts.

    Parameters
    ----------
    state : State | None
            the climate entity state to inspect

    Returns
    -------
    bool
            True if the single-setpoint feature bit is set, False otherwise
            (including when state is None)
    """
    if state is None:
        return False
    supported_features = state.attributes.get("supported_features", 0)
    return bool(supported_features & ClimateEntityFeature.TARGET_TEMPERATURE)


# The attribute a device publishes its setpoint under depends on the role it
# plays: BT drives a TRV towards the lower bound of a range and a cooler
# towards the upper one. The single-setpoint key comes first in both cases,
# because a device that offers it is driven through it.
TRV_SETPOINT_KEYS = ("temperature", "target_temp_low")
COOLER_SETPOINT_KEYS = ("temperature", "target_temp_high")


class InboundSetpoint(NamedTuple):
    """A setpoint reported by a controlled device, prepared for adoption.

    Attributes
    ----------
    raw : float
            the reported value in °C, before range clamping
    value : float
            the reported value in °C after clamping into BT's range
    clamped : bool
            whether clamping changed the value
    is_echo : bool
            whether the report is BT's own write coming back
    """

    raw: float
    value: float
    clamped: bool
    is_echo: bool


def read_setpoint_celsius(
    self, state: State | None, keys: tuple[str, ...], log_source: str
) -> float | None:
    """Read the first usable setpoint attribute from a state and return it in °C.

    A climate entity that supports both a single target and a target range
    publishes the key it does not currently drive as None, so a
    present-but-empty attribute must not stop the next key from being read.

    Parameters
    ----------
    self :
            the Better Thermostat instance, supplying ``hass`` and ``device_name``
    state : State | None
            the climate entity state to inspect, or None when unavailable
    keys : tuple[str, ...]
            the attribute names to try, in order of precedence
    log_source : str
            caller name, forwarded to attr_to_celsius for logging context

    Returns
    -------
    float | None
            the setpoint in Celsius, or None when no key holds a usable value
    """
    if state is None:
        return None
    for key in keys:
        if state.attributes.get(key) is None:
            continue
        setpoint = attr_to_celsius(self, state, key, None, log_source)
        if setpoint is not None:
            return setpoint
    return None


def normalize_step(value: float | int | str | None, fallback: float = 0.5) -> float:
    """Coerce a reported temperature step to a usable positive float.

    NaN and infinity survive ``float()`` and pass a ``<= 0`` test, so they are
    rejected explicitly: a NaN step makes every echo comparison false, an
    infinite one makes them all true.
    """
    if value is None:
        return fallback
    try:
        step = float(value)
    except TypeError, ValueError:
        return fallback
    if not math.isfinite(step) or step <= 0:
        return fallback
    return step


def reported_setpoint_step_celsius(
    state: State | None, device_name: str, system_unit: str | None, log_source: str
) -> float | None:
    """Return the setpoint step a state publishes, as a Celsius delta.

    This is the unit rule and nothing else. A device publishes its step in its
    own unit, so a Fahrenheit step is scaled as a temperature difference (5/9)
    rather than run through the absolute Fahrenheit-to-Celsius conversion. The
    unit is resolved from the same attributes mapping the step was read from,
    because that is the device the step belongs to.

    ``None`` means the state publishes no convertible step. A missing attribute
    and an attribute holding ``None`` are the same case and are not logged; a
    value that fails conversion is logged by ``convert_to_float``. Sign and
    magnitude are not judged and no fallback is applied, so a caller that needs
    a usable positive step supplies its own.

    Parameters
    ----------
    state : State | None
            the device state carrying the reported ``target_temp_step``, or
            None when the state is missing
    device_name : str
            the Better Thermostat instance name, for logging context
    system_unit : str | None
            the configured system temperature unit, used when the attributes
            name no unit of their own
    log_source : str
            caller name, forwarded to convert_to_float for logging context

    Returns
    -------
    float | None
            the reported step as a Celsius delta, or None when the state
            publishes no convertible step
    """
    attributes = state.attributes if state is not None else {}
    raw_step = attributes.get(ATTR_TARGET_TEMP_STEP)
    if raw_step is None:
        return None
    # The raw value is stringified before conversion, which is what makes a
    # boolean fail: ``float(True)`` is 1.0, while ``float("True")`` raises.
    step = convert_to_float(str(raw_step), device_name, log_source)
    if step is None:
        return None
    if state_temperature_unit(attributes, system_unit) == UnitOfTemperature.FAHRENHEIT:
        return round(step * 5.0 / 9.0, 4)
    return step


def device_setpoint_step(self, state: State, log_source: str) -> float:
    """Return a controlled device's setpoint step as a °C delta.

    The step belongs to the device that reports it and carries that device's
    unit, so a Fahrenheit reading is converted to a Celsius delta before it can
    be compared with a Celsius setpoint. A device that publishes no usable step
    falls back to Better Thermostat's own step.

    Parameters
    ----------
    self :
            the Better Thermostat instance, supplying ``hass``, ``device_name``
            and the configured step
    state : State
            the device state carrying the reported ``target_temp_step``
    log_source : str
            caller name, forwarded for logging context

    Returns
    -------
    float
            the device's setpoint step as a positive Celsius delta
    """
    step = reported_setpoint_step_celsius(
        state, self.device_name, self.hass.config.units.temperature_unit, log_source
    )
    if step is None or step <= 0:
        return normalize_step(self.bt_target_temp_step)
    return step


def setpoint_echo_window(step: float) -> float:
    """Return the distance below which a setpoint difference is grid noise.

    A reported value carries the rounding of ``convert_to_float``'s 0.01 grid
    while the device's step and the values BT wrote sit on the device's own
    grid, so one full step of movement can land a hair below ``step``. The
    window shrinks by that noise and stays positive for a tiny step.

    Parameters
    ----------
    step : float
            the device's setpoint step in °C

    Returns
    -------
    float
            the largest difference that still counts as the same setpoint
    """
    return max(step - SETPOINT_MATCH_TOLERANCE, SETPOINT_MATCH_TOLERANCE)


def resolve_inbound_setpoint(
    self,
    state: State | None,
    *,
    keys: tuple[str, ...],
    known_values: tuple[float | None, ...],
    step: float,
    log_source: str,
) -> InboundSetpoint | None:
    """Prepare a setpoint reported by a controlled device for adoption.

    The shared adoption gate for setpoints BT does not own: it resolves the
    value to °C via :func:`read_setpoint_celsius`, clamps it into BT's range,
    and decides whether it is BT's own write coming back. A device settles a
    written value on its own grid and republishes it, sometimes from a later
    poll whose context is not BT's, so anything within one device step of a
    value BT wrote is an echo. User input moves a setpoint by at least one
    step. Answering rather than logging keeps the caller free to decide
    whether a clamp is worth reporting.

    A write of BT's own comes back in one of two shapes, and both are the same
    question. The device republishes the written value verbatim, which the
    reported value answers; or the write was itself the product of a clamp, and
    the device's out-of-range report maps back onto that written value, which
    the clamped value answers. Either match means the device is carrying BT's
    own write, so a report is an echo when either comparison lands inside the
    window.

    Parameters
    ----------
    self :
            the Better Thermostat instance, supplying ``hass``, ``device_name``
            and the configured range
    state : State | None
            the device state carrying the reported setpoint
    keys : tuple[str, ...]
            the attribute names to try, in order of precedence
    known_values : tuple[float | None, ...]
            the values BT itself wrote, in °C; non-numeric entries are ignored
    step : float
            the device's setpoint step as a Celsius delta; a step read from a
            device attribute carries that device's unit and has to be converted
            before it is passed
    log_source : str
            caller name, forwarded for logging context

    Returns
    -------
    InboundSetpoint | None
            the resolved setpoint, or None when the state holds no usable value
    """
    raw = read_setpoint_celsius(self, state, keys, log_source)
    if raw is None:
        return None

    # A bound stays None until a child entity reports one, so each side is
    # enforced only once it is known. Non-overlapping heater and cooler ranges
    # leave bt_min_temp above bt_max_temp, so the two bounds are applied in
    # sequence rather than exclusively and the upper one decides.
    value = raw
    clamped = False
    if self.bt_min_temp is not None and value < self.bt_min_temp:
        value = self.bt_min_temp
        clamped = True
    if self.bt_max_temp is not None and self.bt_max_temp < value:
        value = self.bt_max_temp
        clamped = True

    echo_window = setpoint_echo_window(step)
    is_echo = any(
        isinstance(known, (int, float))
        and (abs(raw - known) < echo_window or abs(value - known) < echo_window)
        for known in known_values
    )
    return InboundSetpoint(raw=raw, value=value, clamped=clamped, is_echo=is_echo)


def state_says_nothing(state: State | None) -> bool:
    """Answer whether a state carries no statement about its own device.

    A missing state, ``unavailable`` and ``unknown`` all leave the device
    unaccounted for, while attributes can still be present on every one of
    them: a climate entity publishes its full attribute set while it reports
    ``unknown``, and one that writes the state machine directly keeps the
    attributes it last set. A reading taken from such a state is retained
    rather than reported, so a caller that would act on it as a live value has
    to decline it.

    Parameters
    ----------
    state : State | None
            the device state to inspect

    Returns
    -------
    bool
            True when the state is missing, ``unavailable`` or ``unknown``
    """
    return state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def resolve_state_change_event(
    self, event, device_label: str
) -> tuple[State, State, str] | None:
    """Return the states of a device event worth acting on, or None.

    Shared prologue of the device event handlers: an event is actionable when
    it carries both states, both are States with attributes, it names an
    entity, and it was not caused by BT's own service call — those carry
    ``self.context``.

    Parameters
    ----------
    self :
            the Better Thermostat instance, supplying ``context`` and ``device_name``
    event :
            the state change event to inspect
    device_label : str
            role of the device in log messages, e.g. ``"TRV"`` or ``"Cooler"``

    Returns
    -------
    tuple[State, State, str] | None
            (old_state, new_state, entity_id) when actionable, with the entity
            id guaranteed to be a string, else None
    """
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    entity_id = event.data.get("entity_id")

    if new_state is None or old_state is None:
        _LOGGER.debug(
            "better_thermostat %s: %s %s update contained not all necessary data "
            "for processing, skipping",
            self.device_name,
            device_label,
            entity_id,
        )
        return None

    if not isinstance(new_state, State) or not isinstance(old_state, State):
        _LOGGER.debug(
            "better_thermostat %s: %s %s update contained not a State, skipping",
            self.device_name,
            device_label,
            entity_id,
        )
        return None

    if new_state.attributes is None:
        _LOGGER.debug(
            "better_thermostat %s: %s %s update had no attributes, skipping",
            self.device_name,
            device_label,
            entity_id,
        )
        return None

    if not isinstance(entity_id, str):
        _LOGGER.debug(
            "better_thermostat %s: %s update without an entity id, skipping",
            self.device_name,
            device_label,
        )
        return None

    if self.context == event.context:
        return None

    return old_state, new_state, entity_id


def attr_to_celsius(
    self,
    state: State | None,
    key: str,
    default: str | int | float | None = None,
    context: str = "",
) -> float | None:
    """Read a temperature attribute from a foreign state and return it in °C.

    The single inbound boundary for foreign temperatures: it resolves the source
    unit via :func:`state_temperature_unit` (system-unit fallback, since
    ``climate`` entities expose no unit attribute) and converts to the Celsius
    Better Thermostat works in internally.

    Parameters
    ----------
    self :
            the Better Thermostat instance, supplying ``hass`` and ``device_name``
    state : State | None
            the source state to read from, or None when it is unavailable
    key : str
            the attribute name holding the temperature (e.g. ``"temperature"``)
    default : str | int | float | None
            value used when the attribute is missing
    context : str
            calling context, forwarded for logging

    Returns
    -------
    float | None
            the temperature in Celsius, or None if conversion failed

    See Also
    --------
    convert_to_float_celsius : performs the unit conversion
    state_temperature_unit : resolves the source unit
    """
    attributes = state.attributes if state is not None else {}
    return convert_to_float_celsius(
        str(attributes.get(key, default)),
        self.device_name,
        context,
        unit_of_measurement=state_temperature_unit(
            attributes, self.hass.config.units.temperature_unit
        ),
    )


def get_current_set_temperatures(
    self, state: State | None, log_source: str
) -> set[float]:
    """Read the single-setpoint and range-low temperatures from a climate state.

    Centralizes the "read temperature and target_temp_low, then build a
    non-None set" logic shared by control_trv()'s write-skip check and
    check_target_temperature()'s confirmation polling. A device's
    supported_features range bit doesn't guarantee its setpoint is
    actually driven via target_temp_low -- only a model-specific quirk
    makes that true, and an un-quirked range-capable device may still
    only ever be written via plain "temperature" through the generic
    adapter. Returning both non-None values as a set lets callers accept
    a match on either, correct regardless of which path performed the
    write.

    Parameters
    ----------
    self :
            the Better Thermostat instance, supplying ``hass`` and ``device_name``
    state : State | None
            the climate entity state to inspect, or None when unavailable
    log_source : str
            caller name, forwarded to attr_to_celsius for logging context

    Returns
    -------
    set[float]
            the set of non-None current setpoints (temperature and,
            when range mode is supported, target_temp_low)
    """
    single = attr_to_celsius(self, state, "temperature", None, log_source)
    range_low = (
        attr_to_celsius(self, state, "target_temp_low", None, log_source)
        if supports_temperature_range(state)
        else None
    )
    return {v for v in (single, range_low) if v is not None}


# Written setpoints are rounded on the device step grid (round_by_step with
# e.g. 0.1, or a Fahrenheit step converted to Celsius), while read-back values
# pass through convert_to_float's 0.01 grid. The two grids are not
# binary-float compatible, so exact equality between a written and a read-back
# setpoint is unreliable. 0.01 covers both the float-grid noise (~1e-14) and
# the worst legitimate write-vs-readback divergence (half the 0.01 read grid,
# 0.005), while staying far below the smallest distinguishable setpoint step
# (0.1) — it can never conflate two distinct setpoints.
SETPOINT_MATCH_TOLERANCE = 0.01


def matches_any_setpoint(
    value: float | None,
    setpoints: set[float],
    tolerance: float = SETPOINT_MATCH_TOLERANCE,
) -> bool:
    """Check whether a setpoint matches any element of a set within a tolerance.

    Written setpoints (rounded on the device step grid) and read-back
    setpoints (rounded on convert_to_float's 0.01 grid) land on different
    binary-float grids, so callers compare them with this tolerance-based
    check instead of exact set membership.

    Parameters
    ----------
    value : float | None
            the setpoint to look for, or None when no value is available
    setpoints : set[float]
            the setpoints to compare against
    tolerance : float
            maximum absolute difference still considered a match
            (default: SETPOINT_MATCH_TOLERANCE)

    Returns
    -------
    bool
            True if value is not None and lies within tolerance of any
            element of setpoints, False otherwise (including for an
            empty set)
    """
    if value is None:
        return False
    return any(abs(value - setpoint) <= tolerance for setpoint in setpoints)


class rounding:
    """Rounding helpers for stable step-based rounding.

    Provides minor offsets to avoid floating point rounding artifacts when
    converting values to integer steps.
    """

    @staticmethod
    def up(x: float) -> float:
        """Round up with a tiny epsilon to avoid FP artifacts."""
        return math.ceil(x - 0.0001)

    @staticmethod
    def down(x: float) -> float:
        """Round down with a tiny epsilon to avoid FP artifacts."""
        return math.floor(x + 0.0001)

    @staticmethod
    def nearest(x: float) -> float:
        """Round to nearest step with a small epsilon to avoid up-rounding."""
        return round(x - 0.0001)


def round_by_step(
    value: float | None,
    step: float | None,
    f_rounding: Callable[[float], float] = rounding.nearest,
) -> float | None:
    """Round the value based on the allowed decimal 'step' size.

    Parameters
    ----------
    value : float
            the value to round
    step : float
            size of one step
    f_rounding : callable
            rounding function (default: rounding.nearest)

    Returns
    -------
    float
            the rounded value
    """

    if value is None or step is None:
        return None
    # Use default rounding function if none provided
    if f_rounding is None:
        f_rounding = rounding.nearest
    # convert to integer number of steps for rounding, then convert back to decimal
    return f_rounding(value / step) * step


def check_float(potential_float):
    """Check if a string is a float.

    Parameters
    ----------
    potential_float :
            the value to check

    Returns
    -------
    bool
            True if the value is a float, False otherwise.

    """
    try:
        float(potential_float)
        return True
    except ValueError, TypeError:
        return False


def convert_time(time_string):
    """Convert a time string to a datetime object.

    Parameters
    ----------
    time_string :
            a string representing a time

    Returns
    -------
    datetime
            the converted time as a datetime object.
    None
            If the time string is not a valid time.
    """
    try:
        _current_time = dt_util.now()
        _get_hours_minutes = datetime.strptime(time_string, "%H:%M")
        return _current_time.replace(
            hour=_get_hours_minutes.hour,
            minute=_get_hours_minutes.minute,
            second=0,
            microsecond=0,
        )
    except ValueError:
        return None


async def find_valve_entity(self, entity_id):
    """Locate a per-TRV valve position helper entity, if available.

    Returns a mapping with the entity_id, whether it appears writable, and the
    detection reason. ``None`` if no related entity could be found.
    """
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        return None

    # Some integrations (notably certain Zigbee stacks) may expose valve helpers
    # under a different Home Assistant device_id than the climate entity.
    # To support these, also match candidates by shared device identifiers.
    dev_reg = None
    base_identifiers: set[tuple[str, str]] = set()
    try:
        dev_reg = dr.async_get(self.hass)
        device_id = getattr(reg_entity, "device_id", None)
        base_device = dev_reg.async_get(device_id) if device_id is not None else None
        base_identifiers = set(getattr(base_device, "identifiers", set()) or set())
    except Exception:
        dev_reg = None
        base_identifiers = set()

    config_entry_id = reg_entity.config_entry_id
    if config_entry_id is None:
        return None
    entity_entries = async_entries_for_config_entry(entity_registry, config_entry_id)
    preferred_domains = {"number", "input_number"}
    readonly_candidate: dict[str, Any] | None = None

    def _device_matches(candidate) -> bool:
        # Strong match: same device
        if getattr(candidate, "device_id", None) == getattr(
            reg_entity, "device_id", None
        ):
            return True
        # Fallback: match by shared identifiers if device registry is available
        if dev_reg is None or not base_identifiers:
            return False
        cand_device_id = getattr(candidate, "device_id", None)
        if not cand_device_id:
            return False
        try:
            cand_device = dev_reg.async_get(cand_device_id)
        except Exception:
            return False
        cand_identifiers = set(getattr(cand_device, "identifiers", set()) or set())
        return bool(base_identifiers.intersection(cand_identifiers))

    # Known translation_key values used by TRV integrations for valve-related entities.
    # These are stable, language-independent identifiers set by the integration.
    _VALVE_TRANSLATION_KEYS: dict[str, str] = {
        "valve_position": "valve_position",
        "valve_opening_degree": "valve_opening_degree",
        "valve_closing_degree": "valve_closing_degree",
        "pi_heating_demand": "pi_heating_demand",
        "heating_demand": "pi_heating_demand",
        # Shelly BLU TRV uses this translation_key
        "valve": "valve_position",
    }

    def _classify_by_translation_key(entity) -> str | None:
        """Classify entity by its translation_key (stable, language-independent)."""
        tk = getattr(entity, "translation_key", None)
        if tk and tk in _VALVE_TRANSLATION_KEYS:
            return _VALVE_TRANSLATION_KEYS[tk]
        return None

    def _classify(uid: str, ent_id: str, original_name: str) -> str | None:
        """Classify by string matching (fallback for integrations without translation_key)."""
        descriptor = f"{uid} {ent_id} {original_name}".lower()
        # Sonoff TRVZB (and some others) expose explicit valve degree entities
        if "valve_opening_degree" in descriptor:
            return "valve_opening_degree"
        if "valve_closing_degree" in descriptor:
            return "valve_closing_degree"

        # Existing patterns
        if "pi_heating_demand" in descriptor:
            return "pi_heating_demand"
        if "valve_position" in descriptor:
            return "valve_position"

        # Generic fallbacks: try to catch "valve ... position/opening/degree"
        if "valve" in descriptor and (
            "position" in descriptor
            or "opening" in descriptor
            or "degree" in descriptor
        ):
            return "valve_generic"

        if descriptor.endswith("_position") or descriptor.endswith(" position"):
            return "position"
        return None

    def _score(reason: str, writable: bool, domain: str) -> tuple[int, int, int]:
        # Higher is better.
        reason_score = {
            "valve_opening_degree": 100,
            "valve_closing_degree": 95,
            "valve_position": 90,
            "pi_heating_demand": 80,
            "valve_generic": 60,
            "position": 50,
        }.get(reason, 0)
        writable_score = 10 if writable else 0
        domain_score = 1 if domain in preferred_domains else 0
        return (reason_score, writable_score, domain_score)

    best: dict[str, Any] | None = None
    best_score: tuple[int, int, int] = (-1, -1, -1)

    for entity in entity_entries:
        uid = entity.unique_id or ""
        if not _device_matches(entity):
            continue

        # Prefer translation_key (stable, language-independent) over string matching
        reason = _classify_by_translation_key(entity)
        if reason is None:
            reason = _classify(
                uid,
                entity.entity_id or "",
                getattr(entity, "original_name", None) or "",
            )
        if reason is None:
            continue
        domain = (entity.entity_id or "").split(".", 1)[0]
        writable = domain in preferred_domains
        info = {
            "entity_id": entity.entity_id,
            "writable": writable,
            "reason": reason,
            "domain": domain,
        }

        score = _score(reason, writable, domain)
        if best is None or score > best_score:
            best = info
            best_score = score
        if not writable and readonly_candidate is None:
            readonly_candidate = info

    if best is not None and best.get("writable"):
        _LOGGER.debug(
            "better thermostat: Found writable valve helper %s for %s (reason=%s)",
            best.get("entity_id"),
            entity_id,
            best.get("reason"),
        )
        return best

    if readonly_candidate is not None:
        _LOGGER.debug(
            "better thermostat: Found read-only valve helper %s for %s (reason=%s)",
            readonly_candidate.get("entity_id"),
            entity_id,
            readonly_candidate.get("reason"),
        )
        return readonly_candidate

    _LOGGER.debug(
        "better thermostat: Could not find valve position entity for %s", entity_id
    )
    return None


async def find_battery_entity(self, entity_id, _visited=None):
    """Find the battery entity related to the given entity's device.

    Returns the `entity_id` of the battery sensor attached to the same device
    as `entity_id`, or None if none found.

    For groups, returns the battery entity with the lowest battery level
    among all group members.
    """
    entity_registry = er.async_get(self.hass)

    entity_info = entity_registry.entities.get(entity_id)

    if entity_info is None:
        return None

    device_id = entity_info.device_id

    # Groups and virtual entities have no device_id
    # Check if this is a group and resolve member batteries
    if device_id is None:
        state = self.hass.states.get(entity_id)
        if state and "entity_id" in state.attributes:
            # It's a group - find battery with lowest level among members
            return await _find_lowest_battery_in_group(
                self, state.attributes["entity_id"], _visited
            )
        return None

    for entity in entity_registry.entities.values():
        if entity.device_id == device_id and (
            entity.device_class == "battery"
            or entity.original_device_class == "battery"
        ):
            return entity.entity_id

    return None


async def _find_lowest_battery_in_group(self, member_ids, visited=None):
    """Find the battery entity with the lowest level among group members.

    Parameters
    ----------
    self :
        BetterThermostat instance
    member_ids :
        list of entity_id strings to search
    visited :
        set of already visited entity_ids to prevent infinite recursion

    Returns
    -------
    entity_id of the battery with lowest level, or None if no batteries found
    """
    if visited is None:
        visited = set()

    lowest_battery_id = None
    lowest_battery_level = None

    for member_id in member_ids:
        # Skip already visited entities to prevent infinite recursion
        if member_id in visited:
            continue
        visited.add(member_id)

        battery_id = await find_battery_entity(self, member_id, visited)
        if battery_id is None:
            continue

        battery_state = self.hass.states.get(battery_id)
        if battery_state is None:
            continue

        try:
            level = float(battery_state.state)
        except ValueError, TypeError:
            _LOGGER.debug(
                "better_thermostat: non-numeric battery state '%s' for %s",
                battery_state.state,
                battery_id,
            )
            continue

        if lowest_battery_level is None or level < lowest_battery_level:
            lowest_battery_level = level
            lowest_battery_id = battery_id

    return lowest_battery_id


# Known translation_key values used by TRV integrations for calibration entities.
# These are stable, language-independent identifiers set by the integration.
_CALIBRATION_TRANSLATION_KEYS: set[str] = {
    "local_temperature_calibration",
    "temperature_calibration",
    "temperature_offset",
    "calibration_temperature",
    "local_temperature_offset",
    # eq3btsmart uses "offset" as translation_key for its temperature offset
    "offset",
}

# Domains the calibration write path can address (number.set_value or
# select option handling).  Read-only entities such as the Zigbee2MQTT
# sensor.*_local_temperature must never be picked as calibration target.
_CALIBRATION_ENTITY_DOMAINS: set[str] = {"number", "select"}


async def find_local_calibration_entity(self, entity_id):
    """Find the local calibration entity for the TRV.

    Uses the entity registry's ``translation_key`` and ``original_name``
    for a stable, language-independent lookup.  Falls back to the legacy
    unique_id / entity_id string matching for older integrations.
    Only writable candidates (``number`` or ``select`` entities) are
    considered.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id :
            entity id of the TRV to find the local calibration entity for

    Returns
    -------
    str
            the entity_id of the local calibration entity
    None
            if no local calibration entity was found
    """
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        return None
    config_entry_id = reg_entity.config_entry_id
    if config_entry_id is None:
        return None
    entity_entries = async_entries_for_config_entry(entity_registry, config_entry_id)
    calibration_entity = None
    # First pass: match by translation_key (preferred, stable approach)
    for entity in entity_entries:
        if entity.device_id != reg_entity.device_id:
            continue
        if entity.domain not in _CALIBRATION_ENTITY_DOMAINS:
            continue
        tk = getattr(entity, "translation_key", None)
        if tk and tk in _CALIBRATION_TRANSLATION_KEYS:
            _LOGGER.debug(
                "better thermostat: Found local calibration entity %s for %s (translation_key=%s)",
                entity.entity_id,
                entity_id,
                tk,
            )
            calibration_entity = entity.entity_id
            break

    # Second pass: fallback to string matching on unique_id / entity_id / original_name.
    # Restricted to writable calibration domains: a read-only sensor sharing
    # the same substring (e.g. sensor.*_local_temperature) is never a valid
    # match, and without the restriction the winner depended on registry
    # iteration order, which is not guaranteed.
    if calibration_entity is None:
        for entity in entity_entries:
            if entity.device_id != reg_entity.device_id:
                continue
            if entity.domain not in _CALIBRATION_ENTITY_DOMAINS:
                continue
            descriptor = f"{entity.unique_id} {entity.entity_id} {getattr(entity, 'original_name', '') or ''}".lower()
            if (
                "temperature_calibration" in descriptor
                or "temperature_offset" in descriptor
                or "temperatur_offset" in descriptor
                or "local_temperature" in descriptor
            ):
                _LOGGER.debug(
                    "better thermostat: Found local calibration entity %s for %s (string match)",
                    entity.entity_id,
                    entity_id,
                )
                calibration_entity = entity.entity_id
                break

    if calibration_entity is None:
        _LOGGER.debug(
            "better thermostat: Could not find local calibration entity for %s",
            entity_id,
        )

    return calibration_entity


async def get_trv_intigration(self, entity_id):
    """Get the integration of the TRV.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id :
            entity id of the TRV to look up

    Returns
    -------
    str
            the integration of the TRV
    """
    entity_reg = er.async_get(self.hass)
    entry = entity_reg.async_get(entity_id)
    if entry is None:
        return "generic_thermostat"
    try:
        return entry.platform
    except AttributeError:
        return "generic_thermostat"


def get_max_value(obj, value, default):
    """Get the max value of an dict object."""
    try:
        _raw = []
        for key in obj.keys():
            _temp = obj[key].get(value, 0)
            if _temp is not None:
                _raw.append(_temp)
        return max(_raw, key=float)
    except KeyError, ValueError:
        return default


def get_min_value(obj, value, default):
    """Get the min value of an dict object."""
    try:
        _raw = []
        for key in obj.keys():
            _temp = obj[key].get(value, 999)
            if _temp is not None:
                _raw.append(_temp)
        return min(_raw, key=float)
    except KeyError, ValueError:
        return default


async def get_device_model(self, entity_id: str) -> str:
    """Determine the device model from the Device Registry entry.

    Priority: model_id > model (before parens) > model > config > "generic"
    """
    selected: str | None = None
    source: str = "none"

    try:
        entity_reg = er.async_get(self.hass)
        entry = entity_reg.async_get(entity_id)
        dev_reg = dr.async_get(self.hass)
        device = None
        try:
            dev_id = getattr(entry, "device_id", None)
            if isinstance(dev_id, str) and dev_id:
                device = dev_reg.async_get(dev_id)
        except Exception:
            device = None
        # Selection exclusively via Device-Registry
        try:
            _LOGGER.debug(
                "better_thermostat %s: device registry -> manufacturer=%s model=%s model_id=%s name=%s identifiers=%s",
                self.device_name,
                getattr(device, "manufacturer", None),
                getattr(device, "model", None),
                getattr(device, "model_id", None),
                getattr(device, "name", None),
                list(getattr(device, "identifiers", []) or []),
            )
        except Exception:
            pass

        dev_model_id = getattr(device, "model_id", None)
        if isinstance(dev_model_id, str) and len(dev_model_id.strip()) >= 2:
            selected = dev_model_id.strip()
            source = "devreg.model_id"
        else:
            model_str = getattr(device, "model", None)
            _LOGGER.debug(
                "better_thermostat %s: device.model raw='%s'",
                self.device_name,
                model_str,
            )
            if isinstance(model_str, str) and model_str.strip():
                # Extract model before parentheses: "MODEL (Desc)" -> "MODEL"
                model_clean: str = re.sub(r"\s*\(.*\)\s*$", "", model_str).strip()
                if len(model_clean) >= 2:
                    selected = model_clean
                    source = "devreg.model(before_parens)"
                elif len(model_str.strip()) >= 2:
                    selected = model_str.strip()
                    source = "devreg.model"
    except Exception:
        # swallow registry access issues and continue to fallback
        pass

    # Final fallback: configured model, then generic
    if not selected and isinstance(self.model, str) and len(self.model.strip()) >= 2:
        selected = self.model.strip()
        source = "config.model"
    if not selected:
        selected = "generic"
        source = "default"

    _LOGGER.debug(
        "better_thermostat %s: get_device_model(%s) selected='%s' via %s",
        self.device_name,
        entity_id,
        selected,
        source,
    )
    return selected


async def async_fire_logbook_entry(self, key: str, default_msg: str) -> None:
    """Fire a logbook entry safely, with fallback translations."""
    from homeassistant.helpers import translation
    from homeassistant.util import slugify

    from custom_components.better_thermostat.utils.const import DOMAIN

    hass_obj = getattr(self, "hass", None)
    log_msg = default_msg
    if hass_obj is not None:
        try:
            lang = getattr(getattr(hass_obj, "config", None), "language", "en")
            translations = await translation.async_get_translations(
                hass_obj, lang, "entity", integrations=[DOMAIN]
            )
            log_msg = translations.get(
                f"component.{DOMAIN}.entity.sensor.logbook.state.{key}", default_msg
            )
        except Exception:
            pass

        entity_id = getattr(self, "entity_id", None)
        if not entity_id:
            name = getattr(self, "name", "better_thermostat")
            entity_id = f"climate.{slugify(name)}"

        hass_obj.bus.async_fire(
            "logbook_entry",
            {
                "name": getattr(self, "name", "Better Thermostat"),
                "message": log_msg,
                "entity_id": entity_id,
                "domain": DOMAIN,
            },
        )
