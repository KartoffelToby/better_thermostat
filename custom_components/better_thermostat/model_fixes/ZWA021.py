"""Model quirks for the Eurotronic Spirit Z / Aeotec ZWA021 family.

These Z-Wave TRVs can be driven by externally supplied valve positions, but
only after being switched into a hidden "Manufacturer Specific" thermostat
mode. The quirk below engages that mode when — and only when — Better
Thermostat is configured for direct valve control. In every other calibration
mode the module is inert and the device is driven through the standard
``climate`` services, exactly like an unquirked TRV.
"""

import logging

from homeassistant.components.climate.const import HVACMode

from ..utils.const import CalibrationType

_LOGGER = logging.getLogger(__name__)

# Z-Wave JS service identifiers. Kept as literals to avoid importing the
# zwave_js integration package (which pulls in the zwave_js_server dependency)
# at module load time; the values are stable public service names.
_ZWAVE_JS_DOMAIN = "zwave_js"
_ZWAVE_JS_SET_VALUE = "set_value"

# Thermostat Mode command class (0x40) with the manufacturer-specific mode
# value that lets the device accept external valve positions. The mode is not
# advertised during the device interview (firmware omission), so it has to be
# written directly.
_THERMOSTAT_MODE_COMMAND_CLASS = "64"
_MANUFACTURER_SPECIFIC_MODE = "31"

# While in manufacturer-specific mode the valve is driven through the
# Multilevel Switch command class (0x26); its ``targetValue`` takes the valve
# opening on a 0-99 scale (0 = closed, 99 = fully open). This is the same
# mechanism the zwave-js "Multilevel Switch" control and OpenZWave use — the
# device exposes no writable valve *number* helper.
_MULTILEVEL_SWITCH_COMMAND_CLASS = 38
_VALVE_MAX = 99


def _is_direct_valve(self, entity_id):
    """Return True when this TRV is configured for direct valve control."""
    adv = self.real_trvs[entity_id].get("advanced", {}) or {}
    return adv.get("calibration") == CalibrationType.DIRECT_VALVE_BASED


def fix_local_calibration(self, entity_id, offset):
    """Return the given local calibration offset unchanged."""
    return offset


def fix_valve_calibration(self, entity_id, valve):
    """Return the given valve calibration unchanged."""
    return valve


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return the given target temperature unchanged."""
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """Engage the manufacturer-specific mode for direct valve control.

    Only active when the TRV is configured for direct valve control and the
    requested mode is not OFF. In all other cases this returns ``False`` so the
    caller falls back to the standard ``climate.set_hvac_mode`` service, leaving
    behaviour identical to a device without this quirk.
    """
    if not _is_direct_valve(self, entity_id):
        return False
    if hvac_mode == HVACMode.OFF:
        # Let the standard off path close the device / drive the valve to 0.
        return False

    _LOGGER.debug(
        "better_thermostat %s: TRV %s ZWA021 manufacturer-specific valve mode",
        self.device_name,
        entity_id,
    )
    await self.hass.services.async_call(
        _ZWAVE_JS_DOMAIN,
        _ZWAVE_JS_SET_VALUE,
        {
            "entity_id": entity_id,
            "command_class": _THERMOSTAT_MODE_COMMAND_CLASS,
            "property": "mode",
            "value": _MANUFACTURER_SPECIFIC_MODE,
        },
        blocking=True,
        context=self.context,
    )
    return True


async def override_set_temperature(self, entity_id, temperature):
    """Do not override set temperature."""
    return False


async def override_set_valve(self, entity_id, percent):
    """Drive the valve directly via the Multilevel Switch command class.

    Active only in direct valve control; otherwise returns ``False`` so the
    generic valve handling applies. The device is expected to already be in
    manufacturer-specific mode (see :func:`override_set_hvac_mode`). The
    requested 0-100 % opening is mapped onto the device's 0-99 range.
    """
    if not _is_direct_valve(self, entity_id):
        return False
    try:
        value = int(round(min(max(float(percent), 0.0), 100.0) / 100.0 * _VALVE_MAX))
    except TypeError, ValueError:
        return False

    _LOGGER.debug(
        "better_thermostat %s: TRV %s ZWA021 set valve %s%% -> %s/%s",
        self.device_name,
        entity_id,
        percent,
        value,
        _VALVE_MAX,
    )
    await self.hass.services.async_call(
        _ZWAVE_JS_DOMAIN,
        _ZWAVE_JS_SET_VALUE,
        {
            "entity_id": entity_id,
            "command_class": _MULTILEVEL_SWITCH_COMMAND_CLASS,
            "property": "targetValue",
            "value": value,
        },
        blocking=True,
        context=self.context,
    )
    return True
