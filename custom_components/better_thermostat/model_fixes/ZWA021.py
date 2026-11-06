"""Model quirks for the Eurotronic Spirit Z / Aeotec ZWA021 family.

These Z-Wave TRVs can be driven by externally supplied valve positions, but
only after being switched into a hidden "Manufacturer Specific" thermostat
mode. The quirk below engages that mode when — and only when — Better
Thermostat is configured for direct valve control. In every other calibration
mode the module is inert and the device is driven through the standard
``climate`` services, exactly like an unquirked TRV.
"""

import asyncio
import logging
import math
from time import monotonic

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError

from ..utils.const import CalibrationType
from ..utils.helpers import find_local_calibration_entity

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


# The Spirit/ZWA021 accepts an externally reported air temperature when its
# local temperature offset is set to the reserved -128 value. Remember the
# last report per TRV so an unchanged sensor value does not generate another
# radio transmission on each accepted Better Thermostat update.
_EXTERNAL_TEMPERATURE_MODE_VALUE = -128.0
_EXTERNAL_TEMPERATURE_LAST_SENT = "_zwa021_external_temperature_last_sent"
_EXTERNAL_TEMPERATURE_OPTION = "zwave_external_temperature"
_EXTERNAL_TEMPERATURE_PREVIOUS_OFFSET = "_zwave_external_previous_offset"
_EXTERNAL_TEMPERATURE_MODE_PENDING = "_zwa021_external_temperature_mode_pending"
_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING = (
    "_zwa021_external_temperature_mode_write_pending"
)
_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT = (
    "_zwa021_external_temperature_mode_write_pending_at"
)
_EXTERNAL_TEMPERATURE_MODE_LAST_ATTEMPT = (
    "_zwa021_external_temperature_mode_last_attempt"
)
_EXTERNAL_TEMPERATURE_MODE_RETRY_INTERVAL_S = 60.0
_EXTERNAL_TEMPERATURE_MODE_WRITE_TIMEOUT_S = 30.0
_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_TIMEOUT_S = 60.0

# Let the shared keepalive distinguish its deliberate refresh from ordinary
# sensor updates, which are deduplicated above.
external_temperature_force_refresh = True


def _is_direct_valve(self, entity_id):
    """Return True when this TRV is configured for direct valve control."""
    adv = self.real_trvs[entity_id].advanced or {}
    return adv.get("calibration") == CalibrationType.DIRECT_VALVE_BASED


