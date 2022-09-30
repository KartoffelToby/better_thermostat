import asyncio
import logging
from .events.trv import convert_outbound_states
from datetime import datetime, timedelta
from homeassistant.components.climate.const import (
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    SERVICE_SET_HVAC_MODE,
)
from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import ATTR_TEMPERATURE

from .helpers import convert_to_float, round_to_hundredth_degree
from .weather import check_weather

_LOGGER = logging.getLogger(__name__)


async def control_queue(self):
    """The accutal control loop.
            Parameters
    ----------
    self :
            instance of better_thermostat

    Returns
    -------
    None
    """
    while True:
        if self.ignore_states:
            await asyncio.sleep(1)
            continue
        controls_to_process = await self.control_queue_task.get()
        if controls_to_process is not None:
            _LOGGER.debug(f"better_thermostat {self.name}: processing controls")
            await control_trv(controls_to_process)
            self.control_queue_task.task_done()


async def control_trv(self, force_mode_change: bool = False):
    """This is the main controller for the real TRV

    Parameters
    ----------
    self :
            instance of better_thermostat
    force_mode_change : bool
            forces a mode change regardless which mode the TRV is in.

    Returns
    -------
    None
    """
    if self.startup_running:
        return
    async with self._temp_lock:
        self.ignore_states = True

        # our own state is in self._bt_hvac_mode
        # the current target TRV state is in self._trvs_hvac_mode

        _current_TRV_mode = self.hass.states.get(self.heater_entity_id).state
        system_mode_change = False
        window_open_status_changed = False
        call_for_heat_updated = False

        if self._bt_hvac_mode == HVAC_MODE_OFF:
            _LOGGER.debug(
                f"better_thermostat {self.name}: control_trv: own mode is off, setting TRV mode to off"
            )
            self._trv_hvac_mode = HVAC_MODE_OFF

        else:
            call_for_heat_updated = check_weather(self)

            if self.call_for_heat is False:
                _LOGGER.debug(
                    f"better_thermostat {self.name}: control_trv: own mode is on, call for heat decision is false, setting TRV mode to off"
                )
                self._trv_hvac_mode = HVAC_MODE_OFF
            elif self.call_for_heat is True:
                _LOGGER.debug(
                    f"better_thermostat {self.name}: control_trv: own mode is on, call for heat decision is true, setting TRV mode to on"
                )
                self._trv_hvac_mode = HVAC_MODE_HEAT
            else:
                _LOGGER.debug(
                    f"better_thermostat {self.name}: control_trv: own mode is on, call for heat decision is unknown, setting TRV mode to on"
                )
                self._trv_hvac_mode = HVAC_MODE_HEAT

        if self.window_open is True or self.window_open is None:
            _LOGGER.debug(
                f"better_thermostat {self.name}: control_trv: own mode is on, window is open or status of window is unknown, setting TRV mode to off"
            )
            # if the window is open or the sensor is not available, we're done
            self._trv_hvac_mode = HVAC_MODE_OFF

        if self._trv_hvac_mode == HVAC_MODE_OFF:
            if _current_TRV_mode != HVAC_MODE_OFF:
                system_mode_change = True
                await set_trv_values(self, "hvac_mode", HVAC_MODE_OFF)
        else:
            remapped_states = convert_outbound_states(self, self._trv_hvac_mode)
            if not isinstance(remapped_states, dict):
                self.ignore_states = False
                return None
            converted_hvac_mode = remapped_states.get("system_mode") or None
            current_heating_setpoint = (
                remapped_states.get("current_heating_setpoint") or None
            )
            calibration = remapped_states.get("local_temperature_calibration") or None

            if converted_hvac_mode is not None:
                if _current_TRV_mode != converted_hvac_mode or force_mode_change:
                    _LOGGER.debug(
                        f"better_thermostat {self.name}: control_trv: current TRV mode: {_current_TRV_mode} new TRV mode: {converted_hvac_mode}"
                    )
                    system_mode_change = True
                    await set_trv_values(self, "hvac_mode", converted_hvac_mode)
            if current_heating_setpoint is not None:
                await set_trv_values(
                    self,
                    "temperature",
                    current_heating_setpoint,
                    hvac_mode=converted_hvac_mode,
                )
            if calibration is not None:
                await set_trv_values(self, "local_temperature_calibration", calibration)

        if system_mode_change:
            # block updates from the TRV for a short while to avoid sending too many system change commands
            await asyncio.sleep(5)

        if self._last_window_state != self.window_open or self._init:
            self._last_window_state = self.window_open
            window_open_status_changed = True

        if call_for_heat_updated or system_mode_change or window_open_status_changed:
            self.async_write_ha_state()
        self.ignore_states = False
        self._init = False


