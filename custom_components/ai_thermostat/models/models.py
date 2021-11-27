import logging
from custom_components.ai_thermostat.models.BRT_100_TRV.remap import BRT_100_TRV_inbound, BRT_100_TRV_outbound
from custom_components.ai_thermostat.models.GS361A_H04.remap import GS361A_H04_thermostat_inbound, GS361A_H04_thermostat_outbound
from custom_components.ai_thermostat.models.SPZB0001.remap import SPZB0001_inbound, SPZB0001_outbound
from custom_components.ai_thermostat.models.TS0601_thermostat.remap import TS0601_thermostat_inbound, TS0601_thermostat_outbound
from custom_components.ai_thermostat.models.utils import cleanState, default_calibration
from homeassistant.components.climate.const import (
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF
)
_LOGGER = logging.getLogger(__name__)

def convert_inbound_states(self,state):
    try:
        if self.hass.states.get(self.heater_entity_id).attributes.get('device') is not None:
            self.model = self.hass.states.get(self.heater_entity_id).attributes.get('device').get('model') 
        else:
            _LOGGER.exception("ai_thermostat: can't read the device model of TVR, Enable include_device_information in z2m or checkout issue #1")
    except RuntimeError:
        _LOGGER.exception("ai_thermostat: error can't get the TRV")



    if(self.model == "SPZB0001"):
        return SPZB0001_inbound(self,state)
    elif(self.model == "BRT-100-TRV"):
        return BRT_100_TRV_inbound(self,state)
    elif(self.model == "TS0601_thermostat"):
        return TS0601_thermostat_inbound(self,state)
    elif(self.model == "GS361A-H04"):
        return GS361A_H04_thermostat_inbound(self,state)
    else:
        if state.get('system_mode') == HVAC_MODE_HEAT or state.get('system_mode') == HVAC_MODE_OFF:
            temp_system_mode = state.get('system_mode')
        else:
            temp_system_mode = HVAC_MODE_HEAT
        return cleanState(self._target_temp,state.get('local_temperature'),state.get('local_temperature_calibration'),temp_system_mode,True)
    

def convert_outbound_states(self,hvac_mode):
    try:
        if self.hass.states.get(self.heater_entity_id).attributes.get('device') is not None:
            self.model = self.hass.states.get(self.heater_entity_id).attributes.get('device').get('model')
        else:
            _LOGGER.exception("ai_thermostat: can't read the device model of TVR, Enable include_device_information in z2m or checkout issue #1")
    except RuntimeError:
        _LOGGER.exception("ai_thermostat: error can't get the TRV")



    if(self.model == "SPZB0001"):
        return SPZB0001_outbound(self,hvac_mode)
    elif(self.model == "BRT-100-TRV"):
        return BRT_100_TRV_outbound(self,hvac_mode)
    elif(self.model == "TS0601_thermostat"):
        return TS0601_thermostat_outbound(self,hvac_mode)
    elif(self.model == "GS361A-H04"):
        return GS361A_H04_thermostat_outbound(self,hvac_mode)
    else:
        self.calibration_type = 0
        state = self.hass.states.get(self.heater_entity_id).attributes
        new_calibration = default_calibration(self)
        return cleanState(self._target_temp,state.get('local_temperature'),state.get('local_temperature_calibration'),hvac_mode,True,new_calibration)