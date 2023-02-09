def fix_local_calibration(self, entity_id, offset):
    # device SEA802 fix
    if (self.cur_temp - self.bt_target_temp) < -0.2:
        offset -= 2.5

    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    # device SEA802 fix
    _cur_trv_temp = float(
        self.hass.states.get(entity_id).attributes["current_temperature"]
    )
    if _cur_trv_temp is None:
        return temperature
    if (
        round(temperature, 1) > round(_cur_trv_temp, 1)
        and temperature - _cur_trv_temp < 2.5
    ):
        temperature += 2.5
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return False


async def override_set_temperature(self, entity_id, temperature):
    return False
