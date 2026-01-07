"""Model quirks for generic TS0601 Zigbee thermostats.

Contains model-specific handling for known quirks in TS0601-based devices.
"""


def fix_local_calibration(self, entity_id, offset):
    """Normalize local calibration offset for TS0601 devices.

    This function performs model-specific rounding/adjustment to avoid
    spurious values that would lead to incorrect behavior.
    """
    _cur_external_temp = self.cur_temp
    _target_temp = self.bt_target_temp

    if (_cur_external_temp + 0.1) >= _target_temp:
        offset = round(offset + 0.5, 1)
    elif (_cur_external_temp + 0.5) >= _target_temp:
        offset -= 2.5

    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Adjust target temperature calibration for TS0601 devices.

    Ensures a minimum distance between the current TRV internal temperature
    and the requested setpoint to avoid oscillation.
    """
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

    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """No special HVAC mode override for TS0601 devices."""
    return False


async def override_set_temperature(self, entity_id, temperature):
    """No special set_temperature override for TS0601 devices."""
    return False
