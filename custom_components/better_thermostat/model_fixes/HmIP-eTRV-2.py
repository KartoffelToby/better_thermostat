def fix_local_calibration(self, entity_id, offset):
    return offset

def fix_target_temperature_calibration(self, entity_id, temperature):
	"""
	Fixes endless heating loops caused by homeaticIP device algorithm heating already when temperature is not below the set temperature yet. 
	Adds a bigger delta of at least -1.5 when not trying to heat.
    """

    _cur_trv_temp = float(
        self.hass.states.get(entity_id).attributes["current_temperature"]
    )
    if _cur_trv_temp is None:
        return temperature
    if temperature < _cur_trv_temp:
        temperature -= 1.5

    return temperature

async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return False

async def override_set_temperature(self, entity_id, temperature):
    return False
