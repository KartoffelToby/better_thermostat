# AI THERMOSTAT for Zigbee2MQTT

This Integration brings some Smartness in your TRV Zigbee2MQTT setup.

What does it? Basically, it combines a external temperature sensor, window/door Sensors and a weather-Entity, so your TRV is calibrateted with the temperature from the external sensor and turned of if some window is open. It also trun off the termostat if a configured outside tempature is reached for two days in a row, so if its outside warm enoth you didn't need usless heating your rooms.

Its also usefull for those they only need a off and heating state and controlled the room themperature with a settet target temperature based on external temperature sensor.

So if you use the Google Assistant Intigration to controll your Thermostat you not longer get into an issue of incombatible modes. or the problem that a target temp can't be set because the devices is in "auto" mode thats remapped to eco

Youst set your Target Heat point with your voice or the Google Home app and your good to go.

At this time i testet it with two models: (but basaclly all zigbee2mqtt trv should work.)

- Eurotronic Spirit Zigbee (SPZB0001)
- Moes SEA801-Zigbee/SEA802-Zigbee

The SPZB0001 is Special, it uses the "heat" mode for boost and the auto mode for the "normal" operation, to avoid that it remaps heat with auto internaly, the boost mode is lost with this configuration.

If you have a special Thermostat like the SPZB0001 feel free to open an issue.

The Intigration gets the Model identfyer automatic, nothing to do here.

**IMPORTANT: be sure to enable "legacy" in Zigbee2MQTT on the TRV devices and settings if you havent the key local_temperature_calibration in your HA instance**

## SETUP
You need to configure a "virtual" thermostat for every used thermostat.

Here is a example configuration.
```yaml
climate:
  - platform: ai_thermostat
    name: room
    thermostat: climate.tvr
    temperature_sensor: sensor.temperature
    window_sensors: group.office_windows
    weather: weather.xxx
    off_temperature: 20
```


Key | Value | Required? | Description
--- | --- | --- | ---
***platform*** | `ai_thermostat` | *yes* |
***name*** | `Thermostat - Livingroom` | *no* | Used to name the virtual thermostat
***thermostat*** | `climate.tvr` | *yes* | a zigbee2mqtt climate entity.
***temperature_sensor*** | `sensor.temperature` | *yes* | a zigbee2mqtt sensor entity that is used for the actual temperature input of the thermostat.
***window_sensors*** | `group.livingroom_windows` | *yes* | a group of window/door - sensors (see below) that is used for the open window detection of the thermostat (the termostat dosn't need to support a open window detection for that feature).
***weather*** | `weather.xxx` | *yes* | a weather entity from Home Assistent to check the forcast to detect if heating is needed. (Meteorologisk institutt (Metno))
***off_temperature*** | `20` | *yes* | a int number as an temperature if the forcast outside temperature is above it the thermostat is turend off.

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
