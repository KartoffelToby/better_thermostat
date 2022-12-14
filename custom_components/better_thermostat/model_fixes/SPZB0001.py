def fix_local_calibration(self, entity_id, offset):
    if (self.cur_temp + 0.5) > self.bt_target_temp:
        offset += 3
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    if (self.cur_temp + 0.5) > self.bt_target_temp:
        temperature -= 3
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return False


async def override_set_temperature(self, entity_id, temperature):
    return False
