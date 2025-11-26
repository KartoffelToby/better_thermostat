"""Zigbee2MQTT local calibration value fix.

This model fix addresses an issue where Zigbee2MQTT reports incorrect
floating-point values (e.g., +/- 1e6) for the local calibration. The
helper functions below normalize values to sensible integer offsets.
"""

import math


def fix_local_calibration(self, entity_id, offset):
    """Sanitize and normalize a reported calibration offset.

    Rounds to the nearest integer (towards ceiling if the room is heating)
    to recover from the erroneous float values produced by some Zigbee
    integrations.
    """
    if self.cur_temp < self.bt_target_temp:
        offset = math.ceil(offset)
    else:
        offset = math.floor(offset)

    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """No-op target-temperature calibration fix for this model."""
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """No override on system mode for this model."""
    return False


async def override_set_temperature(self, entity_id, temperature):
    """No temperature override for this model."""
    return False
