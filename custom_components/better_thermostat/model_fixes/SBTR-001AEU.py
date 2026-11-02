"""Quirks for the Shelly BLU TRV (SBTR-001AEU).

Home Assistant's Shelly integration registers the TRV behind a BLU gateway
with the model id ``SBTR-001AEU`` and the model name ``Shelly BLU TRV``. With
its own thermostat enabled the TRV regulates on an external room
temperature, which the integration exposes as the ``external_temperature``
number on the TRV's device (-50 to 50 °C in steps of 0.1 °C). A TRV that
gets no such value reports ``ext_temp_missing`` on the gateway. This module
keeps that number current with the room temperature Better Thermostat
regulates on.

The room temperature is offered on every reading Better Thermostat accepts
and again every 30 minutes. A write is due when nothing was written yet,
when the temperature moved by 0.1 °C, when the number no longer holds the
value written last, or when 5 minutes have passed since the last write. The
TRV does not publish after how long it gives up on a value it was given, so
a 30-minute offer is written unless a write went out less than 5 minutes
before it, and the gap between two writes stays under 35 minutes.

The TRV corrects its own regulation with that value, so it is written only
to a TRV Better Thermostat passes the target to unchanged, which is the
calibration mode ``no_calibration``; a calibration on top would compensate
the same bias a second time. Under direct valve control the TRV's own
thermostat is switched off and reads no room temperature, so nothing is
written either. A TRV that would take the value but has no enabled number
for it is reported once in the log.

Everything else stays as the default quirks have it.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import math
from time import monotonic
from typing import Final

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_conversion import TemperatureConverter

from custom_components.better_thermostat.model_fixes.default import (
    VALVE_MAINTENANCE_INTERVAL_HOURS,
    fix_local_calibration,
    fix_target_temperature_calibration,
    initial_tweak,
    override_set_hvac_mode,
    override_set_temperature,
    trv_state_unknown_as_available,
)
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    MaintenanceIntervalQuirk,
    ModelFixHost,
    ModelQuirks,
    QuirkScratchpad,
    UnknownStateQuirk,
)
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationOutput,
)
from custom_components.better_thermostat.utils.helpers import (
    configured_calibration_mode,
    configured_calibration_output,
    external_temperature_input_value,
    find_keyed_device_entity,
    state_is_live,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "maybe_set_external_temperature",
    "override_set_hvac_mode",
    "override_set_temperature",
]

# Translation key the Shelly integration registers the number under, and the
# fragment its unique id ``<address>-blutrv:<id>-external_temperature`` ends in.
_TK_EXTERNAL_TEMPERATURE: Final = frozenset({"external_temperature"})
_EXTERNAL_TEMPERATURE_FRAGMENT: Final = "external_temperature"

# Range of the number, in degrees Celsius, for one that publishes no bounds.
_INPUT_MIN_CELSIUS: Final = -50.0
_INPUT_MAX_CELSIUS: Final = 50.0

# Smallest change of the room temperature worth a write of its own.
_SIGNIFICANT_CHANGE_KELVIN: Final = 0.1

# Seconds after which an unchanged temperature is written again. Shorter than
# the 30-minute offer, so an offer is skipped only right after another write.
_REFRESH_SECONDS: Final = 5 * 60

# How far the value the number holds may sit from the one written, in degrees
# Celsius, before it counts as a different value. Half the number's step.
_HELD_VALUE_TOLERANCE_KELVIN: Final = 0.05

# Seconds a write is given before the value the number holds counts as its
# answer to it.
_WRITE_ECHO_GRACE_SECONDS: Final = 60


def _published_unit(state: State | None) -> str:
    """Return the temperature unit the number publishes, Celsius when it names none."""
    attributes: Mapping[str, object] = state.attributes if state is not None else {}
    declared_unit = attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if (
        isinstance(declared_unit, str)
        and declared_unit in TemperatureConverter.VALID_UNITS
    ):
        return declared_unit
    return UnitOfTemperature.CELSIUS


def _to_celsius(state: State | None, value: float) -> float:
    """Return the temperature in degrees Celsius a value of the number stands for."""
    return TemperatureConverter.convert(
        value, _published_unit(state), UnitOfTemperature.CELSIUS
    )


def _held_celsius(state: State | None) -> float | None:
    """Return the temperature the number holds now, or None when it reports none."""
    if not state_is_live(state):
        return None
    try:
        return _to_celsius(state, float(state.state))
    except TypeError, ValueError:
        return None


def _write_is_due(
    scratchpad: QuirkScratchpad, celsius: float, held_celsius: float | None, now: float
) -> bool:
    """Answer whether the room temperature is to be written to the TRV.

    Parameters
    ----------
    scratchpad : QuirkScratchpad
        Write state of the TRV, holding the last write if there was one.
    celsius : float
        The temperature to write, in degrees Celsius, as the number takes it.
    held_celsius : float or None
        The temperature the number holds, or None when it reports none.
    now : float
        Monotonic clock reading, in seconds.

    Returns
    -------
    bool
        True when nothing was written yet, the number holds a different value
        from the one written once the write has had time to land, the
        temperature moved by at least 0.1 °C, or ``_REFRESH_SECONDS`` have
        passed since the last write.
    """
    written_celsius = scratchpad.get("_shelly_blu_external_temperature_celsius")
    written_at = scratchpad.get("_shelly_blu_external_temperature_written_at")
    if written_celsius is None or written_at is None:
        return True
    if (
        held_celsius is not None
        and now - written_at >= _WRITE_ECHO_GRACE_SECONDS
        and not math.isclose(
            held_celsius, written_celsius, abs_tol=_HELD_VALUE_TOLERANCE_KELVIN
        )
    ):
        return True
    if round(abs(celsius - written_celsius), 2) >= _SIGNIFICANT_CHANGE_KELVIN:
        return True
    return now - written_at >= _REFRESH_SECONDS


def _report_missing_number(
    self: ModelFixHost, entity_id: str, scratchpad: QuirkScratchpad
) -> None:
    """Warn once that the TRV has no enabled external temperature number."""
    if scratchpad.get("_shelly_blu_external_temperature_missing_logged"):
        return
    scratchpad["_shelly_blu_external_temperature_missing_logged"] = True
    _LOGGER.warning(
        "better_thermostat %s: Shelly BLU TRV %s has no enabled external "
        "temperature number, so the room temperature does not reach it and "
        "the TRV regulates without it (the gateway reports ext_temp_missing). "
        "Enable the number in Home Assistant; it is written from the next "
        "reading on",
        self.device_name,
        entity_id,
    )


async def maybe_set_external_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Write the room temperature to the TRV's external temperature number.

    Looks for the ``external_temperature`` number on the TRV's device and
    writes the temperature, rounded to a tenth of a degree Celsius,
    converted into the unit the number publishes and held inside its range,
    unless the last write is recent and the temperature has not moved since.
    A room sensor on the TRV's own device is never written back into it.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass``, the TRV registry
        and the context the service call is made under.
    entity_id : str
        The TRV whose device carries the number.
    temperature : float
        The room temperature to write, in degrees Celsius.

    Returns
    -------
    bool
        True when the number was written, False when the write was not due,
        the TRV runs a calibration or direct valve control, the device has
        no enabled number, the room sensor is the TRV's own, the value is not
        a finite number, or the write failed.
    """
    try:
        trv = self.real_trvs[entity_id]
        if (
            configured_calibration_output(trv.advanced)
            is CalibrationOutput.DIRECT_VALVE_BASED
        ):
            _LOGGER.debug(
                "better_thermostat %s: Shelly BLU TRV external temperature not written: %s runs direct valve control",
                self.device_name,
                entity_id,
            )
            return False
        # The TRV corrects its own regulation with the external value. A
        # calibration in Better Thermostat corrects the setpoint for the same
        # bias, so the two would compensate twice.
        if (
            configured_calibration_mode(trv.advanced)
            is not CalibrationMode.NO_CALIBRATION
        ):
            _LOGGER.debug(
                "better_thermostat %s: Shelly BLU TRV external temperature not written: %s runs a calibration",
                self.device_name,
                entity_id,
            )
            return False
        entity_registry = er.async_get(self.hass)
        reg_entity = entity_registry.async_get(entity_id)
        if reg_entity is None or reg_entity.device_id is None:
            _LOGGER.debug(
                "better_thermostat %s: Shelly BLU TRV external temperature: no device for %s",
                self.device_name,
                entity_id,
            )
            return False
        device_id = reg_entity.device_id
        # A room sensor on the TRV's own device is the TRV's reading; writing
        # it back would feed the TRV its own measurement.
        room_sensor = entity_registry.async_get(self.sensor_entity_id)
        if room_sensor is not None and room_sensor.device_id == device_id:
            _LOGGER.debug(
                "better_thermostat %s: Shelly BLU TRV external temperature not written: room sensor %s belongs to %s",
                self.device_name,
                self.sensor_entity_id,
                entity_id,
            )
            return False
        target = find_keyed_device_entity(
            entity_registry,
            device_id,
            "number",
            _TK_EXTERNAL_TEMPERATURE,
            _EXTERNAL_TEMPERATURE_FRAGMENT,
        )
        if target is None:
            _report_missing_number(self, entity_id, trv.extra)
            return False
        trv.extra.pop("_shelly_blu_external_temperature_missing_logged", None)

        try:
            celsius = float(temperature)
        except TypeError, ValueError:
            _LOGGER.debug(
                "better_thermostat %s: Shelly BLU TRV maybe_set_external_temperature got non-float: %s",
                self.device_name,
                temperature,
            )
            return False
        if not math.isfinite(celsius):
            _LOGGER.debug(
                "better_thermostat %s: Shelly BLU TRV maybe_set_external_temperature got non-finite: %s",
                self.device_name,
                temperature,
            )
            return False

        target_state = self.hass.states.get(target)
        value = external_temperature_input_value(
            target_state, celsius, _INPUT_MIN_CELSIUS, _INPUT_MAX_CELSIUS
        )
        written_celsius = _to_celsius(target_state, value)
        now = monotonic()
        if not _write_is_due(
            trv.extra, written_celsius, _held_celsius(target_state), now
        ):
            _LOGGER.debug(
                "better_thermostat %s: Shelly BLU TRV external temperature on %s already holds %.2f (for %s)",
                self.device_name,
                target,
                written_celsius,
                entity_id,
            )
            return False

        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": target, "value": value},
            blocking=True,
            context=self.context,
        )
        trv.extra["_shelly_blu_external_temperature_celsius"] = written_celsius
        trv.extra["_shelly_blu_external_temperature_written_at"] = now
        _LOGGER.debug(
            "better_thermostat %s: set Shelly BLU TRV external_temperature=%s on %s (for %s)",
            self.device_name,
            value,
            target,
            entity_id,
        )
        return True
    except (
        HomeAssistantError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ) as ex:
        # The TRV did not take the value: the gateway is out of reach, the
        # TRV is asleep, or the integration is reloading. Nothing is
        # recorded, so the next reading or refresh writes again.
        _LOGGER.warning(
            "better_thermostat %s: Shelly BLU TRV external temperature write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    maybe_set_external_temperature = staticmethod(maybe_set_external_temperature)
    VALVE_MAINTENANCE_INTERVAL_HOURS = VALVE_MAINTENANCE_INTERVAL_HOURS
    initial_tweak = staticmethod(initial_tweak)
    trv_state_unknown_as_available = staticmethod(trv_state_unknown_as_available)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_EXTERNAL_TEMPERATURE_QUIRK: ExternalTemperatureQuirk = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
_MAINTENANCE_INTERVAL_QUIRK: MaintenanceIntervalQuirk = _Surface()
_UNKNOWN_STATE_QUIRK: UnknownStateQuirk = _Surface()
