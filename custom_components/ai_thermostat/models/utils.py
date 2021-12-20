import asyncio
from custom_components.ai_thermostat.helpers import convert_decimal
from homeassistant.helpers.json import JSONEncoder
import logging
from datetime import datetime, timedelta
_LOGGER = logging.getLogger(__name__)


class cleanState:
  def __init__(self, current_temperature, local_temperature,local_temperature_calibration,system_mode,has_real_mode = True, calibration = 0):
    self.current_temperature = current_temperature
    self.local_temperature = local_temperature
    self.local_temperature_calibration = local_temperature_calibration
    self.system_mode = system_mode
    self.has_real_mode = has_real_mode
    self.calibration = calibration

def default_calibration(self):
  state = self.hass.states.get(self.heater_entity_id).attributes
  #new_calibration = int(math.ceil((math.floor(float(self._cur_temp)) - round(float(state.get('local_temperature')))) + round(float(state.get('local_temperature_calibration')))))
  # temp range fix
  new_calibration = float((float(self._cur_temp) - float(state.get('local_temperature'))) + float(state.get('local_temperature_calibration')))
  if new_calibration > 0 and new_calibration < 1:
    new_calibration = round(new_calibration)
  if new_calibration < -30:
      new_calibration = -30
  if new_calibration > 30:
      new_calibration = 30

  return convert_decimal(new_calibration)

async def dampening(self, calibration):
  state = self.hass.states.get(self.heater_entity_id).attributes
  mqtt = self.hass.components.mqtt
  if (datetime.now() > (self.start_dampening_event + timedelta(minutes = 15))) and state.get('system_mode') is not None and self._target_temp is not None and self._cur_temp is not None and not self.night_status:
    # check if dampening is needed
    if (float(self._target_temp) - 0.5) < float(self._cur_temp):
      self.ignoreStates = True
      _LOGGER.debug("Dampening event started")
      await mqtt.async_publish(self.hass,'zigbee2mqtt/'+state.get('device').get('friendlyName')+'/set/current_heating_setpoint', float(5), 0, False)
      await asyncio.sleep(60)
      await mqtt.async_publish(self.hass,'zigbee2mqtt/'+state.get('device').get('friendlyName')+'/set/current_heating_setpoint', float(calibration), 0, False)
      self.start_dampening_event = datetime.now()
      self.ignoreStates = False

def temperature_calibration(self):
  state = self.hass.states.get(self.heater_entity_id).attributes
  new_calibration = abs(float(round((float(self._target_temp) - float(self._cur_temp)) + float(state.get('local_temperature')),2)))
  if new_calibration < float(self._min_temp):
      new_calibration = float(self._min_temp)
  if new_calibration > float(self._max_temp):
      new_calibration = float(self._max_temp)

  #loop = asyncio.get_event_loop()
  #loop.create_task(dampening(self,new_calibration))
            
  return new_calibration
