def fix_local_calibration(self, entity_id, offset):
    if offset > 5:
        offset = 5
    elif offset < -5:
        offset = -5
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return False


async def override_set_temperature(self, entity_id, temperature):
    return False