async def set_target_temperature(self, **kwargs):
    """Update the target temperature of the thermostat

    Parameters
    ----------
    self :
            self instance of better_thermostat
    kwargs :
            Piped attributes from HA.

    Returns
    -------
    None
    """
    if (
        self.homaticip
        and (self.last_change + timedelta(seconds=10)).timestamp()
        > datetime.now().timestamp()
    ):
        _LOGGER.info(
            f"better_thermostat {self.name}: skip controlling.set_target_temperature because of homaticip throttling"
        )
        return

    _new_setpoint = convert_to_float(
        kwargs.get(ATTR_TEMPERATURE), self.name, "controlling.set_target_temperature()"
    )
    if _new_setpoint is None:
        _LOGGER.info(
            f"better_thermostat {self.name}: received a new setpoint from HA, but temperature attribute was not set, ignoring"
        )
        return
    _LOGGER.info(
        f"better_thermostat {self.name}: received a new setpoint from HA: {_new_setpoint}"
    )
    self._target_temp = _new_setpoint
    self.async_write_ha_state()
    await self.control_queue_task.put(self)


async def set_hvac_mode(self, hvac_mode):
    """Set the HVAC mode for the thermostat

    Parameters
    ----------
    self :
            self instance of better_thermostat
    hvac_mode :
            The new HVAC mode

    Returns
    -------
    None
    """
    if (
        self.homaticip
        and (self.last_change + timedelta(seconds=10)).timestamp()
        > datetime.now().timestamp()
    ):
        _LOGGER.info(
            f"better_thermostat {self.name}: skip controlling.set_hvac_mode because of homaticip throttling"
        )
        return
    if hvac_mode in (HVAC_MODE_HEAT, HVAC_MODE_OFF):
        _LOGGER.info(
            f"better_thermostat {self.name}: received new HVAC mode {hvac_mode} from HA"
        )
        self._bt_hvac_mode = hvac_mode
    else:
        _LOGGER.error(
            "better_thermostat %s: Unsupported hvac_mode %s", self.name, hvac_mode
        )
    self.async_write_ha_state()
    await self.control_queue_task.put(self)


async def set_trv_values(self, key, value, hvac_mode=None):
    """Do necessary actions to set the TRV values.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    key :
            sort of service call for HA to update the TRV
    value :
            the value to parse to the service call

    Returns
    -------
    None
    """

    if hvac_mode is HVAC_MODE_OFF:
        if HVAC_MODE_OFF not in self._TRV_SUPPORTED_HVAC_MODES:
            _LOGGER.debug(
                f"better_thermostat {self.name}: set_trv_values: TRV does not support hvac_mode off, sending just heat"
            )
            hvac_mode = HVAC_MODE_HEAT

    if key == "temperature":
        if hvac_mode is None:
            _LOGGER.error(
                f"better_thermostat {self.name}: set_trv_values() called for a temperature change without a specified hvac mode"
            )
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": self.heater_entity_id, "temperature": value},
            blocking=True,
        )
    elif key == "hvac_mode":
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self.heater_entity_id, "hvac_mode": value},
            blocking=True,
        )
    elif key == "local_temperature_calibration":
        value = round_to_hundredth_degree(value)
        current_calibration = self.hass.states.get(
            self.local_temperature_calibration_entity
        ).state
        if current_calibration != value and (
            (self._last_calibration + timedelta(minutes=5)).timestamp()
            < datetime.now().timestamp()
        ):
            max_calibration = self.hass.states.get(
                self.local_temperature_calibration_entity
            ).attributes.get("max", 127)
            min_calibration = self.hass.states.get(
                self.local_temperature_calibration_entity
            ).attributes.get("min", -128)
            if value > max_calibration:
                value = max_calibration
            if value < min_calibration:
                value = min_calibration
            await self.hass.services.async_call(
                "number",
                SERVICE_SET_VALUE,
                {
                    "entity_id": self.local_temperature_calibration_entity,
                    "value": value,
                },
                blocking=True,
            )
            self._last_calibration = datetime.now()
        else:
            _LOGGER.debug(
                f"better_thermostat {self.name}: set_trv_values: skipping local calibration because of throttling"
            )
    elif key == "valve_position":
        await self.hass.services.async_call(
            "number",
            SERVICE_SET_VALUE,
            {"entity_id": self.valve_position_entity, "value": value},
            blocking=True,
        )
    else:
        _LOGGER.error(
            "better_thermostat %s: set_trv_values() called with unsupported key %s",
            self.name,
            key,
        )
    _LOGGER.info(
        "better_thermostat %s: send new %s to TRV, value: '%s'", self.name, key, value
    )
    self.last_change = datetime.now()
    await asyncio.sleep(5)


