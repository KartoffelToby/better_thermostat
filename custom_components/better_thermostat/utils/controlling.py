"""Controlling module for Better Thermostat."""

import asyncio
import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.better_thermostat.adapters.delegate import (
    get_current_offset,
    set_hvac_mode,
    set_offset,
    set_temperature,
    set_valve,
)
from custom_components.better_thermostat.events.trv import convert_outbound_states
from custom_components.better_thermostat.model_fixes.model_quirks import (
    override_set_hvac_mode,
)
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.helpers import convert_to_float

_LOGGER = logging.getLogger(__name__)


def _is_boost_heating_active(self) -> bool:
    """Check if boost mode is active and heating is needed.

    Returns True when boost preset is active and current temperature
    is below target temperature.
    """
    return (
        self.preset_mode == PRESET_BOOST
        and self.cur_temp is not None
        and self.bt_target_temp is not None
        and self.cur_temp < self.bt_target_temp
    )


def _get_valve_control(
    self, heater_entity_id: str, calibration_mode, calibration_type
) -> tuple[dict | None, str | None]:
    """Determine valve control settings based on boost mode or calibration.

    Returns a tuple of (valve_settings_dict, source_name).
    valve_settings_dict contains 'valve_percent' and 'apply_valve' keys.
    Returns (None, None) if no valve control should be applied.
    """
    # Boost mode takes priority - set valve to 100%
    if _is_boost_heating_active(self):
        return {"valve_percent": 100, "apply_valve": True}, "boost_mode"

    # Check calibration-based valve control
    if calibration_type != CalibrationType.DIRECT_VALVE_BASED:
        return None, None

    # Try calibration balance from various calibration modes
    cal_bal = self.real_trvs[heater_entity_id].get("calibration_balance")
    if (
        isinstance(cal_bal, dict)
        and cal_bal.get("apply_valve")
        and cal_bal.get("valve_percent") is not None
    ):
        source_map = {
            CalibrationMode.MPC_CALIBRATION: "mpc_calibration",
            CalibrationMode.TPI_CALIBRATION: "tpi_calibration",
            CalibrationMode.PID_CALIBRATION: "pid_calibration",
            CalibrationMode.HEATING_POWER_CALIBRATION: "heating_power_calibration",
        }
        source = source_map.get(calibration_mode)
        if source:
            return cal_bal, source

    # Fallback to raw balance
    raw_balance = self.real_trvs[heater_entity_id].get("balance")
    if (
        isinstance(raw_balance, dict)
        and raw_balance.get("apply_valve")
        and raw_balance.get("valve_percent") is not None
    ):
        return raw_balance, "balance"

    return None, None


async def _apply_valve_control(
    self, heater_entity_id: str, bal: dict | None, source: str | None
) -> bool:
    """Apply valve control settings to the TRV.

    Returns True if valve was set, False otherwise.
    """
    if bal is None:
        return False

    target_pct = int(round(bal.get("valve_percent", 0)))
    target_pct = _apply_valve_max_opening(self, heater_entity_id, target_pct)

    _LOGGER.debug(
        "better_thermostat %s: TO TRV set_valve: %s to: %s%% (source=%s)",
        self.device_name,
        heater_entity_id,
        target_pct,
        source,
    )
    ok = await set_valve(self, heater_entity_id, target_pct)
    if not ok:
        _LOGGER.debug(
            "better_thermostat %s: delegate.set_valve returned False (target=%s%%, entity=%s, source=%s)",
            self.device_name,
            target_pct,
            heater_entity_id,
            source,
        )
    return ok


async def _reset_valve_on_safety_override(
    self, heater_entity_id: str, new_hvac_mode, source: str | None
) -> None:
    """Reset valve to 0% when safety overrides force HVAC OFF during boost mode.

    When boost mode sets valve to 100% but safety checks (window open, no heat call)
    force HVAC mode to OFF, we must reset the valve to avoid conflicting commands.
    """
    if new_hvac_mode == HVACMode.OFF and source == "boost_mode":
        try:
            _LOGGER.debug(
                "better_thermostat %s: Safety override active, resetting valve to 0%% for %s",
                self.device_name,
                heater_entity_id,
            )
            await set_valve(self, heater_entity_id, 0)
        except Exception:
            _LOGGER.warning(
                "better_thermostat %s: Failed to reset valve for %s during safety override",
                self.device_name,
                heater_entity_id,
                exc_info=True,
            )


