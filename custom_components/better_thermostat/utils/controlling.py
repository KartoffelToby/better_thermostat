import asyncio
import logging

from .bridge import (
    set_offset,
    get_current_offset,
    get_offset_steps,
    set_temperature,
    set_hvac_mode,
)
from ..events.trv import convert_outbound_states
from homeassistant.components.climate.const import HVAC_MODE_OFF

from .helpers import convert_to_float, calibration_round

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
        if self.ignore_states or self.startup_running:
            await asyncio.sleep(1)
            continue
        else:
            controls_to_process = await self.control_queue_task.get()
            if controls_to_process is not None:
                result = await control_trv(self)
                if result is False:
                    self.control_queue_task.put(self)
                self.control_queue_task.task_done()


async def control_trv(self):
    """This is the main controller for the real TRV

    Parameters
    ----------
    self :
            instance of better_thermostat

    Returns
    -------
    None
    """
    async with self._temp_lock:
        self.ignore_states = True
        _trv = self.hass.states.get(self.heater_entity_id)
        _current_TRV_mode = _trv.state
        _current_set_temperature = convert_to_float(
            str(_trv.attributes.get("temperature", None)), self.name, "controlling()"
        )

        _hvac_mode_send = HVAC_MODE_OFF

        _remapped_states = convert_outbound_states(self, self._bt_hvac_mode)
        _converted_hvac_mode = _remapped_states.get("system_mode", None)
        _temperature = _remapped_states.get("temperature", None)
        _calibration = _remapped_states.get("local_temperature_calibration", None)

        if not isinstance(_remapped_states, dict):
            self.ignore_states = False
            return False

        if self.call_for_heat is True:
            _hvac_mode_send = _converted_hvac_mode

            if self.window_open is True:
                # if the window is open or the sensor is not available, we're done
                self._last_main_hvac_mode = _hvac_mode_send
                _hvac_mode_send = HVAC_MODE_OFF
                self._last_states["last_window_open"] = True
                _LOGGER.debug(
                    f"better_thermostat {self.name}: control_trv: window is open or status of window is unknown, setting window open"
                )
            elif (
                self.window_open is False
                and self._last_states.get("last_window_open", False) is True
            ):
                _hvac_mode_send = self._last_main_hvac_mode
                self._last_states["last_window_open"] = False
                _LOGGER.debug(
                    f"better_thermostat {self.name}: control_trv: window is closed, setting window closed restoring mode: {_hvac_mode_send}"
                )

        if (
            _temperature is not None
            and self._bt_hvac_mode != HVAC_MODE_OFF
            and self.window_open is False
        ):
            if _temperature != _current_set_temperature:
                old = self._last_states.get("last_target_temp", "?")
                _LOGGER.debug(
                    f"better_thermostat {self.name}: TO TRV set_temperature: from: {old} to: {_temperature}"
                )
                await set_temperature(self, _temperature)
                self._last_states["last_target_temp"] = _temperature
                if self._target_temp_received is True:
                    self._target_temp_received = False
                    asyncio.create_task(check_target_temperature(self))

        if (
            _calibration is not None
            and self._bt_hvac_mode != HVAC_MODE_OFF
            and self.window_open is False
        ):
            old_calibration = await get_current_offset(self)
            step_calibration = await get_offset_steps(self)
            current_calibration = convert_to_float(
                str(old_calibration), self.name, "controlling()"
            )
            if step_calibration.is_integer():
                _calibration = calibration_round(
                    float(str(format(float(_calibration), ".1f")))
                )
            else:
                _calibration = float(str(format(float(_calibration), ".1f")))

            old = self._last_states.get("last_calibration", current_calibration)
            if self._calibration_received is True and old != _calibration:
                _LOGGER.debug(
                    f"better_thermostat {self.name}: TO TRV set_local_temperature_calibration: from: {old} to: {_calibration}"
                )
                await set_offset(self, _calibration)
                self._last_states["last_calibration"] = _calibration
                self._calibration_received = False
                asyncio.create_task(check_offset(self))

        if _hvac_mode_send is not None:
            if (
                _hvac_mode_send != _current_TRV_mode
                or self._calibration_received is False
            ):
                if self._calibration_received is True:
                    _LOGGER.debug(
                        f"better_thermostat {self.name}: TO TRV set_hvac_mode: from: {_current_TRV_mode} to: {_hvac_mode_send}"
                    )
                await set_hvac_mode(self, _hvac_mode_send)
                self._last_states["last_hvac_mode"] = _hvac_mode_send
                if self._system_mode_received is True:
                    self._system_mode_received = False
                    asyncio.create_task(check_system_mode(self))

        self.ignore_states = False
        return True


async def check_system_mode(self):
    """check system mode"""
    _timeout = 0
    _current_system_mode = self._last_states.get("last_hvac_mode", None)
    while True:
        if _current_system_mode == self._trv_hvac_mode:
            _timeout = 0
            break
        if _timeout > 20:
            _LOGGER.warning(f"better_thermostat {self.name}: check_mode: timeout")
            await self.control_queue_task.put(self)
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    self._system_mode_received = True
    _LOGGER.debug(f"better_thermostat {self.name}: hvac mode accepted")
    return True


async def check_target_temperature(self):
    """Check if target temperature is reached."""
    _timeout = 0
    while True:
        _current_set_temperature = convert_to_float(
            str(
                self.hass.states.get(self.heater_entity_id).attributes.get(
                    "temperature", None
                )
            ),
            self.name,
            "check_target()",
        )
        if self._last_states["last_target_temp"] == _current_set_temperature:
            _timeout = 0
            break
        if _timeout > 20:
            _LOGGER.warning(
                f"better_thermostat {self.name}: TO TRV set_temperature: timeout"
            )
            await self.control_queue_task.put(self)
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    self._target_temp_received = True
    _LOGGER.debug(
        f"better_thermostat {self.name}: TO TRV set_temperature: target accepted"
    )
    return True


async def check_offset(self):
    """Check if offset is accepted by TRV."""
    _timeout = 0
    _current_local_temperature = self._TRV_current_temp
    while True:
        if _timeout > 60:
            _LOGGER.warning(
                f"better_thermostat {self.name}: check_offset: timeout while waiting for offset to be accepted by TRV"
            )
            await self.control_queue_task.put(self)
            break
        if _current_local_temperature != self._TRV_current_temp:
            break
        await asyncio.sleep(1)
        _timeout += 1

    self._calibration_received = True
    _LOGGER.debug(f"better_thermostat {self.name}: calibration accepted by TRV")

    return True
