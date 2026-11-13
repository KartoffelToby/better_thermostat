"""Model quirks for the Eurotronic Spirit Z / Aeotec ZWA021 family.

These Z-Wave TRVs can be driven by externally supplied valve positions, but
only after being switched into a hidden "Manufacturer Specific" thermostat
mode. The quirk engages that mode only for direct valve control. Separately,
it can send the room temperature as a CC 49 report when the device's offset
number already holds its reserved ``-128`` external-temperature value and
Better Thermostat is configured for ``no_calibration``.
"""

import logging
import math
from time import monotonic

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError

from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    ModelFixHost,
    ModelQuirks,
    UnknownStateQuirk,
    ValveQuirk,
)

from ..adapters.base import reserved_published_values
from ..utils.const import CalibrationMode, CalibrationOutput
from ..utils.helpers import (
    configured_calibration_mode,
    configured_calibration_output,
    find_local_calibration_entity,
)

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

# Key in the TRV record's ``extra`` mapping: set once the valve mode was
# written and went through.
_VALVE_MODE_ENGAGED = "_zwa021_valve_mode_engaged"
_EXTERNAL_TEMPERATURE_LAST_SENT = "_zwa021_external_temperature_last_sent"
_EXTERNAL_TEMPERATURE_SENT_AT = "_zwa021_external_temperature_sent_at"
_EXTERNAL_TEMPERATURE_MODE = -128.0
_EXTERNAL_TEMPERATURE_REFRESH_SECONDS = 15 * 60.0


def _is_direct_valve(self: ModelFixHost, entity_id: str) -> bool:
    """Return True when this TRV is configured for direct valve control."""
    return (
        configured_calibration_output(self.real_trvs[entity_id].advanced)
        == CalibrationOutput.DIRECT_VALVE_BASED
    )


def trv_state_unknown_as_available(self: ModelFixHost, entity_id: str) -> bool:
    """Answer whether the TRV is operating while its state reads ``unknown``.

    The manufacturer-specific thermostat mode is not one the climate entity
    describes, so the entity reports ``unknown`` for as long as the device
    is in it. The device is reachable throughout and takes the valve
    positions Better Thermostat writes.

    Parameters
    ----------
    self : ModelFixHost
        Host providing Home Assistant access and the per-TRV records
    entity_id : str
        Entity ID of the TRV being judged

    Returns
    -------
    bool
        True while this TRV is configured for direct valve control, the
        only setting under which the quirk engages that mode.
    """
    return _is_direct_valve(self, entity_id)


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Return the given local calibration offset unchanged."""
    return calibration_offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Return the given target temperature unchanged."""
    return temperature


async def _local_offset_entity(self: ModelFixHost, entity_id: str) -> str | None:
    """Find and cache the local offset entity used to select external input."""
    trv = self.real_trvs[entity_id]
    calibration_entity = trv.local_temperature_calibration_entity
    if calibration_entity is None:
        calibration_entity = await find_local_calibration_entity(self, entity_id)
        trv.local_temperature_calibration_entity = calibration_entity
    return calibration_entity