def _apply_valve_max_opening(self, entity_id: str, target_pct: int) -> int:
    """Clamp target valve percent to user-defined max opening (if configured)."""

    max_opening = (self.real_trvs.get(entity_id) or {}).get("valve_max_opening")
    if isinstance(max_opening, (int, float)):
        try:
            max_opening = int(round(float(max_opening)))
        except (TypeError, ValueError):
            return target_pct
        return min(target_pct, max(0, min(100, max_opening)))
    return target_pct


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

    try:
        while True:
            if getattr(self, "in_maintenance", False):
                await asyncio.sleep(1)
                continue

            if self.ignore_states or self.startup_running:
                await asyncio.sleep(1)
                continue
            else:
                controls_to_process = await self.control_queue_task.get()
                if controls_to_process is not None:
                    self.ignore_states = True

                    # Calculate heating power once per cycle
                    try:
                        await self.calculate_heating_power()
                    except Exception:
                        _LOGGER.exception(
                            "better_thermostat %s: ERROR calculating heating power",
                            self.device_name,
                        )

                    # Calculate heat loss once per cycle (idle cooling)
                    try:
                        await self.calculate_heat_loss()
                    except Exception:
                        _LOGGER.exception(
                            "better_thermostat %s: ERROR calculating heat loss",
                            self.device_name,
                        )

                    # Handle cooler logic once per cycle
                    if self.cooler_entity_id is not None:
                        try:
                            await control_cooler(self)
                        except Exception:
                            _LOGGER.exception(
                                "better_thermostat %s: ERROR controlling cooler",
                                self.device_name,
                            )

                    # Create tasks for all TRVs to run in parallel
                    tasks = []
                    for trv in self.real_trvs.keys():
                        tasks.append(control_trv(self, trv))

                    # Run all TRV controls in parallel
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    result = True
                    for i, res in enumerate(results):
                        if isinstance(res, Exception):
                            trv_id = list(self.real_trvs.keys())[i]
                            _LOGGER.error(
                                "better_thermostat %s: ERROR controlling TRV %s: %s",
                                self.device_name,
                                trv_id,
                                res,
                            )
                            result = False
                        elif res is False:
                            result = False

                    # Retry task if some TRVs failed. Discard the task if the queue is full
                    # to avoid blocking and therefore deadlocking this function.
                    if result is False:
                        try:
                            self.control_queue_task.put_nowait(self)
                        except asyncio.QueueFull:
                            _LOGGER.debug(
                                "better_thermostat %s: control queue is full, discarding task",
                                self.device_name,
                            )

                    self.control_queue_task.task_done()
                    if not getattr(self, "in_maintenance", False):
                        self.ignore_states = False
    except asyncio.CancelledError:
        _LOGGER.debug(
            "better_thermostat %s: control_queue task cancelled, cleaning up",
            self.device_name,
        )
        raise
    finally:
        # Ensure ignore_states is reset on any exit unless maintenance wants it suppressed.
        if not getattr(self, "in_maintenance", False):
            self.ignore_states = False


