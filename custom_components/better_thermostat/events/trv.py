import asyncio
import logging
from typing import Union

from homeassistant.components.climate.const import HVAC_MODE_HEAT, HVAC_MODE_OFF
from homeassistant.core import callback, State

from ..utils.helpers import (
    calculate_local_setpoint_delta,
    calculate_setpoint_override,
    mode_remap,
    round_to_half_degree,
    convert_to_float,
)

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
    if self.startup_running:
        return

    _updated_needed = False

    entity_id = event.data.get("entity_id")

    child_lock = self.real_trvs[entity_id]["advanced"].get("child_lock")

    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")

    _org_trv_state = new_state.state

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
        new_state = convert_inbound_states(self, entity_id, new_state)
    except TypeError:
        _LOGGER.debug(
            f"better_thermostat {self.name}: remapping TRV state failed, skipping"
        )
        return

    _new_current_temp = convert_to_float(
        str(new_state.attributes.get("current_temperature", None)),
        self.name,
        "TRV_current_temp",
    )

    if (
        _new_current_temp is not None
        and self.real_trvs[entity_id]["current_temperature"] != _new_current_temp
    ):
        _old_temp = self.real_trvs[entity_id]["current_temperature"]
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV's sends new internal temperature from {_old_temp} to {_new_current_temp}"
        )
        self.real_trvs[entity_id]["current_temperature"] = _new_current_temp
        _updated_needed = True
        if self.real_trvs[entity_id]["calibration_received"] is False:
            self.real_trvs[entity_id]["calibration_received"] = True
            _LOGGER.debug(f"better_thermostat {self.name}: calibration accepted by TRV")
            await asyncio.sleep(1)

    new_decoded_system_mode = str(new_state.state)

    if new_decoded_system_mode not in (HVAC_MODE_OFF, HVAC_MODE_HEAT):
        # not an valid mode, overwriting
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV's decoded TRV mode is not valid, skipping"
        )
        return

    if self.real_trvs[entity_id]["hvac_mode"] != _org_trv_state and not child_lock:
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV's decoded TRV mode changed from {self._bt_hvac_mode} to {new_decoded_system_mode}"
        )
        if self.window_open:
            self.real_trvs[entity_id]["hvac_mode"] = _org_trv_state
        else:
            self._bt_hvac_mode = new_decoded_system_mode
            self.real_trvs[entity_id]["hvac_mode"] = _org_trv_state
        _updated_needed = True

    try:
        if event.context.id == self._context.id:
            _LOGGER.debug(
                f"better_thermostat {self.name}: Ignoring event from own changes"
            )
            return
    except AttributeError:
        pass

    if self.real_trvs[entity_id]["ignore_trv_states"] or self.ignore_states:
        return

    _new_heating_setpoint = convert_to_float(
        str(new_state.attributes.get("temperature", None)),
        self.name,
        "trigger_trv_change()",
    )
    if _new_heating_setpoint is not None and self._bt_hvac_mode is not HVAC_MODE_OFF:
        if (
            _new_heating_setpoint < self._min_temp
            or self._max_temp < _new_heating_setpoint
        ):
            _LOGGER.warning(
                f"better_thermostat {self.name}: New TRV setpoint outside of range, overwriting it"
            )

            if _new_heating_setpoint < self._min_temp:
                _new_heating_setpoint = self._min_temp
            else:
                _new_heating_setpoint = self._max_temp

        if (
            self._target_temp != _new_heating_setpoint
            and not child_lock
            and self.real_trvs[entity_id]["last_temperature"] != _new_heating_setpoint
        ):
            _LOGGER.debug(
                f"better_thermostat {self.name}: TRV's decoded TRV target temp changed from {self._target_temp} to {_new_heating_setpoint}"
            )
            self._target_temp = _new_heating_setpoint
            _updated_needed = True

    if _updated_needed or child_lock:
        if (
            self._bt_hvac_mode == HVAC_MODE_OFF
            and self.real_trvs[entity_id]["hvac_mode"] == HVAC_MODE_OFF
        ):
            self.async_write_ha_state()
            return

        _LOGGER.debug(f"better_thermostat {self.name}: {entity_id} TRV update triggerd")
        self.async_write_ha_state()
        await self.control_queue_task.put(self)


def convert_inbound_states(self, entity_id, state: State):
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

    state.state = mode_remap(self, entity_id, str(state.state), True)

    return state


def convert_outbound_states(self, entity_id, hvac_mode) -> Union[dict, None]:
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
                    temperature: float
                    local_temperature: float
                    local_temperature_calibration: float
                    system_mode: string
    None
            In case of an error.
    """

    _new_local_calibration = None
    _new_heating_setpoint = None

    try:
        _calibration_type = self.real_trvs[entity_id].get("calibration", 1)

        if _calibration_type is None:
            _LOGGER.warning(
                "better_thermostat %s: no calibration type found in device config, talking to the TRV using fallback mode",
                self.name,
            )
            _new_heating_setpoint = self._target_temp
            _new_local_calibration = round_to_half_degree(
                calculate_local_setpoint_delta(self, entity_id)
            )
            if _new_local_calibration is None:
                return None

        else:
            if _calibration_type == 0:
                _round_calibration = self.real_trvs[entity_id]["advanced"].get(
                    "calibration_round"
                )

                if _round_calibration is not None and (
                    (
                        isinstance(_round_calibration, str)
                        and _round_calibration.lower() == "true"
                    )
                    or _round_calibration is True
                ):
                    _new_local_calibration = round_to_half_degree(
                        calculate_local_setpoint_delta(self, entity_id)
                    )
                else:
                    _new_local_calibration = calculate_local_setpoint_delta(
                        self, entity_id
                    )

                _new_heating_setpoint = self._target_temp

            elif _calibration_type == 1:

                _round_calibration = self.real_trvs[entity_id]["advanced"].get(
                    "calibration_round"
                )

                if _round_calibration is not None and (
                    (
                        isinstance(_round_calibration, str)
                        and _round_calibration.lower() == "true"
                    )
                    or _round_calibration is True
                ):
                    _new_heating_setpoint = round_to_half_degree(
                        calculate_setpoint_override(self, entity_id)
                    )
                else:
                    _new_heating_setpoint = calculate_setpoint_override(self, entity_id)

            _system_modes = self.real_trvs[entity_id]["hvac_modes"]
            _has_system_mode = False
            if _system_modes is not None:
                _has_system_mode = True

            # Handling different devices with or without system mode reported or contained in the device config

            hvac_mode = mode_remap(self, entity_id, str(hvac_mode), False)

            if _has_system_mode is False:
                _LOGGER.debug(
                    f"better_thermostat {self.name}: device config expects no system mode, while the device has one. Device system mode will be ignored"
                )
                if hvac_mode == HVAC_MODE_OFF:
                    _new_heating_setpoint = 5
                hvac_mode = None

            elif _has_system_mode is None:
                if hvac_mode == HVAC_MODE_OFF:
                    _LOGGER.debug(
                        f"better_thermostat {self.name}: sending 5°C to the TRV because this device has no system mode and heater should be off"
                    )
                    _new_heating_setpoint = 5
                hvac_mode = None

        return {
            "temperature": _new_heating_setpoint,
            "local_temperature": self.real_trvs[entity_id]["current_temperature"],
            "system_mode": hvac_mode,
            "local_temperature_calibration": _new_local_calibration,
        }
    except Exception:
        return None
