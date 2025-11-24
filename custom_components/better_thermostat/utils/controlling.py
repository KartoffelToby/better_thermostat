"""Controlling module for Better Thermostat."""

import asyncio
import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.better_thermostat.model_fixes.model_quirks import (
    override_set_hvac_mode,
)

from custom_components.better_thermostat.adapters.delegate import (
    set_offset,
    get_current_offset,
    set_temperature,
    set_hvac_mode,
    set_valve,
)

from custom_components.better_thermostat.events.trv import convert_outbound_states

from custom_components.better_thermostat.utils.helpers import convert_to_float

from custom_components.better_thermostat.utils.const import CalibrationMode


_LOGGER = logging.getLogger(__name__)


class TaskManager:
    """Task manager for Better Thermostat."""

    def __init__(self):
        """Initialize the task manager."""
        self.tasks = set()

    def create_task(self, coro):
        """Create a task."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


async def control_queue(self):
    """Control the queue.

    Parameters
    ----------
    self :
            instance of better_thermostat

    Returns
    -------
    None
    """
    if not hasattr(self, "task_manager"):
        self.task_manager = TaskManager()

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
                            self.device_name,
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
    """Control the TRV.

    Parameters
    ----------
    self :
            instance of better_thermostat

    Returns
    -------
    None
    """
    if not hasattr(self, "task_manager"):
        self.task_manager = TaskManager()

    async with self._temp_lock:
        self.real_trvs[heater_entity_id]["ignore_trv_states"] = True
        # Formerly update_hvac_action(self) (removed / centralized in climate entity)
        try:
            # Preserve old action for change detection if attributes exist
            if hasattr(self, "attr_hvac_action"):
                self.old_attr_hvac_action = getattr(self, "attr_hvac_action", None)
            # Recompute current hvac action (uses internal climate logic)
            if hasattr(self, "_compute_hvac_action"):
                self.attr_hvac_action = self._compute_hvac_action()
        except Exception:
            _LOGGER.debug(
                "better_thermostat %s: hvac action recompute failed (non critical)",
                getattr(self, "device_name", "unknown"),
            )
        await self.calculate_heating_power()
        _trv = self.hass.states.get(heater_entity_id)

        # Check if TRV is available before attempting to control it
        if _trv is None or _trv.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning(
                "better_thermostat %s: TRV %s is unavailable, skipping control cycle",
                self.device_name,
                heater_entity_id,
            )
            self.ignore_states = False
            self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
            return True

        _current_set_temperature = convert_to_float(
            str(_trv.attributes.get("temperature", None)),
            self.device_name,
            "controlling()",
        )

        _remapped_states = convert_outbound_states(
            self, heater_entity_id, self.bt_hvac_mode
        )
        if not isinstance(_remapped_states, dict):
            _LOGGER.debug(
                f"better_thermostat {self.device_name}: ERROR {
                    heater_entity_id} {_remapped_states}"
            )
            await asyncio.sleep(10)
            self.ignore_states = False
            self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
            return False

        _temperature = _remapped_states.get("temperature", None)
        _calibration = _remapped_states.get("local_temperature_calibration", None)
        _calibration_mode = self.real_trvs[heater_entity_id]["advanced"].get(
            "calibration_mode", CalibrationMode.DEFAULT
        )

        # Optional: set valve position if supported (e.g., MQTT/Z2M)
        try:
            bal = self.real_trvs[heater_entity_id].get("balance")
            if bal:
                target_pct = int(bal.get("valve_percent", 0))
                last_pct = self.real_trvs[heater_entity_id].get("last_valve_percent")
                # Apply if changed (let balance module handle hysteresis/min-interval). Skip only if no heating demand.
                if self.call_for_heat is False:
                    _LOGGER.debug(
                        "better_thermostat %s: skipping valve update for %s (call_for_heat is False)",
                        self.device_name,
                        heater_entity_id,
                    )
                elif last_pct is not None and int(last_pct) == target_pct:
                    _LOGGER.debug(
                        "better_thermostat %s: skipping valve update for %s (unchanged %s%%)",
                        self.device_name,
                        heater_entity_id,
                        target_pct,
                    )
                else:
                    _LOGGER.debug(
                        "better_thermostat %s: TO TRV set_valve: %s to: %s%%",
                        self.device_name,
                        heater_entity_id,
                        target_pct,
                    )
                    ok = await set_valve(self, heater_entity_id, target_pct)
                    if not ok:
                        _LOGGER.debug(
                            "better_thermostat %s: delegate.set_valve returned False (target=%s%%, entity=%s)",
                            self.device_name,
                            target_pct,
                            heater_entity_id,
                        )
        except Exception:
            _LOGGER.debug(
                "better_thermostat %s: set_valve not applied for %s (unsupported or failed)",
                self.device_name,
                heater_entity_id,
            )

        _new_hvac_mode = handle_window_open(self, _remapped_states)
        _new_hvac_mode = handle_hvac_mode_tolerance(self, _remapped_states)

        # New cooler section
        if self.cooler_entity_id is not None:
            if (
                self.cur_temp >= self.bt_target_cooltemp - self.tolerance
                and _new_hvac_mode is not HVACMode.OFF
                and self.cur_temp > self.bt_target_temp
            ):
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": self.cooler_entity_id,
                        "temperature": self.bt_target_cooltemp,
                    },
                    blocking=True,
                    context=self.context,
                )
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": self.cooler_entity_id, "hvac_mode": HVACMode.COOL},
                    blocking=True,
                    context=self.context,
                )
            elif (
                self.cur_temp <= (self.bt_target_cooltemp - self.tolerance)
                and _new_hvac_mode is not HVACMode.OFF
            ):
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": self.cooler_entity_id,
                        "temperature": self.bt_target_cooltemp,
                    },
                    blocking=True,
                    context=self.context,
                )
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": self.cooler_entity_id, "hvac_mode": HVACMode.OFF},
                    blocking=True,
                    context=self.context,
                )
            else:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": self.cooler_entity_id,
                        "temperature": self.bt_target_cooltemp,
                    },
                    blocking=True,
                    context=self.context,
                )
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": self.cooler_entity_id, "hvac_mode": HVACMode.OFF},
                    blocking=True,
                    context=self.context,
                )

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
                f"better_thermostat {self.device_name}: sending {
                    _min_temp}°C to the TRV because this device has no system mode off and heater should be off"
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
                f"better_thermostat {self.device_name}: TO TRV set_hvac_mode: {
                    heater_entity_id} from: {_trv.state} to: {_new_hvac_mode}"
            )
            self.real_trvs[heater_entity_id]["last_hvac_mode"] = _new_hvac_mode
            _tvr_has_quirk = await override_set_hvac_mode(
                self, heater_entity_id, _new_hvac_mode
            )
            if _tvr_has_quirk is False:
                await set_hvac_mode(self, heater_entity_id, _new_hvac_mode)
            if self.real_trvs[heater_entity_id]["system_mode_received"] is True:
                self.real_trvs[heater_entity_id]["system_mode_received"] = False
                self.task_manager.create_task(check_system_mode(self, heater_entity_id))

        # set new calibration offset
        if (
            _calibration is not None
            and _new_hvac_mode != HVACMode.OFF
            and _calibration_mode != CalibrationMode.NO_CALIBRATION
        ):
            _current_calibration_s = await get_current_offset(self, heater_entity_id)

            if _current_calibration_s is None:
                _LOGGER.error(
                    "better_thermostat %s: calibration fatal error %s",
                    self.device_name,
                    heater_entity_id,
                )
                # this should not be before, set_hvac_mode (because if it fails, the new hvac mode will never be sent)
                self.ignore_states = False
                self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
                return True

            _current_calibration = convert_to_float(
                str(_current_calibration_s), self.device_name, "controlling()"
            )

            _calibration = float(str(_calibration))

            _old_calibration = self.real_trvs[heater_entity_id].get(
                "last_calibration", _current_calibration
            )

            if self.real_trvs[heater_entity_id][
                "calibration_received"
            ] is True and float(_old_calibration) != float(_calibration):
                _LOGGER.debug(
                    f"better_thermostat {self.device_name}: TO TRV set_local_temperature_calibration: {
                        heater_entity_id} from: {_old_calibration} to: {_calibration}"
                )
                await set_offset(self, heater_entity_id, _calibration)
                self.real_trvs[heater_entity_id]["calibration_received"] = False

        # set new target temperature
        if _temperature is not None and (
            _new_hvac_mode != HVACMode.OFF or _no_off_system_mode
        ):
            if _temperature != _current_set_temperature:
                old = self.real_trvs[heater_entity_id].get("last_temperature", "?")
                _LOGGER.debug(
                    f"better_thermostat {self.device_name}: TO TRV set_temperature: {
                        heater_entity_id} from: {old} to: {_temperature}"
                )
                self.real_trvs[heater_entity_id]["last_temperature"] = _temperature
                await set_temperature(self, heater_entity_id, _temperature)
                if self.real_trvs[heater_entity_id]["target_temp_received"] is True:
                    self.real_trvs[heater_entity_id]["target_temp_received"] = False
                    self.task_manager.create_task(
                        check_target_temperature(self, heater_entity_id)
                    )

        await asyncio.sleep(3)
        self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
        return True


def handle_window_open(self, _remapped_states):
    """Handle window open state."""
    _converted_hvac_mode = _remapped_states.get("system_mode", None)
    _hvac_mode_send = _converted_hvac_mode

    if self.window_open is True and self.last_window_state is False:
        # if the window is open or the sensor is not available, we're done
        self.last_main_hvac_mode = _hvac_mode_send
        _hvac_mode_send = HVACMode.OFF
        self.last_window_state = True
        _LOGGER.debug(
            f"better_thermostat {
                self.device_name}: control_trv: window is open or status of window is unknown, setting window open"
        )
    elif self.window_open is False and self.last_window_state is True:
        _hvac_mode_send = self.last_main_hvac_mode
        self.last_window_state = False
        _LOGGER.debug(
            f"better_thermostat {
                self.device_name}: control_trv: window is closed, setting window closed restoring mode: {_hvac_mode_send}"
        )

    # Force off on window open
    if self.window_open is True:
        _hvac_mode_send = HVACMode.OFF

    return _hvac_mode_send


def handle_hvac_mode_tolerance(self, _remapped_states):
    """Determine the appropriate HVAC mode to display based on the current temperature and a specified tolerance.

    If the current temperature is within the tolerance range of the target temperature, the function returns HVACMode.OFF,
    indicating that no heating or cooling is needed. Otherwise, it returns the last main HVAC mode that was active.

    Parameters
    ----------
    _remapped_states : dict
        A dictionary containing the current system states, expected to include the key "system_mode".

    Returns
    -------
    str
        Returns HVACMode.OFF if the current temperature is within tolerance of the target temperature.
        Otherwise, returns the last main HVAC mode.
    """
    # Add tolerance check
    _within_tolerance = self.cur_temp >= (
        self.bt_target_temp - self.tolerance
    ) and self.cur_temp <= (self.bt_target_temp + self.tolerance)

    # Initialize the state tracker if it doesn't exist
    if not hasattr(self, "_was_within_tolerance"):
        self._was_within_tolerance = False

    # Only update last_main_hvac_mode when entering tolerance from outside
    if _within_tolerance:
        if not self._was_within_tolerance:
            self.last_main_hvac_mode = _remapped_states.get("system_mode", None)
        self._was_within_tolerance = True
        return HVACMode.OFF
    else:
        self._was_within_tolerance = False
        # If last_main_hvac_mode is None, fall back to current system_mode
        if self.last_main_hvac_mode is not None:
            return self.last_main_hvac_mode
        else:
            return _remapped_states.get("system_mode", None)


async def check_system_mode(self, heater_entity_id=None):
    """Check system mode."""
    _timeout = 0
    _real_trv = self.real_trvs[heater_entity_id]
    while _real_trv["hvac_mode"] != _real_trv["last_hvac_mode"]:
        if _timeout > 360:
            _LOGGER.debug(
                f"better_thermostat {self.device_name}: {
                    heater_entity_id} the real TRV did not respond to the system mode change"
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
            self.device_name,
            "check_target_temperature()",
        )
        if _timeout == 0:
            _LOGGER.debug(
                f"better_thermostat {self.device_name}: {heater_entity_id} / check_target_temp / _last: {
                    _real_trv['last_temperature']} - _current: {_current_set_temperature}"
            )
        if (
            _current_set_temperature is None
            or _real_trv["last_temperature"] == _current_set_temperature
        ):
            _timeout = 0
            break
        if _timeout > 360:
            _LOGGER.debug(
                f"better_thermostat {self.device_name}: {
                    heater_entity_id} the real TRV did not respond to the target temperature change"
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)

    _real_trv["target_temp_received"] = True
    return True
