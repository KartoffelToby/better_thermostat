import asyncio
import math
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
  #new_calibration = float(round((float(self._cur_temp) - float(state.get('local_temperature'))) + float(state.get('local_temperature_calibration')),2))
  new_calibration = int(math.ceil((math.floor(float(self._cur_temp)) - round(float(state.get('local_temperature')))) + round(float(state.get('local_temperature_calibration')))))

  return new_calibration

async def overswing(self,calibration):
  state = self.hass.states.get(self.heater_entity_id).attributes
  mqtt = self.hass.components.mqtt
  if (datetime.now() > (self.lastOverswing + timedelta(minutes = 5))) and state.get('system_mode') is not None and self._target_temp is not None and self._cur_temp is not None and not self.night_status:
    check_overswing = (float(self._target_temp) - 0.5) < float(self._cur_temp)
    if check_overswing:
      self.ignoreStates = True
      _LOGGER.debug("Overswing detected")
      mqtt.async_publish('zigbee2mqtt/'+state.get('device').get('friendlyName')+'/set/current_heating_setpoint', float(5), 0, False)
      await asyncio.sleep(60)
      mqtt.async_publish('zigbee2mqtt/'+state.get('device').get('friendlyName')+'/set/current_heating_setpoint', float(calibration), 0, False)
      self.lastOverswing = datetime.now()
      self.ignoreStates = False

def temperature_calibration(self):
  state = self.hass.states.get(self.heater_entity_id).attributes
  new_calibration = abs(float(round((float(self._target_temp) - float(self._cur_temp)) + float(state.get('local_temperature')),2)))
  if new_calibration < float(self._min_temp):
      new_calibration = float(self._min_temp)
  if new_calibration > float(self._max_temp):
      new_calibration = float(self._max_temp)

  loop = asyncio.get_event_loop()
  loop.create_task(overswing(self,new_calibration))
            
  return new_calibration
