"""Helper functions for the Better Thermostat component."""

from datetime import datetime
from enum import Enum
import logging
import math
import re
from typing import Any

from homeassistant.components.climate.const import HVACMode
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_registry import async_entries_for_config_entry

from custom_components.better_thermostat.utils.const import (
    CONF_HEAT_AUTO_SWAPPED,
    MAX_HEATING_POWER,
    MIN_HEATING_POWER,
    VALVE_MIN_BASE,
    VALVE_MIN_OPENING_LARGE_DIFF,
    VALVE_MIN_PROPORTIONAL_SLOPE,
    VALVE_MIN_SMALL_DIFF_THRESHOLD,
    VALVE_MIN_THRESHOLD_TEMP_DIFF,
    CalibrationMode,
)

_LOGGER = logging.getLogger(__name__)


def normalize_calibration_mode(mode: Any) -> CalibrationMode | str | None:
    """Normalize a calibration_mode field from TRV advanced data."""

    # Backwards compatibility: older configs stored numeric calibration modes
    # (e.g. 0 for DEFAULT). Only map known values.
    if isinstance(mode, (int, float)):
        try:
            numeric = int(mode)
        except (TypeError, ValueError):
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


def is_calibration_mode(mode: Any, expected: CalibrationMode) -> bool:
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
        advanced = (bt.real_trvs.get(entity_id, {}) or {}).get("advanced", {}) or {}
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


def mode_remap(self, entity_id, hvac_mode: str, inbound: bool = False) -> str:
    """Remap HVAC mode to correct mode if nessesary.

    Parameters
    ----------
    self :
            FIXME
    hvac_mode : str
            HVAC mode to be remapped

    inbound : bool
            True if the mode is coming from the device, False if it is coming from the HA.

    Returns
    -------
    str
            remapped mode according to device's quirks
    """
    _heat_auto_swapped = self.real_trvs[entity_id]["advanced"].get(
        CONF_HEAT_AUTO_SWAPPED, False
    )

    if _heat_auto_swapped:
        if hvac_mode == HVACMode.HEAT and not inbound:
            return HVACMode.AUTO
        if hvac_mode == HVACMode.AUTO and inbound:
            return HVACMode.HEAT
        return hvac_mode

    trv_modes = self.real_trvs[entity_id]["hvac_modes"]
    if HVACMode.HEAT not in trv_modes and HVACMode.HEAT_COOL in trv_modes:
        # entity only supports HEAT_COOL, but not HEAT - need to translate
        if not inbound and hvac_mode == HVACMode.HEAT:
            return HVACMode.HEAT_COOL
        if inbound and hvac_mode == HVACMode.HEAT_COOL:
            return HVACMode.HEAT

    if hvac_mode != HVACMode.AUTO:
        return hvac_mode

    _LOGGER.error(
        f"better_thermostat {self.device_name}: {entity_id} HVAC mode {
            hvac_mode
        } is not supported by this device, is it possible that you forgot to set the heat auto swapped option?"
    )
    return HVACMode.OFF


def heating_power_valve_position(self, entity_id):
    """Compute an expected valve position from the heating power.

    Given the global `heating_power` estimate and the target/current
    temperature, a heuristic mapping to valve opening percentage is
    returned (between 0.0 and 1.0).
    """
    _temp_diff = float(float(self.bt_target_temp) - float(self.cur_temp))

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
        f"better_thermostat {self.device_name}: {
            entity_id
        } / heating_power_valve_position - temp diff: {
            round(_temp_diff, 1)
        } - heating power: {
            round(heating_power, 4)
        } (bounded) - expected valve position: {round(valve_pos * 100)}%"
    )
    return valve_pos

    # Example values for different heating_power and temp_diff:
    # With heating_power of 0.02:
    # | temp_diff | valve_pos  |
    # |-----------|------------|
    # | 0.1       | 0.0871     |
    # | 0.2       | 0.1678     |
    # | 0.3       | 0.2462     |
    # | 0.4       | 0.3232     |
    # | 0.5       | 0.3992     |

    # With heating_power of 0.01:
    # | temp_diff | valve_pos  |
    # |-----------|------------|
    # | 0.1       | 0.1678     |
    # | 0.2       | 0.3232     |
    # | 0.3       | 0.4744     |
    # | 0.4       | 0.6227     |
    # | 0.5       | 0.7691     |

    # With heating_power of 0.005:
    # | temp_diff | valve_pos  |
    # |-----------|------------|
    # | 0.1       | 0.3232     |
    # | 0.2       | 0.6227     |
    # | 0.3       | 0.9139     |
    # | 0.4       | 1.0000     |
    # | 0.5       | 1.0000     |