async def maybe_set_external_temperature(
    self, entity_id, temperature: float, *, force_refresh: bool = False
) -> bool:
    """Send the room temperature to the Spirit/ZWA021 as a Z-Wave report.

    The TRV keeps its own thermostat and valve regulation. Better Thermostat
    only sends a Sensor Multilevel air-temperature report (CC 49); it never
    calculates or writes a valve position here. To avoid changing normal
    offset calibration behaviour, the report is sent only while the device's
    local offset number currently reports the reserved external-sensor value
    (-128).

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance.
    entity_id : str
        Climate entity ID of the Spirit/ZWA021.
    temperature : float
        Filtered room temperature in Celsius, as produced by the existing
        external-sensor path.
    force_refresh : bool
        Send even if the value is unchanged. The existing periodic keepalive
        uses this to refresh the report without causing writes on every
        unchanged sensor update.

    Returns
    -------
    bool
        True if a report was sent or the current value was already present;
        False if this device is not in external-temperature mode or the write
        could not be sent.
    """
    if self.real_trvs[entity_id].integration != "zwave_js":
        return False
    if _is_direct_valve(self, entity_id):
        return False

    trv = self.real_trvs[entity_id]
    if not (trv.advanced or {}).get(_EXTERNAL_TEMPERATURE_OPTION, False):
        return False
    if trv.extra.get(_EXTERNAL_TEMPERATURE_MODE_PENDING):
        await async_retry_external_temperature_mode(self, entity_id)
    calibration_entity = trv.local_temperature_calibration_entity
    if calibration_entity is None:
        # Temperature forwarding is independent of Better Thermostat's own
        # calibration type, so discover the sibling offset entity here too.
        calibration_entity = await find_local_calibration_entity(self, entity_id)
        trv.local_temperature_calibration_entity = calibration_entity
    if calibration_entity is None:
        return False
    calibration_state = self.hass.states.get(calibration_entity)
    if calibration_state is None:
        return False
    try:
        offset_value = float(calibration_state.state)
        temperature_value = float(temperature)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(temperature_value):
        return False

    # This is an explicit opt-in guard: -128 is the device's reserved
    # external-temperature mode, not a usable local offset.
    if not math.isclose(
        offset_value, _EXTERNAL_TEMPERATURE_MODE_VALUE, rel_tol=0.0, abs_tol=1e-6
    ):
        return False

    # The Air Temperature sensor type (1), Celsius scale (0), and the
    # filtered room value match the Z-Wave JS invoke_cc_api call used to verify
    # this command against the test thermostat.
    report_value = round(temperature_value, 2)
    extra = self.real_trvs[entity_id].extra
    previous = extra.get(_EXTERNAL_TEMPERATURE_LAST_SENT)
    if previous is not None and not force_refresh:
        if math.isclose(report_value, previous, rel_tol=0.0, abs_tol=0.005):
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
                "parameters": [1, 0, report_value],
            },
            blocking=True,
            context=self.context,
        )
    except (HomeAssistantError, OSError, RuntimeError, TypeError, ValueError) as ex:
        _LOGGER.warning(
            "better_thermostat %s: Spirit/ZWA021 external temperature report for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False

    extra[_EXTERNAL_TEMPERATURE_LAST_SENT] = report_value
    _LOGGER.debug(
        "better_thermostat %s: sent Spirit/ZWA021 external air temperature %.2f C to %s%s",
        self.device_name,
        report_value,
        entity_id,
        " (keepalive)" if force_refresh else "",
    )
    return True


def external_temperature_mode_pending(self, entity_id: str) -> bool:
    """Return whether a mode transition or late disable needs retrying.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance.
    entity_id : str
        Climate entity ID of the Spirit/ZWA021.

    Returns
    -------
    bool
        ``True`` while a mode write needs retrying or a late ``-128`` report
        needs restoration after the option was disabled.
    """
    trv = self.real_trvs.get(entity_id)
    if trv is None:
        return False
    if trv.extra.get(_EXTERNAL_TEMPERATURE_MODE_PENDING):
        return True

    # A timed-out enable write can report -128 after the option was disabled.
    # Treat that late report as pending restoration instead of leaving the TRV
    # in external-temperature mode.
    if (trv.advanced or {}).get(_EXTERNAL_TEMPERATURE_OPTION, False):
        return False
    previous_offset = (trv.advanced or {}).get(_EXTERNAL_TEMPERATURE_PREVIOUS_OFFSET)
    if previous_offset is None:
        return False
    calibration_entity = trv.local_temperature_calibration_entity
    state = self.hass.states.get(calibration_entity) if calibration_entity else None
    if state is None:
        return False
    try:
        current = float(state.state)
    except (TypeError, ValueError):
        return False
    return math.isclose(
        current, _EXTERNAL_TEMPERATURE_MODE_VALUE, rel_tol=0.0, abs_tol=1e-6
    )


async def async_retry_external_temperature_mode(
    self, entity_id: str, *, force: bool = False
) -> bool:
    """Retry a pending external-temperature offset transition.

    Retries are throttled so rapid room-sensor updates cannot create repeated
    Z-Wave offset writes. A state change to the requested offset confirms the
    transition and clears the pending markers.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance.
    entity_id : str
        Climate entity ID of the Spirit/ZWA021.
    force : bool
        Retry immediately after an offset state change, bypassing the retry
        interval.

    Returns
    -------
    bool
        ``True`` when no transition is pending or the requested offset is
        confirmed; otherwise, ``False``.
    """
    trv = self.real_trvs.get(entity_id)
    if trv is None:
        return True
    if not trv.extra.get(_EXTERNAL_TEMPERATURE_MODE_PENDING):
        if not external_temperature_mode_pending(self, entity_id):
            return True
        trv.extra[_EXTERNAL_TEMPERATURE_MODE_PENDING] = True

    now = monotonic()
    write_pending_at = trv.extra.get(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT)
    if trv.extra.get(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING) and (
        write_pending_at is None
        or now - write_pending_at
        >= _EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_TIMEOUT_S
    ):
        # A report can be lost even when the command was accepted. Bound the
        # wait, then retry against the currently reported offset below.
        trv.extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING, None)
        trv.extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT, None)

    last_attempt = trv.extra.get(_EXTERNAL_TEMPERATURE_MODE_LAST_ATTEMPT)
    if (
        not force
        and last_attempt is not None
        and now - last_attempt < _EXTERNAL_TEMPERATURE_MODE_RETRY_INTERVAL_S
    ):
        return False

    trv.extra[_EXTERNAL_TEMPERATURE_MODE_LAST_ATTEMPT] = now
    return await async_apply_external_temperature_mode(self, entity_id)


