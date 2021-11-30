# Moes BRT-100-TRV

## Zigbee2Mqtt info:

|     |     |
|-----|-----|
| Model | BRT-100-TRV  |
| Vendor  | Moes  |
| Description | Thermostatic radiator valve |
| Exposes | battery, lock (state), eco_mode, eco_temperature, max_temperature, min_temperature, position, switch (state), window, climate (local_temperature, current_heating_setpoint, local_temperature_calibration, preset), programming_mode, boost_heating, boost_heating_countdown, boost_heating_countdown_time_set, linkquality |
| Picture | ![Moes BRT-100-TRV](https://www.zigbee2mqtt.io/images/devices/BRT-100-TRV.jpg) |


## Remapped behavior

| HA needed exposes | Z2M Exposes | Remapping |
|-----|-----|-----|
| system_mode | NO  | current_heating_setpoint is set to 5 if HVAC_MODE_OFF and back to target on HVAC_MODE_HEAT |
| current_heating_setpoint | YES | used for calibration |
| local_temperature | YES | isn't updated when calibration is set, there is a open [issue](https://github.com/Koenkk/zigbee2mqtt/issues/9486) we use a current_heating_setpoint calibration |