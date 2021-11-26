from custom_components.ai_thermostat.models.utils import cleanState, default_calibration
from homeassistant.components.climate.const import (
    HVAC_MODE_AUTO,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF
)

def SPZB0001_inbound(self,state):
    if state.get('system_mode') is not None:
        temp_system_mode = state.get('system_mode')
        if state.get('system_mode') == HVAC_MODE_AUTO:
            temp_system_mode = HVAC_MODE_HEAT
        return cleanState(state.get('current_heating_setpoint'),state.get('local_temperature'),state.get('local_temperature_calibration'),temp_system_mode,True)

def SPZB0001_outbound(self,hvac_mode):
    state = self.hass.states.get(self.heater_entity_id).attributes
    temp_system_mode = hvac_mode
    if hvac_mode == HVAC_MODE_HEAT:
        temp_system_mode = HVAC_MODE_AUTO
    self.calibration_type = 0
    new_calibration = default_calibration(self)
    return cleanState(self._target_temp,state.get('local_temperature'),state.get('local_temperature_calibration'),temp_system_mode,True,new_calibration)