async def trv_valve_maintenance(self):
    """Maintenance of the TRV valve.

    Returns
    -------
    None
    """

    _LOGGER.info("better_thermostat %s: maintenance started", self.name)

    self.ignore_states = True

    if self.model == "TS0601_thermostat":
        _LOGGER.debug(
            "better_thermostat %s: maintenance will run TS0601_thermostat variant of cycle",
            self.name,
        )

        # get current HVAC mode from HA
        try:
            _last_hvac_mode = self.hass.states.get(self.heater_entity_id).state
        except AttributeError:
            _LOGGER.error(
                "better_thermostat %s: Could not load current HVAC mode", self.name
            )
            self.ignore_states = False
            return

        _i = 0
        _retry_limit_reached = False

        while True:
            # close valve
            _set_HVAC_mode_retry = 0
            while not self._last_reported_valve_position == 0:
                # send close valve command and wait for the valve to close
                await set_trv_values(self, "hvac_mode", "off")
                # wait for an update by the TRV on the valve position
                await self._last_reported_valve_position_update_wait_lock.acquire()
                if (
                    not self._last_reported_valve_position == 0
                    and _set_HVAC_mode_retry < 3
                ):
                    _set_HVAC_mode_retry += 1
                    continue
                elif _set_HVAC_mode_retry == 3:
                    _LOGGER.error(
                        "better_thermostat %s: maintenance could not close valve after 3 retries",
                        self.name,
                    )
                    _retry_limit_reached = True
                    break
                # wait 60 seconds to not overheat the motor
                await asyncio.sleep(60)

            if _retry_limit_reached:
                _LOGGER.error(
                    "better_thermostat %s: maintenance was aborted prematurely due to errors",
                    self.name,
                )
                break

            # end loop after 3 opening cycles
            elif _i > 3:
                _LOGGER.info("better_thermostat %s: maintenance completed", self.name)
                break

            # open valve
            _set_HVAC_mode_retry = 0
            while not self._last_reported_valve_position == 100:
                # send open valve command and wait for the valve to open
                await self.hass.services.async_call(
                    "climate",
                    SERVICE_SET_HVAC_MODE,
                    {"entity_id": self.heater_entity_id, "hvac_mode": "heat"},
                    blocking=True,
                )
                await self._last_reported_valve_position_update_wait_lock.acquire()
                if (
                    not self._last_reported_valve_position == 0
                    and _set_HVAC_mode_retry < 3
                ):
                    _set_HVAC_mode_retry += 1
                    continue
                elif _set_HVAC_mode_retry == 3:
                    _LOGGER.error(
                        "better_thermostat %s: maintenance could not open valve after 3 retries",
                        self.name,
                    )
                    _retry_limit_reached = True
                    break
                # wait 60 seconds to not overheat the motor
                await asyncio.sleep(60)

            if _retry_limit_reached:
                _LOGGER.error(
                    "better_thermostat %s: maintenance was aborted prematurely due to errors",
                    self.name,
                )
                break

            _i += 1

        # returning the TRV to the previous HVAC mode
        await self.hass.services.async_call(
            "climate",
            SERVICE_SET_HVAC_MODE,
            {"entity_id": self.heater_entity_id, "hvac_mode": _last_hvac_mode},
            blocking=True,
        )
        # give the TRV time to process the mode change and report back to HA
        await asyncio.sleep(120)

    else:

        valve_position_available = False
        # check if there's a valve_position field
        try:
            self.hass.states.get(self.heater_entity_id).attributes.get("valve_position")
            valve_position_available = True
        except AttributeError:
            pass

        if valve_position_available:
            for position in (255, 0, 255, 0):
                await set_trv_values(self, "valve_position", position)
                await asyncio.sleep(60)
        else:
            for temperature in (30, 5, 30, 5):
                await set_trv_values(self, "temperature", temperature)
                await asyncio.sleep(60)

    self.ignore_states = False

    # restarting normal heating control immediately
    await self.control_queue_task.put(self)
