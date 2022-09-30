import logging
from typing import Union
from datetime import datetime, timedelta
from homeassistant.components.climate.const import HVAC_MODE_HEAT, HVAC_MODE_OFF
from homeassistant.core import callback, State

from ..helpers import (
    calculate_local_setpoint_delta,
    calculate_setpoint_override,
    mode_remap,
    round_to_half_degree,
)
from ..helpers import convert_to_float

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_trv_change(self, event):
    """Processes TRV status updates

    Parameters
    ----------
    self :
            self instance of better_thermostat
    event :
            Event object from the eventbus. Contains the new and old state from the TRV.

    Returns
    -------
    None
    """
    _updated_needed = False
    child_lock = False
    if self.startup_running:
        _LOGGER.debug(
            f"better_thermostat {self.name}: skipping trigger_trv_change because startup is running"
        )
        return

    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")

    if None in (new_state, old_state, new_state.attributes):
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV update contained not all necessary data for processing, skipping"
        )
        return

    if not isinstance(new_state, State) or not isinstance(old_state, State):
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV update contained not a State, skipping"
        )
        return

    try:
        new_state = convert_inbound_states(self, new_state)
    except TypeError:
        _LOGGER.debug(
            f"better_thermostat {self.name}: remapping TRV state failed, skipping"
        )
        return

    # if flag is on, we won't do any further processing
    if self.ignore_states:
        _LOGGER.debug(
            f"better_thermostat {self.name}: skipping trigger_trv_change because ignore_states is true"
        )
        return

    new_decoded_system_mode = str(new_state.state)

    if new_decoded_system_mode not in (HVAC_MODE_OFF, HVAC_MODE_HEAT):
        # not an valid mode, overwriting
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV's decoded TRV mode is not valid, skipping"
        )
        return

    # we only read setpoint changes from TRV if we are in heating mode
    if self._trv_hvac_mode == HVAC_MODE_OFF:
        return

    if self._TRV_current_temp != new_state.attributes.get("current_temperature"):
        self._TRV_current_temp = convert_to_float(
            new_state.attributes.get("current_temperature"),
            self.name,
            "TRV_current_temp",
        )
        if (
            self.homaticip
            and (self.last_change + timedelta(hours=1)).timestamp()
            > datetime.now().timestamp()
        ):
            _LOGGER.info(
                f"better_thermostat {self.name}: skip sending new calculation based on internal temp to TRV because of homaticip throttling"
            )
            _updated_needed = False
        else:
            _updated_needed = True
            if self.child_lock:
                child_lock = True

    if not self.homaticip and self.child_lock:
        child_lock = True

    _new_heating_setpoint = convert_to_float(
        new_state.attributes.get("current_heating_setpoint")
        or new_state.attributes.get("temperature"),
        self.name,
        "trigger_trv_change()",
    )
    if _new_heating_setpoint is None:
        _LOGGER.debug(
            f"better_thermostat {self.name}: trigger_trv_change: new_heating_setpoint is None, skipping"
        )
        return
    if _new_heating_setpoint < self._min_temp or self._max_temp < _new_heating_setpoint:
        _LOGGER.warning(
            f"better_thermostat {self.name}: New TRV setpoint outside of range, overwriting it"
        )

        if _new_heating_setpoint < self._min_temp:
            _new_heating_setpoint = self._min_temp
        else:
            _new_heating_setpoint = self._max_temp

    if self._trv_hvac_mode != new_decoded_system_mode and not self.child_lock:
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV's decoded TRV mode changed from {self._trv_hvac_mode} to {new_decoded_system_mode}"
        )
        self._trv_hvac_mode = new_decoded_system_mode
        _updated_needed = True

    if (
        self._target_temp != _new_heating_setpoint
        and not self.child_lock
        and self._last_send_target_temp != _new_heating_setpoint
    ):
        self._target_temp = _new_heating_setpoint
        _updated_needed = True

    if _updated_needed or child_lock:
        self.async_write_ha_state()
        # make sure we only update the latest user interaction
        """
        try:
            if self.control_queue_task.qsize() > 0:
                while self.control_queue_task.qsize() > 1:
                    self.control_queue_task.task_done()
        except AttributeError:
            pass
        """
        await self.control_queue_task.put(self)


def update_valve_position(self, valve_position):
    """Updates the stored valve position and triggers async tasks waiting for this

    Parameters
    ----------
    self :
            FIXME
    valve_position :
            the new valve position

    Returns
    -------
    None
    """

    if valve_position is not None:
        _LOGGER.debug(
            f"better_thermostat {self.name}: Updating valve position to {valve_position}"
        )
        self._last_reported_valve_position = valve_position
        self._last_reported_valve_position_update_wait_lock.release()
    else:
        _LOGGER.debug(
            f"better_thermostat {self.name}: Valve position is None, skipping"
        )


