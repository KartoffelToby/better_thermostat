# TuYa TS0601_thermostat

## Zigbee2Mqtt info:

|     |     |
|-----|-----|
| Model | TS0601_thermostat  |
| Vendor  | TuYa  |
| Description | Radiator valve with thermostat |
| Exposes | lock (state), switch (state), battery_low, position, climate (current_heating_setpoint, local_temperature, system_mode, local_temperature_calibration, away_mode, preset), away_preset_days, boost_time, comfort_temperature, eco_temperature, force, max_temperature, min_temperature, week, away_preset_temperature, linkquality |
| Picture | ![TuYa TS0601_thermostat](https://www.zigbee2mqtt.io/images/devices/TS0601_thermostat.jpg) |
| White-label | Moes HY368, Moes HY369RT, SHOJZJ 378RT, Silvercrest TVR01 |


## Remapped behavior

| HA needed exposes | Z2M Exposes | Remapping |
|-----|-----|-----|
| system_mode | YES | HVAC_MODE_HEAT are remapped to HVAC_MODE_AUTO in HA HVAC_MODE_OFF is the same |
| current_heating_setpoint | YES | used for calibration |
| local_temperature | YES | Same |