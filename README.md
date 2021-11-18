# AI THERMOSTAT for Zigbee2MQTT

This integration brings some smartness to your TRV Zigbee2MQTT setup.

What does it? Basically, it combines an external temperature sensor, window/door Sensors, and a weather-Entity, so your TRV is calibrated with the temperature from the external sensor and turned off if some window is open. It also turns off the thermostat if a configured outside temperature is reached for two days in a row, so if it's outside warm enough you didn't need useless heating in your rooms.

It's also useful for those who only need an off and heating state and controlled the room temperature with a set target temperature based on an external temperature sensor.

So if you use the Google Assistant integration to control your thermostat you no longer get into an issue of incompatible modes or the problem that a target temp can't be set because the device is in "auto" mode that's remapped to eco.

Youst set your Target Heat point with your voice or the Google Home app and you are good to go.

At this time I tested it with two models: (but basically all zigbee2mqtt TRV should work.)

- Eurotronic Spirit Zigbee (SPZB0001)
- Moes SEA801-Zigbee/SEA802-Zigbee
- TuYa TS0601_thermostat (TS0601)
- BRT-100-TRV (didn't work at all because of this [issue](https://github.com/Koenkk/zigbee2mqtt/issues/9486)) 

The SPZB0001 is Special, it uses the "heat" mode for boost and the auto mode for the "normal" operation, to avoid that it remaps heat with auto internally, the boost mode is lost with this configuration.

If you have a special Thermostat like the SPZB0001 feel free to open an issue or pull request.

The integration gets the Model identifier automatic, nothing to do here.

**IMPORTANT: be sure to enable "legacy" in Zigbee2MQTT on the TRV devices and settings if you haven't the key local_temperature_calibration in your HA instance and include_device_information in the Zigbee2MQTT MQTT settings**

## SETUP
You need to configure a "virtual" thermostat for every used thermostat.

Here is an example configuration.
```yaml
climate:
  - platform: ai_thermostat
    name: room
    thermostat: climate.tvr
    temperature_sensor: sensor.temperature
    window_sensors: group.office_windows
    weather: weather.xxx
    off_temperature: 20
    window_off_delay: 0
```


Key | Value | Required? | Description
--- | --- | --- | ---
***platform*** | `ai_thermostat` | *yes* |
***name*** | `Thermostat - Livingroom` | *no* | Used to name the virtual thermostat
***thermostat*** | `climate.tvr` | *yes* | a zigbee2mqtt climate entity.
***temperature_sensor*** | `sensor.temperature` | *yes* | a zigbee2mqtt sensor entity that is used for the actual temperature input of the thermostat.
***window_sensors*** | `group.livingroom_windows` | *yes* | a group of window/door - sensors (see below) that is used for the open window detection of the thermostat (the termostat dosn't need to support a open window detection for that feature).
***window_off_delay*** | `10` | *no* | Only set the thermostat to OFF state if the window/door - sensors are open for X seconds. Default ist 0 for instand turn off.
***weather*** | `weather.xxx` | *no* | a weather entity from Home Assistent to check the forcast to detect if heating is needed. (Meteorologisk institutt (Metno))
***off_temperature*** | `20` | *yes* | an int number as an temperature if the forcast outside temperature is above it the thermostat is turend off.

### Example Window/Door - Sensor config

```yaml
livingroom_windows:
  name: Livingroom Windows
  icon: mdi:window-open-variant
  all: false
  entities:
    - binary_sensor.openclose_1
    - binary_sensor.openclose_2
    - binary_sensor.openclose_3
```

<a href="https://www.buymeacoffee.com/kartoffeltoby" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-green.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>
