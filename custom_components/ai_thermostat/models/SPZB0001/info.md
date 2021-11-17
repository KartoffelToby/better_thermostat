# Eurotronic SPZB0001

## Zigbee2Mqtt info:

|     |     |
|-----|-----|
| Model | SPZB0001  |
| Vendor  | Eurotronic  |
| Description | Spirit Zigbee wireless heater thermostat |
| Exposes | battery, climate (occupied_heating_setpoint, local_temperature, system_mode, running_state, local_temperature_calibration, pi_heating_demand), trv_mode, valve_position, linkquality |
| Picture | ![Eurotronic SPZB0001](https://www.zigbee2mqtt.io/images/devices/SPZB0001.jpg) |


## Remapped behavior

| HA needed exposes | Z2M Exposes | Remapping |
|-----|-----|-----|
| system_mode | YES | HVAC_MODE_HEAT are remapped to HVAC_MODE_AUTO in HA HVAC_MODE_OFF is the same |
| local_temperature_calibration | YES | Same |
| occupied_heating_setpoint | YES | in HA current_heating_setpoint and occupied_heating_setpoint are the same, nothing to remap |
| local_temperature | YES | Must be force update with mqtt get local_temperature_calibration (done in climate.py) |