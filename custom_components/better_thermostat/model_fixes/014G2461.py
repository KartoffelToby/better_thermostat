"""Quirks for the Danfoss Ally TRV and its white-label versions.

Zigbee2MQTT drives the Danfoss Ally (014G2461, 014G2463), the Popp 701721
and the Hive UK7004240 through one definition. Each of them takes a room
temperature from outside through ``external_measured_room_sensor``, which
this module keeps current with the room temperature Better Thermostat
regulates on.

The device forgets that value after a while and falls back to its own
sensor: after 3 hours in Auto Offset Mode (``radiator_covered`` off) and
after 35 minutes in Room Sensor Mode (``radiator_covered`` on). It asks not
to be written more often than every 30 minutes, or every 5 minutes in Room
Sensor Mode, unless the temperature moved by at least 0.1 °C. The room
temperature is offered on every reading Better Thermostat accepts and again
every 30 minutes. In Auto Offset Mode a write is due when the temperature
moved by 0.1 °C, when 30 minutes have passed since the last write, or when
the device no longer holds the value written last. Room Sensor Mode takes
every offer, since a skipped one would leave the input unwritten for up to
35 minutes.

The device corrects its own regulation with that value, so it is written
only to a TRV Better Thermostat passes the target to unchanged; a
calibration on top would compensate the same bias a second time.
Everything else stays as the default quirks have it.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import math
from time import monotonic
from typing import Final

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_ON, UnitOfTemperature
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
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.helpers import (
    configured_calibration_mode,
    find_device_entity,
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

# Names of the number that takes the room temperature and of the switch that
# selects how the device uses it, as Zigbee2MQTT exposes them.
_EXTERNAL_SENSOR_KEYWORDS: Final = (
    "external_measured_room_sensor",
    "external measured room sensor",
)
_RADIATOR_COVERED_KEYWORDS: Final = ("radiator_covered", "radiator covered")

# Zigbee2MQTT publishes the input as the raw attribute, in hundredths of a
# degree Celsius from -8000 to 3500, where -8000 means no value. An input
# whose maximum is above this many units counts in hundredths; one at or
# below it counts in degrees of the unit it publishes.
_HUNDREDTHS_ABOVE: Final = 100.0
_HUNDREDTHS_PER_DEGREE: Final = 100

# Range of the input, in degrees Celsius, for one that publishes no bounds.
_INPUT_MIN: Final = -79.99
_INPUT_MAX: Final = 35.0

# Smallest change of the room temperature worth a write of its own.
_SIGNIFICANT_CHANGE_KELVIN: Final = 0.1

# Seconds between two writes of an unchanged temperature in Auto Offset Mode.
# Room Sensor Mode writes on every offer: its timeout is barely longer than
# the 30-minute refresh offer, so skipping one offer would let it run out.
_AUTO_OFFSET_REFRESH_SECONDS: Final = 30 * 60
_ROOM_SENSOR_REFRESH_SECONDS: Final = 0

# How far the value the device reports may sit from the one written, in
# degrees Celsius, before it counts as a different value. Half the coarsest
# resolution a written value carries.
_HELD_VALUE_TOLERANCE_KELVIN: Final = 0.05

# Seconds a write is given before the value the device reports counts as its
# answer to it. Zigbee2MQTT publishes the new value within a second or two;
# until then the state still shows the value from before the write.
_WRITE_ECHO_GRACE_SECONDS: Final = 60


def _counts_in_hundredths(attributes: Mapping[str, object]) -> bool:
    """Answer whether the input counts in hundredths of a degree Celsius.

    Parameters
    ----------
    attributes : Mapping[str, object]
        Attributes of the input's state.

    Returns
    -------
    bool
        True unless the input publishes a maximum a temperature in degrees
        fits under. An input without a readable maximum is taken for the
        raw attribute Zigbee2MQTT publishes.
    """
    try:
        return float(str(attributes["max"])) > _HUNDREDTHS_ABOVE
    except KeyError, TypeError, ValueError:
        return True


def _degree_unit(attributes: Mapping[str, object]) -> str:
    """Return the temperature unit an input counting in degrees publishes.

    An input that declares no temperature unit is taken to count in Celsius.
    """
    declared_unit = attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if (
        isinstance(declared_unit, str)
        and declared_unit in TemperatureConverter.VALID_UNITS
    ):
        return declared_unit
    return UnitOfTemperature.CELSIUS


def _input_value(state: State | None, celsius: float) -> float:
    """Return the value the input takes for ``celsius``.

    The value is held inside the range the input publishes, in the input's
    own scale: hundredths of a degree Celsius, rounded to a whole number, or
    degrees of the unit it publishes, from a reading rounded to a tenth of a
    degree Celsius.

    Parameters
    ----------
    state : State or None
        State of the input, or None when it has none.
    celsius : float
        The room temperature in degrees Celsius.

    Returns
    -------
    float
        The value to write.
    """
    attributes: Mapping[str, object] = state.attributes if state is not None else {}
    if _counts_in_hundredths(attributes):

        def published(degrees: float) -> float:
            return float(round(degrees * _HUNDREDTHS_PER_DEGREE))

        value = published(celsius)
    else:
        unit = _degree_unit(attributes)

        def published(degrees: float) -> float:
            return TemperatureConverter.convert(
                degrees, UnitOfTemperature.CELSIUS, unit
            )

        value = published(round(celsius, 1))

    def bound(name: str, fallback_celsius: float) -> float:
        try:
            return float(str(attributes[name]))
        except KeyError, TypeError, ValueError:
            return published(fallback_celsius)

    # The floor never reaches the input's own minimum: there it stands for
    # no value, which would drop the temperature instead of writing it.
    floor = max(bound("min", _INPUT_MIN), published(_INPUT_MIN))
    return max(floor, min(bound("max", _INPUT_MAX), value))


def _input_celsius(state: State | None, value: float) -> float:
    """Return the temperature in degrees Celsius an input value stands for.

    The inverse of the scaling :func:`_input_value` applies.
    """
    attributes: Mapping[str, object] = state.attributes if state is not None else {}
    if _counts_in_hundredths(attributes):
        return value / _HUNDREDTHS_PER_DEGREE
    return TemperatureConverter.convert(
        value, _degree_unit(attributes), UnitOfTemperature.CELSIUS
    )


def _held_celsius(state: State | None) -> float | None:
    """Return the temperature the input holds now, or None when it reports none."""
    if not state_is_live(state):
        return None
    try:
        return _input_celsius(state, float(state.state))
    except TypeError, ValueError:
        return None


def _is_in_room_sensor_mode(
    self: ModelFixHost, entity_registry: er.EntityRegistry, device_id: str
) -> bool:
    """Answer whether the device regulates on the external value alone.

    That is Room Sensor Mode, which ``radiator_covered`` switches on. A
    device without the switch is taken to be in Auto Offset Mode, the
    device's default. A switch that reports no state may hide either mode,
    so it is taken for Room Sensor Mode, whose shorter timeout the refresh
    then keeps.
    """
    switch = find_device_entity(
        entity_registry, device_id, ["switch"], _RADIATOR_COVERED_KEYWORDS
    )
    if switch is None:
        return False
    state = self.hass.states.get(switch)
    return not state_is_live(state) or state.state == STATE_ON


def _write_is_due(
    scratchpad: QuirkScratchpad,
    celsius: float,
    held_celsius: float | None,
    now: float,
    refresh_seconds: float,
) -> bool:
    """Answer whether the room temperature is to be written to the device.

    Parameters
    ----------
    scratchpad : QuirkScratchpad
        Write state of the TRV, holding the last write if there was one.
        Its value is the one the input took, in degrees Celsius.
    celsius : float
        The room temperature to write, in degrees Celsius.
    held_celsius : float or None
        The temperature the device reports holding, or None when it reports
        none.
    now : float
        Monotonic clock reading, in seconds.
    refresh_seconds : float
        Seconds after which an unchanged temperature is written again.

    Returns
    -------
    bool
        True when nothing was written yet, the device still holds a
        different value from the one written once the write has had time to
        land, the temperature moved by at least 0.1 °C, or
        ``refresh_seconds`` has passed since the last write.
    """
    written_celsius = scratchpad.get("_danfoss_external_sensor_celsius")
    written_at = scratchpad.get("_danfoss_external_sensor_written_at")
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
    return now - written_at >= refresh_seconds


async def maybe_set_external_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Write the room temperature to the device's external room sensor input.

    Looks for the ``external_measured_room_sensor`` number on the TRV's
    device and writes the temperature in the input's scale, unless the last
    write is recent and the temperature has not moved since. A room sensor
    on the TRV's own device is never written back into it.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass``, the TRV registry
        and the context the service call is made under.
    entity_id : str
        The TRV whose device carries the input.
    temperature : float
        The room temperature to write, in degrees Celsius.

    Returns
    -------
    bool
        True when the input was written, False when the write was not due,
        the device has no such input, the room sensor is the TRV's own, the
        value is not a finite number, or the device refused it.
    """
    try:
        trv = self.real_trvs[entity_id]
        # The device corrects its own regulation with the external value. A
        # calibration in Better Thermostat corrects the setpoint for the same
        # bias, so the two would compensate twice; the value is only written
        # where Better Thermostat passes the target through unchanged.
        if (
            configured_calibration_mode(trv.advanced)
            is not CalibrationMode.NO_CALIBRATION
        ):
            _LOGGER.debug(
                "better_thermostat %s: Danfoss Ally external room sensor not written: %s runs a calibration",
                self.device_name,
                entity_id,
            )
            return False
        entity_registry = er.async_get(self.hass)
        reg_entity = entity_registry.async_get(entity_id)
        if reg_entity is None or reg_entity.device_id is None:
            _LOGGER.debug(
                "better_thermostat %s: Danfoss Ally external room sensor: no device for %s",
                self.device_name,
                entity_id,
            )
            return False
        device_id = reg_entity.device_id
        # A room sensor on the TRV's own device is the TRV's reading; writing
        # it back would feed the device its own measurement.
        room_sensor = entity_registry.async_get(self.sensor_entity_id)
        if room_sensor is not None and room_sensor.device_id == device_id:
            _LOGGER.debug(
                "better_thermostat %s: Danfoss Ally external room sensor not written: room sensor %s belongs to %s",
                self.device_name,
                self.sensor_entity_id,
                entity_id,
            )
            return False
        target = find_device_entity(
            entity_registry, device_id, ["number"], _EXTERNAL_SENSOR_KEYWORDS
        )
        if target is None:
            _LOGGER.debug(
                "better_thermostat %s: Danfoss Ally external_measured_room_sensor number entity not found for %s",
                self.device_name,
                entity_id,
            )
            return False

        try:
            celsius = float(temperature)
        except TypeError, ValueError:
            _LOGGER.debug(
                "better_thermostat %s: Danfoss Ally maybe_set_external_temperature got non-float: %s",
                self.device_name,
                temperature,
            )
            return False
        if not math.isfinite(celsius):
            _LOGGER.debug(
                "better_thermostat %s: Danfoss Ally maybe_set_external_temperature got non-finite: %s",
                self.device_name,
                temperature,
            )
            return False

        target_state = self.hass.states.get(target)
        value = _input_value(target_state, celsius)
        written_celsius = _input_celsius(target_state, value)
        refresh_seconds = (
            _ROOM_SENSOR_REFRESH_SECONDS
            if _is_in_room_sensor_mode(self, entity_registry, device_id)
            else _AUTO_OFFSET_REFRESH_SECONDS
        )
        now = monotonic()
        if not _write_is_due(
            trv.extra,
            written_celsius,
            _held_celsius(target_state),
            now,
            refresh_seconds,
        ):
            _LOGGER.debug(
                "better_thermostat %s: Danfoss Ally external room sensor on %s already holds %.2f (for %s)",
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
        trv.extra["_danfoss_external_sensor_celsius"] = written_celsius
        trv.extra["_danfoss_external_sensor_written_at"] = now
        _LOGGER.debug(
            "better_thermostat %s: set Danfoss Ally external_measured_room_sensor=%s on %s (for %s)",
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
        # The device did not take the value: it is asleep, out of reach, or
        # its integration is reloading. Nothing is recorded, so the next
        # reading or refresh writes again.
        _LOGGER.warning(
            "better_thermostat %s: Danfoss Ally external room sensor write for %s failed: %s",
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
