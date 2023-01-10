from datetime import datetime
import logging
from typing import Union
from custom_components.better_thermostat.const import CONF_HOMATICIP

from homeassistant.components.climate.const import (
    HVACMode,
    ATTR_HVAC_ACTION,
    HVACAction,
)
from homeassistant.core import State, callback
from homeassistant.components.group.util import find_state_attributes
from ..utils.helpers import (
    calculate_local_setpoint_delta,
    calculate_setpoint_override,
    convert_to_float,
    mode_remap,
    round_to_half_degree,
)
from custom_components.better_thermostat.utils.bridge import get_current_offset

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_trv_change(self, event):
    """Trigger a change in the trv state."""
    if self.startup_running:
        return
    if self.control_queue_task is None:
        return
    update_hvac_action(self)
    _main_change = False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    entity_id = event.data.get("entity_id")

    if None in (new_state, old_state, new_state.attributes):
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV {entity_id} update contained not all necessary data for processing, skipping"
        )
        return

    if not isinstance(new_state, State) or not isinstance(old_state, State):
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV {entity_id} update contained not a State, skipping"
        )
        return

    # if new_state == old_state:
    #    return

    _org_trv_state = self.hass.states.get(entity_id)
    child_lock = self.real_trvs[entity_id]["advanced"].get("child_lock")

    _new_current_temp = convert_to_float(
        str(_org_trv_state.attributes.get("current_temperature", None)),
        self.name,
        "TRV_current_temp",
    )

    _time_diff = 5
    try:
        for trv in self.all_trvs:
            if trv["advanced"][CONF_HOMATICIP]:
                _time_diff = 600
    except KeyError:
        pass
    if (
        _new_current_temp is not None
        and self.real_trvs[entity_id]["current_temperature"] != _new_current_temp
        and (
            (datetime.now() - self.last_internal_sensor_change).total_seconds()
            > _time_diff
            or (
                self.real_trvs[entity_id]["calibration_received"] is False
                and self.real_trvs[entity_id]["calibration"] == 0
            )
        )
    ):
        _old_temp = self.real_trvs[entity_id]["current_temperature"]
        self.real_trvs[entity_id]["current_temperature"] = _new_current_temp
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV {entity_id} sends new internal temperature from {_old_temp} to {_new_current_temp}"
        )
        self.last_internal_sensor_change = datetime.now()
        _main_change = True

        # TODO: async def in controlling?
        if self.real_trvs[entity_id]["calibration_received"] is False:
            self.real_trvs[entity_id]["calibration_received"] = True
            _LOGGER.debug(
                f"better_thermostat {self.name}: calibration accepted by TRV {entity_id}"
            )
            _main_change = False
            self.old_internal_temp = self.real_trvs[entity_id]["current_temperature"]
            self.old_external_temp = self.cur_temp
            if self.real_trvs[entity_id]["calibration"] == 0:
                self.real_trvs[entity_id][
                    "last_calibration"
                ] = await get_current_offset(self, entity_id)

    if self.ignore_states:
        return

    try:
        mapped_state = convert_inbound_states(self, entity_id, _org_trv_state)
    except TypeError:
        _LOGGER.debug(
            f"better_thermostat {self.name}: remapping TRV {entity_id} state failed, skipping"
        )
        return

    if mapped_state in (HVACMode.OFF, HVACMode.HEAT):
        if (
            self.real_trvs[entity_id]["hvac_mode"] != _org_trv_state.state
            and not child_lock
        ):
            _old = self.real_trvs[entity_id]["hvac_mode"]
            _LOGGER.debug(
                f"better_thermostat {self.name}: TRV {entity_id} decoded TRV mode changed from {_old} to {_org_trv_state.state} - converted {new_state.state}"
            )
            self.real_trvs[entity_id]["hvac_mode"] = _org_trv_state.state
            _main_change = True
            if (
                child_lock is False
                and self.real_trvs[entity_id]["system_mode_received"] is True
                and self.real_trvs[entity_id]["last_hvac_mode"] != _org_trv_state.state
            ):
                self.bt_hvac_mode = mapped_state

    _old_heating_setpoint = convert_to_float(
        str(old_state.attributes.get("temperature", None)),
        self.name,
        "trigger_trv_change()",
    )
    _new_heating_setpoint = convert_to_float(
        str(new_state.attributes.get("temperature", None)),
        self.name,
        "trigger_trv_change()",
    )
    if (
        _new_heating_setpoint is not None
        and _old_heating_setpoint is not None
        and self.bt_hvac_mode is not HVACMode.OFF
    ):
        _LOGGER.debug(
            f"better_thermostat {self.name}: trigger_trv_change / _old_heating_setpoint: {_old_heating_setpoint} - _new_heating_setpoint: {_new_heating_setpoint} - _last_temperature: {self.real_trvs[entity_id]['last_temperature']}"
        )
        if (
            _new_heating_setpoint < self.bt_min_temp
            or self.bt_max_temp < _new_heating_setpoint
        ):
            _LOGGER.warning(
                f"better_thermostat {self.name}: New TRV {entity_id} setpoint outside of range, overwriting it"
            )

            if _new_heating_setpoint < self.bt_min_temp:
                _new_heating_setpoint = self.bt_min_temp
            else:
                _new_heating_setpoint = self.bt_max_temp

        if (
            self.bt_target_temp != _new_heating_setpoint
            and _old_heating_setpoint != _new_heating_setpoint
            and self.real_trvs[entity_id]["last_temperature"] != _new_heating_setpoint
            and not child_lock
            and self.real_trvs[entity_id]["target_temp_received"] is True
            and self.real_trvs[entity_id]["system_mode_received"] is True
            and self.real_trvs[entity_id]["hvac_mode"] is not HVACMode.OFF
            and self.window_open is False
        ):
            _LOGGER.debug(
                f"better_thermostat {self.name}: TRV {entity_id} decoded TRV target temp changed from {self.bt_target_temp} to {_new_heating_setpoint}"
            )
            self.bt_target_temp = _new_heating_setpoint
            _main_change = True

        if self.real_trvs[entity_id]["advanced"].get("no_off_system_mode", False):
            if _new_heating_setpoint == self.real_trvs[entity_id]["min_temp"]:
                self.bt_hvac_mode = HVACMode.OFF
            else:
                self.bt_hvac_mode = HVACMode.HEAT
            _main_change = True

    if _main_change is True:
        self.async_write_ha_state()
        return await self.control_queue_task.put(self)
    self.async_write_ha_state()
    return


