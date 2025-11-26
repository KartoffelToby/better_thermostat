"""Model fixes for SPZB0001 devices.

Device-specific quirks for SPZB0001 thermostats handled by the
Better Thermostat integration.
"""


def fix_local_calibration(self, entity_id, offset):
    """Clamp local calibration to safe bounds for SPZB0001 devices."""
    if offset > 5:
        offset = 5
    elif offset < -5:
        offset = -5
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return a possibly adjusted target temperature for SPZB0001.

    Currently a no-op.
    """
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """Do not override HVAC mode for SPZB0001 devices."""
    return False


async def override_set_temperature(self, entity_id, temperature):
    """Do not override temperature sets for SPZB0001 devices."""
    return False
