import asyncio
from datetime import datetime
import logging
from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP

from homeassistant.components.climate.const import (
    HVACMode,
    ATTR_HVAC_ACTION,
    HVACAction,
)
from homeassistant.core import State, callback
from homeassistant.components.group.util import find_state_attributes
from custom_components.better_thermostat.utils.helpers import (
    convert_to_float,
    mode_remap,
)
from custom_components.better_thermostat.adapters.delegate import get_current_offset

from custom_components.better_thermostat.utils.const import (
    CalibrationType,
    CalibrationMode,
)

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_trv_change(self, event):
    """Trigger a change in the trv state."""
    if self.startup_running:
        return
    if self.control_queue_task is None:
        return
    if self.bt_target_temp is None or self.cur_temp is None or self.tolerance is None:
        return
    asyncio.create_task(update_hvac_action(self))
    _main_change = False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    entity_id = event.data.get("entity_id")

    if None in (new_state, old_state, new_state.attributes):
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: TRV {entity_id} update contained not all necessary data for processing, skipping"
        )
        return

    if not isinstance(new_state, State) or not isinstance(old_state, State):
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: TRV {entity_id} update contained not a State, skipping"
        )
        return
    # set context HACK TO FIND OUT IF AN EVENT WAS SEND BY BT

    # Check if the update is coming from the code
    if self.context == event.context:
        return

    # _LOGGER.debug(f"better_thermostat {self.device_name}: TRV {entity_id} update received")

    _org_trv_state = self.hass.states.get(entity_id)
    child_lock = self.real_trvs[entity_id]["advanced"].get("child_lock")

    _new_current_temp = convert_to_float(
        str(_org_trv_state.attributes.get("current_temperature", None)),
        self.device_name,
        "TRV_current_temp",
    )

    _time_diff = 5
    try:
        for trv in self.all_trvs:
            if trv["advanced"][CONF_HOMEMATICIP]:
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
                and self.real_trvs[entity_id]["calibration"] != 1
            )
        )
    ):
        _old_temp = self.real_trvs[entity_id]["current_temperature"]
        self.real_trvs[entity_id]["current_temperature"] = _new_current_temp
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: TRV {entity_id} sends new internal temperature from {_old_temp} to {_new_current_temp}"
        )
        self.last_internal_sensor_change = datetime.now()
        _main_change = True

        # TODO: async def in controlling?
        if self.real_trvs[entity_id]["calibration_received"] is False:
            self.real_trvs[entity_id]["calibration_received"] = True
            _LOGGER.debug(
                f"better_thermostat {self.device_name}: calibration accepted by TRV {entity_id}"
            )
            _main_change = False
            if self.real_trvs[entity_id]["calibration"] == 0:
                self.real_trvs[entity_id]["last_calibration"] = (
                    await get_current_offset(self, entity_id)
                )

    if self.ignore_states:
        return

    try:
        mapped_state = convert_inbound_states(self, entity_id, _org_trv_state)
    except TypeError:
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: remapping TRV {entity_id} state failed, skipping"
        )
        return

    if mapped_state in (HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL):
        if (
            self.real_trvs[entity_id]["hvac_mode"] != _org_trv_state.state
            and not child_lock
        ):
            _old = self.real_trvs[entity_id]["hvac_mode"]
            _LOGGER.debug(
                f"better_thermostat {self.device_name}: TRV {entity_id} decoded TRV mode changed from {_old} to {_org_trv_state.state} - converted {new_state.state}"
            )
            self.real_trvs[entity_id]["hvac_mode"] = _org_trv_state.state
            _main_change = True
            if (
                child_lock is False
                and self.real_trvs[entity_id]["system_mode_received"] is True
                and self.real_trvs[entity_id]["last_hvac_mode"] != _org_trv_state.state
            ):
                self.bt_hvac_mode = mapped_state

    _main_key = "temperature"
    if "temperature" not in old_state.attributes:
        _main_key = "target_temp_low"

    _old_heating_setpoint = convert_to_float(
        str(old_state.attributes.get(_main_key, None)),
        self.device_name,
        "trigger_trv_change()",
    )
    _new_heating_setpoint = convert_to_float(
        str(new_state.attributes.get(_main_key, None)),
        self.device_name,
        "trigger_trv_change()",
    )
    if (
        _new_heating_setpoint is not None
        and _old_heating_setpoint is not None
        and self.bt_hvac_mode is not HVACMode.OFF
    ):
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: trigger_trv_change test / _old_heating_setpoint: {_old_heating_setpoint} - _new_heating_setpoint: {_new_heating_setpoint} - _last_temperature: {self.real_trvs[entity_id]['last_temperature']}"
        )
        if (
            _new_heating_setpoint < self.bt_min_temp
            or self.bt_max_temp < _new_heating_setpoint
        ):
            _LOGGER.warning(
                f"better_thermostat {self.device_name}: New TRV {entity_id} setpoint outside of range, overwriting it"
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
                f"better_thermostat {self.device_name}: TRV {entity_id} decoded TRV target temp changed from {self.bt_target_temp} to {_new_heating_setpoint}"
            )
            self.bt_target_temp = _new_heating_setpoint
            if self.cooler_entity_id is not None:
                if self.bt_target_temp <= self.bt_target_cooltemp:
                    self.bt_target_cooltemp = (
                        self.bt_target_temp - self.bt_target_temp_step
                    )
                if self.bt_target_temp >= self.bt_target_cooltemp:
                    self.bt_target_cooltemp = (
                        self.bt_target_temp - self.bt_target_temp_step
                    )

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


