[![Active installations - 12](https://badge.t-haber.de/badge/better_thermostat?kill_cache=1)](https://github.com/KartoffelToby/better_thermostat/)
# Better THERMOSTAT for Zigbee2MQTT

This integration brings some smartness to your TRV Zigbee2MQTT setup.

What does it? Basically, it combines an external temperature sensor, window/door Sensors, and a weather-Entity, so your TRV is calibrated with the temperature from the external sensor and turned off if some window is open. It also turns off the thermostat if a configured outside temperature is reached for two days in a row, so if it's outside warm enough you didn't need useless heating in your rooms.

It's also useful for those who only need an off and heating state and controlled the room temperature with a set target temperature based on an external temperature sensor.

So if you use the Google Assistant integration to control your thermostat you no longer get into an issue of incompatible modes or the problem that a target temp can't be set because the device is in "auto" mode that's remapped to eco.

Youst set your Target Heat point with your voice or the Google Home app and you are good to go.

At this time I tested it with two models: (but basically all zigbee2mqtt TRV should work.)

---

- Eurotronic Spirit Zigbee (SPZB0001) **normal calibration**
- Moes SEA801-Zigbee/SEA802-Zigbee **normal calibration**
- TuYa TS0601_thermostat (TS0601) **target temperature calibration**
- BRT-100-TRV **target temperature calibration** (will be switched to normal calibration if this is fixed [issue](https://github.com/Koenkk/zigbee2mqtt/issues/9486))

*All models that are not listed here uses the default wich is the **normal calibration** and expects that the TRV has system modes*

 **normal calibration**: means that the local_temperature_calibration setting in the TRV is used to sync the TRV internal current temperature with the connected room temperature sensor. The target temperature is settable over HA or directly on the TRV there are no restrictions

 **target temperature calibration**: means that the temperature sync is accomplished with a special target temperatur on the TRV, thats the reason why the target temperature displayed on the TRV is not the same as in HA, you only can ajust the target temperature via HA not the TRV itself. If you want more infos why, read #15

---

The SPZB0001 is Special, it uses the "heat" mode for boost and the auto mode for the "normal" operation, to avoid that it remaps heat with auto internally, the boost mode is lost with this configuration.

If you have a special Thermostat like the SPZB0001 feel free to open an issue or pull request.

The integration gets the Model identifier automatic, nothing to do here.

**IMPORTANT: be sure to enable "legacy" in Zigbee2MQTT on the TRV devices and settings if you haven't the key local_temperature_calibration in your HA instance and include_device_information in the Zigbee2MQTT MQTT settings**

## SETUP
You need to configure a "virtual" thermostat for every used thermostat.

Here is an example configuration.
```yaml
climate:
  - platform: better_thermostat
    name: room
    thermostat: climate.tvr
    temperature_sensor: sensor.temperature
    window_sensors: group.office_windows
    weather: weather.xxx #if this is set, the outdoor_sensor is ignored, remove the outdoor_sensor config!
    outdoor_sensor: sensor.outdoor_temperature #if you want to use it, remove the weather config!
    off_temperature: 20
    window_off_delay: 0
    valve_maintenance: false
```


Key | Example Value | Required? | Description
--- | --- | --- | ---
***platform*** | `better_thermostat` | *yes* |
***name*** | `Thermostat - Livingroom` | *no* | Used to name the virtual thermostat
***thermostat*** | `climate.tvr` | *yes* | a zigbee2mqtt climate entity.
***temperature_sensor*** | `sensor.temperature` | *yes* | a zigbee2mqtt sensor entity that is used for the actual temperature input of the thermostat.
***window_sensors*** | `group.livingroom_windows` | *yes* | a group of window/door - sensors (see below) that are used for the open window detection of the thermostat (the thermostat doesn't need to support an open window detection for that feature).
***window_off_delay*** | `10` | *no* | Only set the thermostat to an OFF state if the window/door - sensors are open for X seconds. Default is 0 for an instant turnoff.
***weather*** | `weather.xxx` | *no* | a weather entity from Home Assistant to check the forecast to detect if heating is needed in use of the off_temperature (Meteorologisk Institutt (Metno)) if this is set the outdoor_sensor will be ignored
***outdoor_sensor*** | `sensor.outdoor_temperature` | *no* | a zigbee2mqtt sensor entity that is used for the outdoor temperature calculation in use of the off_temperature for the avg of the last two days.
***off_temperature*** | `20` | *no* | an int number as a temperature if the forecast outside temperature is above it the thermostat is turned off.
***valve_maintenance*** | `false` | *no* | This is a maintenance function that will prevent the valve to get stuck or make annoying sounds, the default is `false`. If set to `true` it will perform a valve open-close-procedure every five days

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