async def control_cooler(self):
    """Control the cooler entity."""
    # Get current cooler state to avoid sending redundant commands
    cooler_state = self.hass.states.get(self.cooler_entity_id)
    if cooler_state is None or cooler_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s unavailable, skipping",
            self.device_name,
            self.cooler_entity_id,
        )
        return

    current_hvac_mode = cooler_state.state
    current_temp = cooler_state.attributes.get("temperature")

    # Determine desired state based on current conditions
    desired_temp = self.bt_target_cooltemp

    if any(
        v is None
        for v in (
            self.cur_temp,
            self.bt_target_cooltemp,
            self.tolerance,
            self.bt_target_temp,
        )
    ):
        _LOGGER.debug(
            "better_thermostat %s: cooler %s one or more required values are None "
            "(cur_temp=%s, bt_target_cooltemp=%s, tolerance=%s, bt_target_temp=%s), "
            "defaulting to OFF",
            self.device_name,
            self.cooler_entity_id,
            self.cur_temp,
            self.bt_target_cooltemp,
            self.tolerance,
            self.bt_target_temp,
        )
        desired_mode = HVACMode.OFF
    elif self.bt_hvac_mode == HVACMode.OFF:
        desired_mode = HVACMode.OFF
    elif (
        self.cur_temp >= self.bt_target_cooltemp - self.tolerance
        and self.cur_temp > self.bt_target_temp
    ):
        desired_mode = HVACMode.COOL
    else:
        desired_mode = HVACMode.OFF

    # Only send temperature command if it differs from current
    if current_temp is None or current_temp != desired_temp:
        if current_temp is None:
            _LOGGER.debug(
                "better_thermostat %s: cooler %s current temperature is unknown, "
                "sending set_temperature command anyway",
                self.device_name,
                self.cooler_entity_id,
            )
        else:
            _LOGGER.debug(
                "better_thermostat %s: TO COOLER set_temperature: %s from: %s to: %s",
                self.device_name,
                self.cooler_entity_id,
                current_temp,
                desired_temp,
            )
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": self.cooler_entity_id, "temperature": desired_temp},
            blocking=True,
            context=self.context,
        )

    # Only send hvac_mode command if it differs from current
    if current_hvac_mode != desired_mode:
        _LOGGER.debug(
            "better_thermostat %s: TO COOLER set_hvac_mode: %s from: %s to: %s",
            self.device_name,
            self.cooler_entity_id,
            current_hvac_mode,
            desired_mode,
        )
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self.cooler_entity_id, "hvac_mode": desired_mode},
            blocking=True,
            context=self.context,
        )


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

    # Removed calculate_heating_power() from here, moved to control_queue

    _trv = self.hass.states.get(heater_entity_id)

    # Check if TRV is available before attempting to control it
    if _trv is None or _trv.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        _LOGGER.debug(
            "better_thermostat %s: TRV %s is unavailable, skipping control. "
            "Control will resume when TRV becomes available.",
            self.device_name,
            heater_entity_id,
        )
        self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
        return True

    _current_set_temperature = convert_to_float(
        str(_trv.attributes.get("temperature", None)), self.device_name, "controlling()"
    )

    _remapped_states = convert_outbound_states(
        self, heater_entity_id, self.bt_hvac_mode
    )
    if not isinstance(_remapped_states, dict):
        _LOGGER.debug(
            "better_thermostat %s: ERROR %s %s",
            self.device_name,
            heater_entity_id,
            _remapped_states,
        )
        # Reduced sleep time on error to avoid blocking too long
        await asyncio.sleep(2)
        # self.ignore_states = False # Don't touch global ignore_states here
        self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
        return False

    _temperature = _remapped_states.get("temperature", None)
    _calibration = _remapped_states.get("local_temperature_calibration", None)
    _calibration_mode = self.real_trvs[heater_entity_id]["advanced"].get(
        "calibration_mode", CalibrationMode.MPC_CALIBRATION
    )
    _calibration_type = self.real_trvs[heater_entity_id]["advanced"].get(
        "calibration", CalibrationType.TARGET_TEMP_BASED
    )

    # Boost mode: set max temperature
    if _is_boost_heating_active(self):
        _temperature = self.real_trvs[heater_entity_id].get("max_temp", 30.0)

    # Determine and apply valve control (boost or calibration-based)
    bal, _source = _get_valve_control(
        self, heater_entity_id, _calibration_mode, _calibration_type
    )
    try:
        await _apply_valve_control(self, heater_entity_id, bal, _source)
    except Exception:
        _LOGGER.debug(
            "better_thermostat %s: set_valve not applied for %s (unsupported or failed)",
            self.device_name,
            heater_entity_id,
        )

    _new_hvac_mode = handle_window_open(self, _remapped_states)

    # if we don't need to heat, we force HVACMode to be off
    if self.call_for_heat is False:
        _new_hvac_mode = HVACMode.OFF

    # Reset valve when safety overrides force HVAC OFF during boost mode
    await _reset_valve_on_safety_override(
        self, heater_entity_id, _new_hvac_mode, _source
    )

    # Manage TRVs with no HVACMode.OFF
    _no_off_system_mode = (
        HVACMode.OFF not in self.real_trvs[heater_entity_id]["hvac_modes"]
    ) or (
        self.real_trvs[heater_entity_id]["advanced"].get("no_off_system_mode", False)
        is True
    )
    if _no_off_system_mode is True and _new_hvac_mode == HVACMode.OFF:
        _min_temp = self.real_trvs[heater_entity_id]["min_temp"]
        _LOGGER.debug(
            "better_thermostat %s: sending %s°C to the TRV because this device has no system mode off and heater should be off",
            self.device_name,
            _min_temp,
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
            "better_thermostat %s: TO TRV set_hvac_mode: %s from: %s to: %s",
            self.device_name,
            heater_entity_id,
            _trv.state,
            _new_hvac_mode,
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
            # self.ignore_states = False # Don't touch global ignore_states here
            self.real_trvs[heater_entity_id]["ignore_trv_states"] = False
            return True

        _current_calibration = convert_to_float(
            str(_current_calibration_s), self.device_name, "controlling()"
        )

        _calibration = float(str(_calibration))

        _old_calibration = self.real_trvs[heater_entity_id].get(
            "last_calibration", _current_calibration
        )

        # Fix for grouped TRVs: If current calibration already matches target,
        # reset calibration_received to True. This handles the case where the
        # TRV's state change event was ignored during the control cycle
        # (when ignore_states=True), leaving calibration_received stuck at False.
        if (
            self.real_trvs[heater_entity_id]["calibration_received"] is False
            and _current_calibration is not None
            and abs(float(_current_calibration) - float(_calibration)) < 0.5
        ):
            _LOGGER.debug(
                "better_thermostat %s: TRV %s calibration already at target (%s), "
                "resetting calibration_received flag",
                self.device_name,
                heater_entity_id,
                _calibration,
            )
            self.real_trvs[heater_entity_id]["calibration_received"] = True

        if self.real_trvs[heater_entity_id]["calibration_received"] is True and float(
            _old_calibration
        ) != float(_calibration):
            _LOGGER.debug(
                "better_thermostat %s: TO TRV set_local_temperature_calibration: %s from: %s to: %s",
                self.device_name,
                heater_entity_id,
                _old_calibration,
                _calibration,
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
                "better_thermostat %s: TO TRV set_temperature: %s from: %s to: %s",
                self.device_name,
                heater_entity_id,
                old,
                _temperature,
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
    if self.window_open:
        return HVACMode.OFF
    return _remapped_states.get("system_mode", None)


async def check_system_mode(self, heater_entity_id=None):
    """Check system mode."""
    _timeout = 0
    _real_trv = self.real_trvs[heater_entity_id]
    while _real_trv["hvac_mode"] != _real_trv["last_hvac_mode"]:
        if _timeout > 360:
            _LOGGER.debug(
                "better_thermostat %s: %s the real TRV did not respond to the system mode change",
                self.device_name,
                heater_entity_id,
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
                "better_thermostat %s: %s / check_target_temp / _last: %s - _current: %s",
                self.device_name,
                heater_entity_id,
                _real_trv["last_temperature"],
                _current_set_temperature,
            )
        if (
            _current_set_temperature is None
            or _real_trv["last_temperature"] == _current_set_temperature
        ):
            _timeout = 0
            break
        if _timeout > 360:
            _LOGGER.debug(
                "better_thermostat %s: %s the real TRV did not respond to the target temperature change",
                self.device_name,
                heater_entity_id,
            )
            _timeout = 0
            break
        await asyncio.sleep(1)
        _timeout += 1
    await asyncio.sleep(2)

    _real_trv["target_temp_received"] = True
    return True
