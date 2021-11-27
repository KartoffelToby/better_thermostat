from custom_components.ai_thermostat.models.utils import cleanState, temperature_calibration

def GS361A_H04_thermostat_inbound(self,state):
    self.calibration_type = 1
    return cleanState(self._target_temp,state.get('local_temperature'),state.get('local_temperature_calibration'),state.get('system_mode'),True)


def GS361A_H04_thermostat_outbound(self,hvac_mode):
    state = self.hass.states.get(self.heater_entity_id).attributes

    self.calibration_type = 1
    new_calibration = temperature_calibration(self)
    return cleanState(self._target_temp,state.get('local_temperature'),state.get('local_temperature_calibration'),hvac_mode,True,new_calibration)
