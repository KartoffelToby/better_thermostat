def fix_local_calibration(self, entity_id, offset):
    if (self.cur_temp - 0.5) <= self.bt_target_temp:
        offset -= 2.5
    elif (self.cur_temp + 0.10) >= self.bt_target_temp:
        offset = round(offset + 0.5, 1)
    if (self.cur_temp + 0.5) > self.bt_target_temp:
        offset += 1
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    _cur_trv_temp = float(
        self.hass.states.get(entity_id).attributes["current_temperature"]
    )
    if _cur_trv_temp is None:
        return temperature
    if (
        round(temperature, 1) > round(_cur_trv_temp, 1)
        and temperature - _cur_trv_temp < 1.5
    ):
        temperature += 1.5
    if (self.cur_temp + 0.5) > self.bt_target_temp:
        temperature -= 2
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return False


async def override_set_temperature(self, entity_id, temperature):
    return False
