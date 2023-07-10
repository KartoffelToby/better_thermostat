import asyncio
import logging

from custom_components.better_thermostat.utils.model_quirks import (
    override_set_hvac_mode,
    override_set_temperature,
)

from .bridge import (
    set_offset,
    get_current_offset,
    get_offset_steps,
    set_temperature,
    set_hvac_mode,
)
from ..events.trv import convert_outbound_states
from homeassistant.components.climate.const import HVACMode

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
                self.ignore_states = True
                result = True
                for trv in self.real_trvs.keys():
                    try:
                        _temp = await control_trv(self, trv)
                        if _temp is False:
                            result = False
                    except Exception:
                        _LOGGER.exception(
                            "better_thermostat %s: ERROR controlling: %s",
                            self.name,
                            trv,
                        )
                        result = False

                # Retry task if some TRVs failed. Discard the task if the queue is full
                # to avoid blocking and therefore deadlocking this function.
                if result is False:
                    try:
                        self.control_queue_task.put_nowait(self)
                    except asyncio.QueueFull:
                        _LOGGER.debug(
                            "better_thermostat %s: control queue is full, discarding task"
                        )

                self.control_queue_task.task_done()
                self.ignore_states = False


async def control_trv(self, heater_entity_id=None):
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
        self.real_trvs[heater_entity_id]["ignore_trv_states"] = True
        await self.calculate_heating_power()
        _trv = self.hass.states.get(heater_entity_id)
        _current_set_temperature = convert_to_float(
            str(_trv.attributes.get("temperature", None)), self.name, "controlling()"
        )

        _remapped_states = convert_outbound_states(
            self, heater_entity_id, self.bt_hvac_mode
        )
        if not isinstance(_remapped_states, dict):
            _LOGGER.debug(
                f"better_thermostat {self.name}: ERROR {heater_entity_id} {_remapped_states}"
            )
            await asyncio.sleep(10)
            self.ignore_states = False
            self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
            return False

        _temperature = _remapped_states.get("temperature", None)
        _calibration = _remapped_states.get("local_temperature_calibration", None)

        _new_hvac_mode = handle_window_open(self, _remapped_states)

        # if we don't need ot heat, we force HVACMode to be off
        if self.call_for_heat is False:
            _new_hvac_mode = HVACMode.OFF

        # Manage TRVs with no HVACMode.OFF
        _no_off_system_mode = (
            HVACMode.OFF not in self.real_trvs[heater_entity_id]["hvac_modes"]
        ) or (
            self.real_trvs[heater_entity_id]["advanced"].get(
                "no_off_system_mode", False
            )
            is True
        )
        if _no_off_system_mode is True and _new_hvac_mode == HVACMode.OFF:
            _min_temp = self.real_trvs[heater_entity_id]["min_temp"]
            _LOGGER.debug(
                f"better_thermostat {self.name}: sending {_min_temp}°C to the TRV because this device has no system mode off and heater should be off"
            )
            _temperature = _min_temp

        # send new HVAC mode to TRV, if it changed
        if (
            _new_hvac_mode is not None
            and _new_hvac_mode != _trv.state
            and (
                (_no_off_system_mode is True and _new_hvac_mode != HVACMode.OFF)
                or (_no_off_system_mode is False)
            )
        ):
            _LOGGER.debug(
                f"better_thermostat {self.name}: TO TRV set_hvac_mode: {heater_entity_id} from: {_trv.state} to: {_new_hvac_mode}"
            )
            self.real_trvs[heater_entity_id]["last_hvac_mode"] = _new_hvac_mode
            _tvr_has_quirk = await override_set_hvac_mode(
                self, heater_entity_id, _new_hvac_mode
            )
            if _tvr_has_quirk is False:
                await set_hvac_mode(self, heater_entity_id, _new_hvac_mode)
            if self.real_trvs[heater_entity_id]["system_mode_received"] is True:
                self.real_trvs[heater_entity_id]["system_mode_received"] = False
                asyncio.create_task(check_system_mode(self, heater_entity_id))

        # set new calibration offset
        if _calibration is not None and _new_hvac_mode != HVACMode.OFF:
            old_calibration = await get_current_offset(self, heater_entity_id)
            step_calibration = await get_offset_steps(self, heater_entity_id)
            if old_calibration is None or step_calibration is None:
                _LOGGER.error(
                    "better_thermostat %s: calibration fatal error %s",
                    self.name,
                    heater_entity_id,
                )
                # this should not be before, set_hvac_mode (because if it fails, the new hvac mode will never be sent)
                self.ignore_states = False
                self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
                return True
            current_calibration = convert_to_float(
                str(old_calibration), self.name, "controlling()"
            )
            if step_calibration.is_integer():
                _calibration = calibration_round(
                    float(str(format(float(_calibration), ".1f")))
                )
            else:
                _calibration = float(str(format(float(_calibration), ".1f")))

            old = self.real_trvs[heater_entity_id].get(
                "last_calibration", current_calibration
            )

            if self.real_trvs[heater_entity_id][
                "calibration_received"
            ] is True and float(old) != float(_calibration):
                _LOGGER.debug(
                    f"better_thermostat {self.name}: TO TRV set_local_temperature_calibration: {heater_entity_id} from: {old} to: {_calibration}"
                )
                await set_offset(self, heater_entity_id, _calibration)
                self.real_trvs[heater_entity_id]["calibration_received"] = False

        # set new target temperature
        if (
            _temperature is not None
            and _new_hvac_mode != HVACMode.OFF
            or _no_off_system_mode
        ):
            if _temperature != _current_set_temperature:
                old = self.real_trvs[heater_entity_id].get("last_temperature", "?")
                _LOGGER.debug(
                    f"better_thermostat {self.name}: TO TRV set_temperature: {heater_entity_id} from: {old} to: {_temperature}"
                )
                self.real_trvs[heater_entity_id]["last_temperature"] = _temperature
                _tvr_has_quirk = await override_set_temperature(
                    self, heater_entity_id, _temperature
                )
                if _tvr_has_quirk is False:
                    await set_temperature(self, heater_entity_id, _temperature)
                if self.real_trvs[heater_entity_id]["target_temp_received"] is True:
                    self.real_trvs[heater_entity_id]["target_temp_received"] = False
                    asyncio.create_task(
                        check_target_temperature(self, heater_entity_id)
                    )

        await asyncio.sleep(3)
        self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
        return True