async def update_hvac_action(self):
    self.bt_target_temp = 5.0 if self.bt_target_temp is None else self.bt_target_temp
    self.cur_temp = 5.0 if self.cur_temp is None else self.cur_temp
    self.tolerance = 0.0 if self.tolerance is None else self.tolerance
    """Update the hvac action."""
    if self.startup_running or self.control_queue_task is None:
        return

    hvac_action = None

    # i don't know why this is here just for hometicip / wtom - 2023-08-23
    # for trv in self.all_trvs:
    #     if trv["advanced"][CONF_HOMEMATICIP]:
    #         entity_id = trv["trv"]
    #         state = self.hass.states.get(entity_id)
    #         if state is None:
    #             continue

    #         if state.attributes.get(ATTR_HVAC_ACTION) == HVACAction.HEATING:
    #             hvac_action = HVACAction.HEATING
    #             break
    #         elif state.attributes.get(ATTR_HVAC_ACTION) == HVACAction.IDLE:
    #             hvac_action = HVACAction.IDLE

    # return the most common action if it is not off
    states = [
        state
        for entity_id in self.real_trvs
        if (state := self.hass.states.get(entity_id)) is not None
    ]

    hvac_actions = list(find_state_attributes(states, ATTR_HVAC_ACTION))

    if not hvac_actions:
        self.attr_hvac_action = None
    # return action off if all are off
    elif all(a == HVACAction.OFF for a in hvac_actions):
        hvac_action = HVACAction.OFF
    # else check if is heating
    elif (
        self.bt_target_temp > self.cur_temp + self.tolerance
        and self.window_open is False
    ):
        hvac_action = HVACAction.HEATING
    elif (
        self.bt_target_temp > self.cur_temp
        and self.attr_hvac_action == HVACAction.HEATING
        and self.window_open is False
    ):
        hvac_action = HVACAction.HEATING
    else:
        hvac_action = HVACAction.IDLE

    if self.hvac_action != hvac_action:
        self.attr_hvac_action = hvac_action
        await self.async_update_ha_state(force_refresh=True)


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


def convert_outbound_states(self, entity_id, hvac_mode) -> dict | None:
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
        _calibration_type = self.real_trvs[entity_id]["advanced"].get("calibration")
        _calibration_mode = self.real_trvs[entity_id]["advanced"].get(
            "calibration_mode"
        )

        if _calibration_type is None:
            _LOGGER.warning(
                "better_thermostat %s: no calibration type found in device config, talking to the TRV using fallback mode",
                self.device_name,
            )
            _new_heating_setpoint = self.bt_target_temp
            _new_local_calibration = calculate_calibration_local(self, entity_id)

            if _new_local_calibration is None:
                return None

        else:
            if _calibration_type == CalibrationType.LOCAL_BASED:
                _new_local_calibration = calculate_calibration_local(self, entity_id)

                _new_heating_setpoint = self.bt_target_temp

            elif _calibration_type == CalibrationType.TARGET_TEMP_BASED:
                if _calibration_mode == CalibrationMode.NO_CALIBRATION:
                    _new_heating_setpoint = self.bt_target_temp
                else:
                    _new_heating_setpoint = calculate_calibration_setpoint(
                        self, entity_id
                    )

            _system_modes = self.real_trvs[entity_id]["hvac_modes"]
            _has_system_mode = _system_modes is not None

            # Handling different devices with or without system mode reported or contained in the device config

            hvac_mode = mode_remap(self, entity_id, str(hvac_mode), False)

            if not _has_system_mode:
                _LOGGER.debug(
                    f"better_thermostat {self.device_name}: device config expects no system mode, while the device has one. Device system mode will be ignored"
                )
                if hvac_mode == HVACMode.OFF:
                    _new_heating_setpoint = self.real_trvs[entity_id]["min_temp"]
                hvac_mode = None
            if hvac_mode == HVACMode.OFF and (
                HVACMode.OFF not in _system_modes
                or self.real_trvs[entity_id]["advanced"].get("no_off_system_mode")
            ):
                _min_temp = self.real_trvs[entity_id]["min_temp"]
                _LOGGER.debug(
                    f"better_thermostat {self.device_name}: sending {_min_temp}°C to the TRV because this device has no system mode off and heater should be off"
                )
                _new_heating_setpoint = _min_temp
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
