"""
This model fix is due to any floating point number being set to +/- 1 million by Zigbee2MQTT for the local_calibration
"""

import math


def fix_local_calibration(self, entity_id, offset):
    """
    If still heating, round UP the offset

    This creates a lower "fake" thermostat temperature, making it heat the room
    """
    if self.cur_temp < self.bt_target_temp:
        offset = math.ceil(offset)
    else:
        offset = math.floor(offset)

    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return False


async def override_set_temperature(self, entity_id, temperature):
    return False
