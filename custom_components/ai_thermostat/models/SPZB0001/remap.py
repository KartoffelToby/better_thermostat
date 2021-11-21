from custom_components.better_thermostat.models.utils import cleanState
from homeassistant.components.climate.const import (
    HVAC_MODE_AUTO,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF
)

def SPZB0001_inbound(self,state,):
    if state.get('system_mode') is not None:
        temp_system_mode = state.get('system_mode')
        if state.get('system_mode') == HVAC_MODE_AUTO:
            temp_system_mode = HVAC_MODE_HEAT
        return cleanState(state.get('current_heating_setpoint'),state.get('local_temperature'),state.get('local_temperature_calibration'),temp_system_mode,True)
    else:
        return cleanState(state.get('current_heating_setpoint'),state.get('local_temperature'),state.get('local_temperature_calibration'),HVAC_MODE_OFF,True)

def SPZB0001_outbound(self,hvac_mode):
    state = self.hass.states.get(self.heater_entity_id).attributes
    temp_system_mode = hvac_mode
    if hvac_mode == HVAC_MODE_HEAT:
        temp_system_mode = HVAC_MODE_AUTO
    self.calibration_type = 0
    new_calibration = float(round(float(self._cur_temp) - (float(state.get('local_temperature')) - float(state.get('local_temperature_calibration'))),1))
    return cleanState(self._target_temp,state.get('local_temperature'),state.get('local_temperature_calibration'),temp_system_mode,True,new_calibration)
