def fix_local_calibration(self, entity_id, offset):
    if offset > -1.5:
        offset -= 1.5
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
    return temperature
