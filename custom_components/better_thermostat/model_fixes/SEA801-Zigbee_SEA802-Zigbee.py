def fix_local_calibration(self, entity_id, offset):
    _cur_external_temp = self.cur_temp
    _target_temp = self.bt_target_temp

    if (_cur_external_temp + 0.1) >= _target_temp:
        offset = round(offset + 0.5, 1)
    elif (_cur_external_temp + 0.5) >= _target_temp:
        offset -= 2.5

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
        # Statt die gewünschte Temperatur pauschal um 1.5°C zu erhöhen,
        # setze sie auf mindestens (aktuelle TRV-Temp + 1.5°C).
        # So ist der Mindestabstand garantiert, ohne unnötig zu überschießen.
        temperature = round(_cur_trv_temp + 1.5, 1)

    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return False


async def override_set_temperature(self, entity_id, temperature):
    return False