async def maybe_set_external_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Send the room reading as Z-Wave Sensor Multilevel Air Temperature.

    The Spirit/ZWA021 keeps its own thermostat and valve regulation. This
    quirk only supplies the air-temperature report; it does not set or restore
    the reserved offset and never writes a valve.
    """
    trv = self.real_trvs[entity_id]
    if trv.integration != "zwave_js" or _is_direct_valve(self, entity_id):
        return False
    if (
        configured_calibration_mode(trv.advanced) is not CalibrationMode.NO_CALIBRATION
    ):
        return False
    calibration_entity = await _local_offset_entity(self, entity_id)
    state = self.hass.states.get(calibration_entity) if calibration_entity else None
    if state is None:
        return False
    try:
        offset = float(state.state)
        report_temperature = float(temperature)
    except (TypeError, ValueError):
        return False
    if (
        not math.isfinite(report_temperature)
        or not math.isclose(offset, _EXTERNAL_TEMPERATURE_MODE, abs_tol=1e-6)
        or _EXTERNAL_TEMPERATURE_MODE not in reserved_published_values(state)
    ):
        return False
    report_temperature = round(report_temperature, 2)
    previous = trv.extra.get(_EXTERNAL_TEMPERATURE_LAST_SENT)
    sent_at = trv.extra.get(_EXTERNAL_TEMPERATURE_SENT_AT)
    now = monotonic()
    if isinstance(previous, int | float) and math.isclose(
        report_temperature, float(previous), abs_tol=0.005
    ):
        if (
            isinstance(sent_at, int | float)
            and now - float(sent_at) < _EXTERNAL_TEMPERATURE_REFRESH_SECONDS
        ):
            return True
    try:
        await self.hass.services.async_call(
            "zwave_js",
            "invoke_cc_api",
            {
                "entity_id": entity_id,
                "command_class": 49,
                "endpoint": 0,
                "method_name": "sendReport",
                "parameters": [1, 0, report_temperature],
            },
            blocking=True,
            context=self.context,
        )
    except (HomeAssistantError, OSError, RuntimeError, TypeError, ValueError) as ex:
        _LOGGER.warning(
            "better_thermostat %s: ZWA021 external temperature report for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False
    trv.extra[_EXTERNAL_TEMPERATURE_LAST_SENT] = report_temperature
    trv.extra[_EXTERNAL_TEMPERATURE_SENT_AT] = monotonic()
    _LOGGER.debug(
        "better_thermostat %s: sent ZWA021 external air temperature %.2f °C to %s",
        self.device_name,
        report_temperature,
        entity_id,
    )
    return True


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """Engage the manufacturer-specific mode for direct valve control.

    Only active when the TRV is configured for direct valve control and the
    requested mode is not OFF. In all other cases, and when the device refuses
    the mode write, this returns ``False`` so the caller falls back to the
    standard ``climate.set_hvac_mode`` service, leaving behaviour identical to
    a device without this quirk.
    """
    if not _is_direct_valve(self, entity_id):
        return False
    if hvac_mode == HVACMode.OFF:
        # Let the standard off path close the device / drive the valve to 0.
        return False

    # The climate entity reads ``unknown`` for as long as the device stays in
    # the manufacturer-specific mode. Once this quirk engaged the mode, that
    # reading says it still holds, and writing it again would only cost the
    # battery device a radio message per control cycle. A device that left
    # the mode reports a standard one and is written again.
    extra = self.real_trvs[entity_id].extra
    state = self.hass.states.get(entity_id)
    if (
        extra.get(_VALVE_MODE_ENGAGED)
        and state is not None
        and state.state == STATE_UNKNOWN
    ):
        return True
    extra.pop(_VALVE_MODE_ENGAGED, None)

    _LOGGER.debug(
        "better_thermostat %s: TRV %s ZWA021 manufacturer-specific valve mode",
        self.device_name,
        entity_id,
    )
    try:
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
    except (HomeAssistantError, OSError) as ex:
        # No Z-Wave JS to reach, a node that is asleep or out of range, or a
        # firmware that rejects the undocumented mode: the device is not in
        # valve mode, so the standard climate service is the one that can
        # still make it heat.
        _LOGGER.warning(
            "better_thermostat %s: ZWA021 valve mode write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False
    extra[_VALVE_MODE_ENGAGED] = True
    return True


async def override_set_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Do not override set temperature."""
    return False


async def override_set_valve(self: ModelFixHost, entity_id: str, percent: int) -> bool:
    """Drive the valve directly via the Multilevel Switch command class.

    Active only in direct valve control; otherwise, and when the device
    refuses the write, returns ``False`` so the generic valve handling
    applies. The device is expected to already be in manufacturer-specific
    mode (see :func:`override_set_hvac_mode`). The requested 0-100 % opening
    is mapped onto the device's 0-99 range.
    """
    if not _is_direct_valve(self, entity_id):
        return False
    try:
        value = round(min(max(float(percent), 0.0), 100.0) / 100.0 * _VALVE_MAX)
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
    try:
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
    except (HomeAssistantError, OSError) as ex:
        # Reporting the refused write as a declined one keeps the caller from
        # recording a position the valve never reached, and leaves it free to
        # try the generic valve channel.
        _LOGGER.warning(
            "better_thermostat %s: ZWA021 valve write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False
    return True


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    override_set_valve = staticmethod(override_set_valve)
    trv_state_unknown_as_available = staticmethod(trv_state_unknown_as_available)
    maybe_set_external_temperature = staticmethod(maybe_set_external_temperature)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_UNKNOWN_STATE_QUIRK: UnknownStateQuirk = _Surface()
_VALVE_QUIRK: ValveQuirk = _Surface()
_EXTERNAL_TEMPERATURE_QUIRK: ExternalTemperatureQuirk = _Surface()
