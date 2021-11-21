from custom_components.better_thermostat.models.utils import cleanState
from homeassistant.components.climate.const import (
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF
)

def BRT_100_TRV_inbound(self,state):
    self.calibration_type = 1
    temp_system_mode = HVAC_MODE_HEAT
    if state.get('current_heating_setpoint') <= 5:
        temp_system_mode = HVAC_MODE_OFF
    return cleanState(self._target_temp,state.get('local_temperature'),state.get('local_temperature_calibration'),temp_system_mode,False)

def BRT_100_TRV_outbound(self,hvac_mode):
    state = self.hass.states.get(self.heater_entity_id).attributes
    temp_target_temp = self._target_temp
    if hvac_mode == HVAC_MODE_OFF:
        temp_target_temp = 0
        hvac_mode = HVAC_MODE_HEAT

    self.calibration_type = 1
    new_calibration = abs(float(round(float(self._target_temp) - (float(self._cur_temp) - float(state.get('local_temperature'))),1)))
    if new_calibration < float(self._min_temp):
        new_calibration = float(self._min_temp)
    if new_calibration > float(self._max_temp):
        new_calibration = float(self._max_temp)
    return cleanState(temp_target_temp,state.get('local_temperature'),state.get('local_temperature_calibration'),hvac_mode,False,new_calibration)