def convert_to_float(
    value: str | int | float | None, instance_name: str, context: str
) -> float | None:
    """Convert value to float or print error message.

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
        # Rounding to 0.1 caused issues where 19.97 became 20.0, leading to
        # incorrect HVAC action decisions (see issues #1792, #1789, #1785).
        return round_by_step(float(value), 0.01)
    except (ValueError, TypeError, AttributeError, KeyError):
        _LOGGER.debug(
            f"better thermostat {instance_name}: Could not convert '{value}' to float in {context}"
        )
        return None


class rounding(Enum):
    """Rounding helpers for stable step-based rounding.

    Provides minor offsets to avoid floating point rounding artifacts when
    converting values to integer steps.
    """

    def up(x: float) -> float:
        """Round up with a tiny epsilon to avoid FP artifacts."""
        return math.ceil(x - 0.0001)

    def down(x: float) -> float:
        """Round down with a tiny epsilon to avoid FP artifacts."""
        return math.floor(x + 0.0001)

    def nearest(x: float) -> float:
        """Round to nearest step with a small epsilon to avoid up-rounding."""
        return round(x - 0.0001)


def round_by_step(
    value: float | None, step: float | None, f_rounding: rounding = rounding.nearest
) -> float | None:
    """Round the value based on the allowed decimal 'step' size.

    Parameters
    ----------
    value : float
            the value to round
    step : float
            size of one step

    Returns
    -------
    float
            the rounded value
    """

    if value is None or step is None:
        return None
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
    except ValueError:
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
        _current_time = datetime.now()
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
        base_device = (
            dev_reg.async_get(reg_entity.device_id)
            if getattr(reg_entity, "device_id", None)
            else None
        )
        base_identifiers = set(getattr(base_device, "identifiers", set()) or set())
    except Exception:
        dev_reg = None
        base_identifiers = set()

    entity_entries = async_entries_for_config_entry(
        entity_registry, reg_entity.config_entry_id
    )
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

    def _classify(uid: str, ent_id: str, original_name: str) -> str | None:
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
        reason = _classify(
            uid, entity.entity_id or "", getattr(entity, "original_name", None) or ""
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
    self : BetterThermostat instance
    member_ids : list of entity_id strings
    visited : set of already visited entity_ids to prevent infinite recursion

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
        except (ValueError, TypeError):
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


async def find_local_calibration_entity(self, entity_id):
    """Find the local calibration entity for the TRV.

    This is a hacky way to find the local calibration entity for the TRV. It is not possible to find the entity
    automatically, because the entity_id is not the same as the friendly_name. The friendly_name is the same for all
    thermostats of the same brand, but the entity_id is different.

    Parameters
    ----------
    self :
            self instance of better_thermostat

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
    entity_entries = async_entries_for_config_entry(
        entity_registry, reg_entity.config_entry_id
    )
    calibration_entity = None
    config_entry_id = reg_entity.config_entry_id
    
    # First pass: Search within the same device
    for entity in entity_entries:
        uid = entity.unique_id + " " + entity.entity_id
        # Make sure we use the correct device entities
        if entity.device_id == reg_entity.device_id:
            if (
                "temperature_calibration" in uid
                or "temperature_offset" in uid
                or "temperatur_offset" in uid
            ):
                calibration_entity = entity.entity_id
                _LOGGER.debug(
                    "better thermostat: Found local calibration entity %s for %s",
                    entity.entity_id,
                    entity_id,
                )
                break
    
    # Fallback: Search for SELECT entities with temperature_offset keyword
    if calibration_entity is None:
        for entity in entity_entries:
            if entity.config_entry_id == config_entry_id:
                if ("temperature_offset" in entity.unique_id.lower() or 
                    "temperatur_offset" in entity.unique_id.lower() or
                    "temperature_offset" in entity.entity_id.lower()):
                    calibration_entity = entity.entity_id
                    _LOGGER.debug(
                        "better thermostat: Found SELECT temperature offset entity %s for %s",
                        entity.entity_id,
                        entity_id,
                    )
                    break
    
    if calibration_entity is None:
        _LOGGER.debug(
            "better thermostat: Could not find local calibration entity for %s", entity_id
        )
    
    return calibration_entity


async def get_trv_intigration(self, entity_id):
    """Get the integration of the TRV.

    Parameters
    ----------
    self :
            self instance of better_thermostat

    Returns
    -------
    str
            the integration of the TRV
    """
    entity_reg = er.async_get(self.hass)
    entry = entity_reg.async_get(entity_id)
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
    except (KeyError, ValueError):
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
    except (KeyError, ValueError):
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
