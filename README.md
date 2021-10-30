# SPZB0001 THERMOSTAT
A clone created from the Home Assistant generic_thermostat to use EUROTRONIC Zigbee SPZB0001 thermostats with external temperature sensors.

# HOW TO INSTALL
Just copy paste the content of the climate.spzb_thermostat/custom_components folder in your config/custom_components directory.

As example you will get the '.py' file in the following path: /config/custom_components/spzb0001_thermostat/climate.py.

## EXAMPLE OF SETUP
You need to configure one virtual spzb0001_thermostat for every used EUROTRONIC Zigbee SPZB0001 thermostat in the `configuration.yaml` file.

Here below the example of manual setup of sensor and parameters to configure.
```yaml
climate:
  - platform: spzb0001_thermostat
    name: room
    heater: switch.heater
    target_sensor: sensor.temperature
    target_temp: 18    
    initial_hvac_mode: "heat"
    away_temp: 15
```

Field | Value | Necessity | Comments
--- | --- | --- | ---
platform | `spzb0001_thermostat` | *Required* |
name| SPZB0001 Thermostat | *Conditional* | Used to distinguish the virtual thermostats
heater |  | *Conditional* | Switch that will activate/deactivate the heating system. This can be only a single EUROTRONIC SPZB0001 Zigbee entity.
target_sensor |  | *Required* | Sensor that is used for the actual temperature input of the thermostat.
target_temp | 18 | Optional |Temperature used for initialization after Home Assistant has started.
initial_hvac_mode | "heat" | *Conditional* | "heat" or "off", what you prefer as the initial startup value of the thermostat.
away_temp | 15 | Optional | Temperature used if the tag away is set.

## ADDITIONAL INFO
This custom component replicates the original generic_thermostat component from Home Assistant to integrate the EUROTRONIC SPZB0001 Zigbee thermostat while using an external temperature sensor for the room temperature. It is stripped down to the necessary only and working configuration options (see above). Lower and upper temperature are hardcoded to reflect the deCONZ integration.

You still need the original EUROTRONIC SPZB0001 Zigbee thermostat as an identy in Home Assistant (best used with the official deCONZ Add-On). The new spzb0001_thermostat just controls this device in the following matter:

As the "intelligence" using this custom components relies on Home Assistant the EUROTRONIC SPZB0001 Zigbee thermostat is logically degraded to work as a heater. To do this properly the custom component only uses the modes `HVAC_MODE_AUTO` and `ATTR_TEMPERATURE=max_temp`. `max_temp` is hardcoded to ensure that the valve of the EUROTRONIC SPZB0001 Zigbee thermostat is physically fully opened (value=255). As soon as the deCONZ-integration of this thermostats allow the direct control of the valve itself I will change this integration accordingly to avoid the hassles with the HVAC modes.

The EUROTRONIC SPZB0001 Zigbee thermostat can't be used as a normal heater switch (`STATE_ON`, `STATE_OFF`) as it only knows `HVAC_MODE_OFF`, `HVAC_MODE_AUTO`, `HVAC_MODE_HEAT` and automatically changes from `HVAC_MODE_HEAT` to `HVAC_MODE_AUTO` after some time. This behaviour can't actually be changed. So the spzb0001_thermostat uses the `HVAC_MODE_AUTO` and the `ATTR_TEMPERATURE=max_temp` (30°C per deCONZ integration) to control the EUROTRONIC SPZB0001 Zigbee thermostat and cause it to fully open the valve.
To switch off the EUROTRONIC SPZB0001 Zigbee thermostat the spzb0001_thermostat uses the `ATTR_TEMPERATURE=min_temp` (5°C per deCONZ integration) and the `HVAC_MODE_OFF` with tested delays to prevent inconsistent states.

For documentation purposes:
If you or any automation toggles the EUROTRONIC SPZB0001 Zigbee thermostat to heat this custom component first sends `HVAC_MODE_HEAT` and 5 seconds later `ATTR_TEMPERATURE=max_temp`. The time of 5 seconds is enough if you assume that the EUROTRONIC SPZB0001 Zigbee thermostat is only controlled by this custom component (so the original state is `HVAC_MODE_OFF` and `ATTR_TEMPERATURE=min_temp`).
If you or any automation toggles the EUROTRONIC SPZB0001 Zigbee thermostat to off this custom component first sends `ATTR_TEMPERATURE=min_temp` and 30 seconds later `HVAC_MODE_OFF`. The time of 30 seconds are usually enough that the EUROTRONIC SPZB0001 Zigbee thermostat fully closed its valve before processing another externally send command. For any thermostats that missed that command for some unknown reason the custom component also sends a `STATE_OFF` (which internally is exactly the same as `HVAC_MODE_OFF`) to the HA service another 30 seconds later. At least I never had a thermostat that did not switch off so far using this method.

For controlling purposes you can visually add the original EUROTRONIC SPZB0001 Zigbee thermostats to another lovelace view to compare the states of the virtual spzb0001_thermostat and the corresponding EUROTRONIC SPZB0001 Zigbee thermostat.