async def async_apply_external_temperature_mode(self, entity_id) -> bool:
    """Set the reserved local offset for report mode, or restore its backup.

    On disable, restore the saved offset only when the device still reports
    -128; an intervening manual change is left untouched. If the entity or
    command is unavailable, the transition stays pending for a later state
    update or keepalive retry.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance.
    entity_id : str
        Climate entity ID of the Spirit/ZWA021.

    Returns
    -------
    bool
        ``True`` when the requested offset is confirmed or no restoration is
        needed; otherwise, ``False`` while waiting for the entity or a write.
    """
    trv = self.real_trvs[entity_id]
    if trv.integration != "zwave_js" or _is_direct_valve(self, entity_id):
        return False
    extra = trv.extra
    enabled = bool((trv.advanced or {}).get(_EXTERNAL_TEMPERATURE_OPTION, False))
    extra[_EXTERNAL_TEMPERATURE_MODE_PENDING] = True
    calibration_entity = trv.local_temperature_calibration_entity
    if calibration_entity is None:
        calibration_entity = await find_local_calibration_entity(self, entity_id)
        trv.local_temperature_calibration_entity = calibration_entity
    if calibration_entity is None:
        _LOGGER.warning(
            "better_thermostat %s: external temperature mode has no local offset entity for %s",
            self.device_name,
            entity_id,
        )
        return False
    state = self.hass.states.get(calibration_entity)
    if state is None:
        return False
    try:
        current = float(state.state)
    except (TypeError, ValueError):
        return False

    if enabled:
        target = _EXTERNAL_TEMPERATURE_MODE_VALUE
        if math.isclose(current, target, rel_tol=0.0, abs_tol=1e-6):
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_PENDING, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_LAST_ATTEMPT, None)
            return True
        if extra.get(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING):
            return False
        action = "enabling"
    else:
        if not math.isclose(
            current, _EXTERNAL_TEMPERATURE_MODE_VALUE, rel_tol=0.0, abs_tol=1e-6
        ):
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_PENDING, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_LAST_ATTEMPT, None)
            return True
        if extra.get(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING):
            return False
        try:
            target = float(
                (trv.advanced or {}).get(_EXTERNAL_TEMPERATURE_PREVIOUS_OFFSET)
            )
        except (TypeError, ValueError):
            _LOGGER.warning(
                "better_thermostat %s: cannot restore the previous offset for %s; no saved value",
                self.device_name,
                entity_id,
            )
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_PENDING, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT, None)
            return False
        if not math.isfinite(target) or math.isclose(
            target, _EXTERNAL_TEMPERATURE_MODE_VALUE, rel_tol=0.0, abs_tol=1e-6
        ):
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_PENDING, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING, None)
            extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT, None)
            _LOGGER.warning(
                "better_thermostat %s: cannot restore the previous offset for %s; saved value is invalid",
                self.device_name,
                entity_id,
            )
            return False
        action = "restoring"

    try:
        async with asyncio.timeout(_EXTERNAL_TEMPERATURE_MODE_WRITE_TIMEOUT_S):
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": calibration_entity, "value": target},
                blocking=True,
                context=self.context,
            )
    except TimeoutError:
        # The call may have reached Z-Wave JS even though no reply arrived.
        # Keep the transition pending and reconcile any late state report.
        extra[_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING] = True
        extra[_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT] = monotonic()
        _LOGGER.warning(
            "better_thermostat %s: %s external temperature mode for %s timed out; "
            "waiting for state confirmation",
            self.device_name,
            action,
            entity_id,
        )
        return False
    except (HomeAssistantError, OSError, RuntimeError, TypeError, ValueError) as ex:
        extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING, None)
        extra.pop(_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT, None)
        _LOGGER.warning(
            "better_thermostat %s: %s external temperature mode for %s failed: %s",
            self.device_name,
            action,
            entity_id,
            ex,
        )
        return False
    extra[_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING] = True
    extra[_EXTERNAL_TEMPERATURE_MODE_WRITE_PENDING_AT] = monotonic()
    _LOGGER.info(
        "better_thermostat %s: %s external temperature offset for %s: %s",
        self.device_name,
        action,
        entity_id,
        target,
    )
    return False



# When Thermostat Mode is set to command class (0x40) with the manufacturer-specific mode
# TRV Climate entity state is reported as STATE_UNKNOWN, but the device is actually available and controllable.
# This flag allows the Better Thermostat to treat STATE_UNKNOWN as available for this model.
# This is a model-specific quirk, so it is set here in the model quirks
def trv_state_unknown_as_available(self, entity_id):
    """Return whether this TRV uses direct valve control.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity identifier of the TRV to check.

    Returns
    -------
    bool
        ``True`` when this TRV uses direct valve-based calibration and should
        be treated as available while its Climate entity state is unknown;
        otherwise, ``False``.
    """
    return _is_direct_valve(self, entity_id)


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
        "better_thermostat %s: TRV %s Spirit/ZWA021 manufacturer-specific valve mode",
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
            "better_thermostat %s: Spirit/ZWA021 valve mode write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False
    extra[_VALVE_MODE_ENGAGED] = True
    return True


async def override_set_temperature(self, entity_id, temperature):
    """Do not override set temperature."""
    return False


async def override_set_valve(self, entity_id, percent):
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
        value = int(round(min(max(float(percent), 0.0), 100.0) / 100.0 * _VALVE_MAX))
    except TypeError, ValueError:
        return False

    _LOGGER.debug(
        "better_thermostat %s: TRV %s Spirit/ZWA021 set valve %s%% -> %s/%s",
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
            "better_thermostat %s: Spirit/ZWA021 valve write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False
    return True