def convert_inbound_states(self, state: State):
    """Convert hvac mode in a thermostat state from HA
    Parameters
    ----------
    self :
            self instance of better_thermostat
    state : State
            Inbound thermostat state, which will be modified
    Returns
    -------
    Modified state
    """

    if state is None:
        raise TypeError("convert_inbound_states() received None state, cannot convert")

    if state.attributes is None or state.state is None:
        raise TypeError("convert_inbound_states() received None state, cannot convert")

    if self._config is None:
        raise TypeError(
            "convert_inbound_states() could not find config, cannot convert"
        )

    state.state = mode_remap(self, str(state.state), True)

    return state


def convert_outbound_states(self, hvac_mode) -> Union[dict, None]:
    """Creates the new outbound thermostat state.
    Parameters
    ----------
    self :
            self instance of better_thermostat
    hvac_mode :
            the HA mode to convert to
    Returns
    -------
    dict
            A dictionary containing the new outbound thermostat state containing the following keys:
                    current_heating_setpoint: float
                    local_temperature: float
                    local_temperature_calibration: float
                    system_mode: string
    None
            In case of an error.
    """
    state = self.hass.states.get(self.heater_entity_id).attributes

    _new_local_calibration = None
    _new_heating_setpoint = None

    if self._config is None:
        _LOGGER.warning(
            "better_thermostat %s: no matching device config loaded, talking to the TRV using fallback mode",
            self.name,
        )
        _new_heating_setpoint = self._target_temp
        _new_local_calibration = round_to_half_degree(
            calculate_local_setpoint_delta(self)
        )
        if _new_local_calibration is None:
            return None

    else:
        _calibration_type = self._config.get("calibration_type")

        if _calibration_type is None:
            _LOGGER.warning(
                "better_thermostat %s: no calibration type found in device config, talking to the TRV using fallback mode",
                self.name,
            )
            _new_heating_setpoint = self._target_temp
            _new_local_calibration = round_to_half_degree(
                calculate_local_setpoint_delta(self)
            )
            if _new_local_calibration is None:
                return None

        else:
            if _calibration_type == 0:
                _round_calibration = self._config.get("calibration_round")

                if _round_calibration is not None and (
                    (
                        isinstance(_round_calibration, str)
                        and _round_calibration.lower() == "true"
                    )
                    or _round_calibration is True
                ):
                    _new_local_calibration = round_to_half_degree(
                        calculate_local_setpoint_delta(self)
                    )
                else:
                    _new_local_calibration = calculate_local_setpoint_delta(self)

                _new_heating_setpoint = self._target_temp

            elif _calibration_type == 1:
                _new_heating_setpoint = calculate_setpoint_override(self)

            _has_system_mode = self._config.get("has_system_mode")
            _system_mode = self._config.get("system_mode")

            if (
                isinstance(_has_system_mode, str)
                and _has_system_mode.lower() == "false"
            ):
                # we expect no system mode
                _has_system_mode = False
            elif (
                isinstance(_has_system_mode, str) and _has_system_mode.lower() == "true"
            ):
                # we expect a system mode
                _has_system_mode = True

            # Handling different devices with or without system mode reported or contained in the device config

            if _has_system_mode is True or (
                _has_system_mode is None and _system_mode is not None
            ):
                hvac_mode = mode_remap(self, hvac_mode)

            elif _has_system_mode is True and _system_mode is None:
                _LOGGER.error(
                    f"better_thermostat {self.name}: device reports no system mode, while device config expects one. No changes to TRV "
                    f"will be made until device reports a system mode (again)"
                )
                return None

            elif _has_system_mode is False:
                if _system_mode is not None:
                    _LOGGER.warning(
                        f"better_thermostat {self.name}: device config expects no system mode, while the device has one. Device system mode will be ignored"
                    )
                if hvac_mode == HVAC_MODE_OFF:
                    _new_heating_setpoint = 5
                hvac_mode = None

            elif _has_system_mode is None and _system_mode is None:
                if hvac_mode == HVAC_MODE_OFF:
                    _LOGGER.info(
                        f"better_thermostat {self.name}: sending 5°C to the TRV because this device has no system mode and heater should be off"
                    )
                    _new_heating_setpoint = 5
                hvac_mode = None

    return {
        "current_heating_setpoint": _new_heating_setpoint,
        "local_temperature": state.get("current_temperature"),
        "system_mode": hvac_mode,
        "local_temperature_calibration": _new_local_calibration,
    }
