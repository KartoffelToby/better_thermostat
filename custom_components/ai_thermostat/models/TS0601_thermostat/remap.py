from custom_components.ai_thermostat.models.utils import cleanState
from homeassistant.components.climate.const import (
    HVAC_MODE_AUTO,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF
)

def TS0601_thermostat_inbound(self,state,):
    if state.get('system_mode') is not None:
        temp_system_mode = state.get('system_mode')
        if state.get('system_mode') == HVAC_MODE_AUTO:
            temp_system_mode = HVAC_MODE_HEAT
        return cleanState(state.get('current_heating_setpoint'),state.get('local_temperature'),state.get('local_temperature_calibration'),temp_system_mode,True)
    else:
        return cleanState(state.get('current_heating_setpoint'),state.get('local_temperature'),state.get('local_temperature_calibration'),HVAC_MODE_OFF,True)

def TS0601_thermostat_outbound(self,hvac_mode):
    state = self.hass.states.get(self.heater_entity_id).attributes
    temp_system_mode = hvac_mode
    if hvac_mode == HVAC_MODE_HEAT:
        temp_system_mode = HVAC_MODE_AUTO
    return cleanState(self._target_temp,state.get('local_temperature'),state.get('local_temperature_calibration'),temp_system_mode,True)
