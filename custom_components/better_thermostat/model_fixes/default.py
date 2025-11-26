"""Default model quirks passthrough for unknown devices.

These helpers implement safe no-op defaults for devices that do not
require specific quirks.
"""


def fix_local_calibration(self, entity_id, offset):
    """Return the given local calibration offset unchanged."""
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return the given target temperature unchanged."""
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """Do not override HVAC mode by default."""
    return False


async def override_set_temperature(self, entity_id, temperature):
    """Do not override set temperature by default."""
    return False


async def override_set_valve(self, entity_id, percent: int):
    """Do not override valve by default."""
    return False


async def override_set_valve(self, entity_id, percent: int):
    return False