def handle_window_open(self, _remapped_states):
    """handle window open"""
    _converted_hvac_mode = _remapped_states.get("system_mode", None)
    _hvac_mode_send = _converted_hvac_mode

    if self.window_open is True and self.last_window_state is False:
        # if the window is open or the sensor is not available, we're done
        self.last_main_hvac_mode = _hvac_mode_send
        _hvac_mode_send = HVACMode.OFF
        self.last_window_state = True
        _LOGGER.debug(
            f"better_thermostat {self.name}: control_trv: window is open or status of window is unknown, setting window open"
        )
    elif self.window_open is False and self.last_window_state is True:
        _hvac_mode_send = self.last_main_hvac_mode
        self.last_window_state = False
        _LOGGER.debug(
            f"better_thermostat {self.name}: control_trv: window is closed, setting window closed restoring mode: {_hvac_mode_send}"
        )

    # Force off on window open
    if self.window_open is True:
        _hvac_mode_send = HVACMode.OFF

    return _hvac_mode_send


async def check_system_mode(self, heater_entity_id=None):
    """check system mode"""
    _timeout = 0
    _real_trv = self.real_trvs[heater_entity_id]
    while _real_trv["hvac_mode"] != _real_trv["last_hvac_mode"]:
        if _timeout > 360:
            _LOGGER.debug(
                f"better_thermostat {self.name}: {heater_entity_id} the real TRV did not respond to the system mode change"
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)
    _real_trv["system_mode_received"] = True
    return True


async def check_target_temperature(self, heater_entity_id=None):
    """Check if target temperature is reached."""
    _timeout = 0
    _real_trv = self.real_trvs[heater_entity_id]
    while True:
        _current_set_temperature = convert_to_float(
            str(
                self.hass.states.get(heater_entity_id).attributes.get(
                    "temperature", None
                )
            ),
            self.name,
            "check_target_temperature()",
        )
        if _timeout == 0:
            _LOGGER.debug(
                f"better_thermostat {self.name}: {heater_entity_id} / check_target_temp / _last: {_real_trv['last_temperature']} - _current: {_current_set_temperature}"
            )
        if (
            _current_set_temperature is None
            or _real_trv["last_temperature"] == _current_set_temperature
        ):
            _timeout = 0
            break
        if _timeout > 120:
            _LOGGER.debug(
                f"better_thermostat {self.name}: {heater_entity_id} the real TRV did not respond to the target temperature change"
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)

    _real_trv["target_temp_received"] = True
    return True
