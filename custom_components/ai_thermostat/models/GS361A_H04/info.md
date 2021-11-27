# Siterwell GS361A-H04

## Zigbee2Mqtt info:

|     |     |
|-----|-----|
| Model | GS361A-H04  |
| Vendor  | Siterwell  |
| Description | Radiator valve with thermostat |
| Exposes | lock (state), switch (state), battery, position, climate (current_heating_setpoint, local_temperature, system_mode, running_state), linkquality |
| Picture | ![Siterwell GS361A-H04](https://www.zigbee2mqtt.io/images/devices/GS361A-H04.jpg) |
| White-label | Essentials 120112, TuYa GTZ02, Revolt NX-4911, Unitec 30946, Tesla TSL-TRV-GS361A, Nedis ZBHTR10WT |



## Remapped behavior

| HA needed exposes | Z2M Exposes | Remapping |
|-----|-----|-----|
| system_mode | YES | Same |
| current_heating_setpoint | YES | used for calibration |
| local_temperature | YES | Same |