def update_hvac_action(self):
    """Update hvac action."""
    # return the most common action if it is not off
    states = [
        state
        for entity_id in self.real_trvs
        if (state := self.hass.states.get(entity_id)) is not None
    ]

    # check if trv has pi_heating_demand
    pi_heating_demands = list(find_state_attributes(states, "pi_heating_demand"))
    if pi_heating_demands:
        pi_heating_demand = max(pi_heating_demands)
        if pi_heating_demand > 1:
            self.attr_hvac_action = HVACAction.HEATING
            self.async_write_ha_state()
            return
        else:
            self.attr_hvac_action = HVACAction.IDLE
            self.async_write_ha_state()
            return

    hvac_actions = list(find_state_attributes(states, ATTR_HVAC_ACTION))
    if not hvac_actions:
        self.attr_hvac_action = None
        self.async_write_ha_state()
        return

    # return action off if all are off
    if all(a == HVACAction.OFF for a in hvac_actions):
        self.attr_hvac_action = HVACAction.OFF
    # else check if is heating
    elif self.bt_target_temp > self.cur_temp:
        self.attr_hvac_action = HVACAction.HEATING
    else:
        self.attr_hvac_action = HVACAction.IDLE

    self.async_write_ha_state()
    return


def convert_inbound_states(self, entity_id, state: State) -> str:
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

    remapped_state = mode_remap(self, entity_id, str(state.state), True)

    if remapped_state not in (HVACMode.OFF, HVACMode.HEAT):
        return None
    return remapped_state


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
            _new_heating_setpoint = self.bt_target_temp
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

                _new_heating_setpoint = self.bt_target_temp

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
                if hvac_mode == HVACMode.OFF:
                    _new_heating_setpoint = self.real_trvs[entity_id]["min_temp"]
                hvac_mode = None
            if (
                HVACMode.OFF not in _system_modes
                or self.real_trvs[entity_id]["advanced"].get(
                    "no_off_system_mode", False
                )
                is True
            ):
                if hvac_mode == HVACMode.OFF:
                    _LOGGER.debug(
                        f"better_thermostat {self.name}: sending 5°C to the TRV because this device has no system mode off and heater should be off"
                    )
                    _new_heating_setpoint = self.real_trvs[entity_id]["min_temp"]
                    hvac_mode = None

        return {
            "temperature": _new_heating_setpoint,
            "local_temperature": self.real_trvs[entity_id]["current_temperature"],
            "system_mode": hvac_mode,
            "local_temperature_calibration": _new_local_calibration,
        }
    except Exception as e:
        _LOGGER.error(e)
        return None
