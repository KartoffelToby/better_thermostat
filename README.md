# AI THERMOSTAT

JUST IN WIP Dont use it at this time.

## EXAMPLE OF SETUP
You need to configure one "virtual" thermostat for every used thermostat in the `configuration.yaml` file.

Here below the example of manual setup of sensor and parameters to configure.
```yaml
climate:
  - platform: ai_thermostat
    name: room
    thermostat: climate.tvr
    type: SPZB // for EUROTRONIC SPZB0001 Zigbee
    temperature_sensor: sensor.temperature
    window_sensors: group.office_windows
    initial_hvac_mode: "off"
    away_temp: 15
```
