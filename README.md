[![Active installations](https://badge.t-haber.de/badge/better_thermostat?kill_cache=1)](https://github.com/KartoffelToby/better_thermostat/)
[![GitHub issues](https://img.shields.io/github/issues/KartoffelToby/better_thermostat?style=for-the-badge)](https://github.com/KartoffelToby/better_thermostat/issues)
[![Version - 1.0.0](https://img.shields.io/badge/Version-1.0.0-009688?style=for-the-badge)](https://github.com/KartoffelToby/better_thermostat/releases)
[![Discord](https://img.shields.io/discord/925725316540923914.svg?style=for-the-badge)](https://discord.gg/9BUegWTG3K)

# Better Thermostat

**Important Notice: Consider this software as unfinished as it has not reached version 1.0.**

### Requirements

- Minimum required Home Assistant version: `2021.12`
 (_Latest tested version: `2022.2.9`_)
- Zigbee2MQTT in case you use Zigbee Thermostats
 (_Latest tested version: `1.22.1-1`_)

### Companion UI

We've created a companion UI element which can display more information that the default thermostat element in Home Assistant. Check it out via HACS: [better-thermostat-ui-card](https://github.com/KartoffelToby/better-thermostat-ui-card)

- If you have a question or need help please create a new [discussion](https://github.com/KartoffelToby/better_thermostat/discussions) or check if your question is already answered.
- If you have a suggestion, found a bug, or want to add a new device or function create a new [issue](https://github.com/KartoffelToby/better_thermostat/issues)
- If you want to contribute to this project create a new [pull request](https://github.com/KartoffelToby/better_thermostat/pulls)

### Featureset

This integration brings some smartness to your connected radiator thermostats setup:

- Uses a temperature sensor far away from the radiators to measure the real room temperature
- Makes your TRVs fully compatible with Google Home
- Let your windows disable your heating (avoids programing this via automations)
- Your weather forcast provider will turn your heat on/off
- Or an outside air temperature sensor can do this as well
- Does some valve-maintenance automatically, to avoid that they will get stuck closed over summer
 
### Which hardware do we support?

At this time following models are tested and reported to work:

| Vendor     | Product Name         | Product Number          | HA Manufacturer | HA Model                                                    | Whitelabel                                                                                                      |
|------------|----------------------|-------------------------|-----------------|-------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| Eurotronic | Spirit Zigbee        | SPZB0001                | `Eurotronic`    | `Spirit Zigbee wireless heater thermostat (SPZB0001)`       |                                                                                                                 |
| Moes       | ZigBee3.0 Thermostat | SEA801&#8209;Zigbee     | `Saswell`       | `Thermostatic radiator valve (SEA801-Zigbee/SEA802-Zigbee)` | -&nbsp;HiHome&nbsp;WZB&#8209;TRVL<br>-&nbsp;Hama&nbsp;00176592<br>-&nbsp;RTX&nbsp;ZB&#8209;RT1                  | 
| Moes       | ZigBee3.0 Thermostat | SEA802&#8209;Zigbee     | `Saswell`       | `Thermostatic radiator valve (SEA801-Zigbee/SEA802-Zigbee)` | -&nbsp;HiHome&nbsp;WZB&#8209;TRVL<br>-&nbsp;Hama&nbsp;00176592<br>-&nbsp;RTX&nbsp;ZB&#8209;RT1                  | 
| TuYa       | TS0601               | TS0601_thermostat       | `TuYa`          | `Radiator valve with thermostat (TS0601_thermostat)`        | -&nbsp;Moes&nbsp;HY368<br>-&nbsp;Moes&nbsp;HY369RT<br>-&nbsp;SHOJZJ&nbsp;378RT<br>-&nbsp;Silvercrest&nbsp;TVR01 |
| Siterwell  | Radiator Thermostat  | GS361A&#8209;H04        |                 |                                                             |                                                                                                                 | 
| BECA       | BRT-100 ZB           | BRT&#8209;100&#8209;TRV |                 |                                                             |                                                                                                                 |


Is your hardware not listed? Shoot us a [ticket](https://github.com/KartoffelToby/better_thermostat/issues)!

### Howto Setup

This custom component requires a manual edit of the configuration.yaml of Home Assistant.

After you opened the configuration file, you'll create one virtual Better Thermostat entity for each room you like us to control. This will create a secondary climate entity which controls the original climate entity of your thermostat.

Here is a minimal configuration example

```yaml
climate:
  - platform: better_thermostat
    name: room
    thermostat: climate.trv
    temperature_sensor: sensor.temperature
    window_sensors: group.office_windows
```

Here is a full configuration example

```yaml
climate:
  - platform: better_thermostat
    name: room
    thermostat: climate.trv
    temperature_sensor: sensor.temperature
    window_sensors: group.office_windows # if this is not set, the window open detection is off
    weather: weather.home # if this is set, the outdoor_sensor is ignored, remove the outdoor_sensor config!
    outdoor_sensor: sensor.outdoor_temperature # if you want to use it, remove the weather entity from the config!
    off_temperature: 17.5
    window_off_delay: 15 # in seconds
    valve_maintenance: false
    night_temp: 18.5
    night_start: '22:00'
    night_end: '06:00'
```


**IMPORTANT: the weather and outdoor_sensor are not required, but you need one of them if you want to use this function, if not remove them**

| Key                      | Example Value                | Required? | Description                                                                                                                                                                                                                                                            |
|--------------------------|------------------------------|-----------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| ***platform***           | `better_thermostat`          | *yes*     |                                                                                                                                                                                                                                                                        |
| ***name***               | `Thermostat - Livingroom`    | *no*      | Used to name the virtual thermostat                                                                                                                                                                                                                                    |
| ***thermostat***         | `climate.trv`                | *yes*     | a climate entity.                                                                                                                                                                                                                                                      |
| ***unique_id***          | `392049`                     | *no*      | A unique_id (e.g. UNIX timestamp) mostly needed when using google home.                                                                                                                                                                                                |
| ***temperature_sensor*** | `sensor.temperature`         | *yes*     | a sensor entity that is used for the actual temperature input of the thermostat.                                                                                                                                                                                       |
| ***window_sensors***     | `group.livingroom_windows`   | *no*      | a group of window/door - sensors (see below) that are used for the open window detection of the thermostat (the thermostat doesn't need to support an open window detection for that feature). If you have only one window, you can pass the entity without the group. |
| ***window_off_delay***   | `15`                         | *no*      | Only set the thermostat to an OFF state if the window/door - sensors are open for X seconds. Default is 0 for an instant turnoff.                                                                                                                                      |
| ***weather***            | `weather.home`               | *no*      | a weather entity (e.g. by the  Meteorologisk Institutt - Metno integration) within Home Assistant to check the forecast to detect if heating is needed. The threshold is set by the off_temperature. This setting overwrites the outdoor_sensor.                       |
| ***outdoor_sensor***     | `sensor.outdoor_temperature` | *no*      | A temperature sensor entity within Home Assistant that is used to determine if the heating should be switched off. The threshold is set by the off_temperature. If a weather entity is configured this setting is ignored.                                             |
| ***off_temperature***    | `17.5`                       | *no*      | An integer as a temperature cutoff in case the weather is warm. This setting requires either a weather or an outdoor_sensor setting to work.                                                                                                                           |
| ***valve_maintenance***  | `false`                      | *no*      | This is a maintenance function that will prevent the valve to get stuck or make annoying sounds, the default is `false`. If set to `true` it will perform a valve open-close-procedure every five days                                                                 |
| ***night_temp***         | `18.5`                       | *no*      | if this value is set, the night temperature reduction is active and set it to the temperature at night (to disable it, remove this setting or set it to -1) see also  night_start and night_end                                                                        |
| ***night_start***        | `23:00`                      | *no*      | define the start time of the night for the night reduction (night_temp must be set) the TRV will be set to the night temp                                                                                                                                              |
| ***night_end***          | `07:00`                      | *no*      | define the end time of the night for the night reduction (night_temp must be set) the TRV will be set back to the last active temp                                                                                                                                     |

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

### Combine multiple TRV to one

Install the HACS [climate_group](https://github.com/daenny/climate_group) from @daenny

As each TRV has an individual local_temperature and must be individually calibrated, you need to create a better_thermostat entity for each TRV and then group them:

Example:

```yaml
climate:
  - platform: better_thermostat
    name: Ai - TRV - Office - 1
    thermostat: climate.real_trv_office_1
    temperature_sensor: sensor.temperatur_office_temperature
    window_sensors: group.office_windows
    weather: weather.home
    off_temperature: 19.5
    unique_id: 1
  - platform: better_thermostat
    name: Ai - TRV - Office - 2
    thermostat: climate.real_trv_office_2
    temperature_sensor: sensor.temperatur_office_temperature
    window_sensors: group.office_windows
    weather: weather.home
    off_temperature: 19.5
    unique_id: 2
  - platform: climate_group
    name: "TRV - Office"
    temperature_unit: C
    entities:
      - climate.ai_trv_office_1
      - climate.ai_trv_office_2
```

### Zigbee2Mqtt config requirements

**IMPORTANT: If you use Zigbee2MQTT to connect to your TRV devices make sure to enable the include_device_information in the Zigbee2MQTT MQTT settings**

If you use Z2M with the HA Supervisor, make sure you set it in the configuration. otherwise, it reset this option on every restart. [#57](/../../issues/57)

```yaml
mqtt:
  base_topic: zigbee2mqtt
  include_device_information: true
```

Switch on the global **include_device_information** under Settings > Mqtt > include_device_information.
<br>
<img src="assets/z2m_include_device_informations.png" width="900px">

---

## ☕ Supporters

If you want to support this project, you can ☕ [**buy a coffee here**](https://www.buymeacoffee.com/kartoffeltoby).

| User    | Donation |
|:--------|:---------|
| Someone | ☕ x 3    |
| Someone | ☕ x 3    |
| Someone | ☕ x 1    |

---

# Maintainers

- [@KartoffelToby](https://github.com/KartoffelToby)
- [@RubenKelevra](https://github.com/RubenKelevra)

## ‎‍💻 Code Contributors

| User                                             |
|:-------------------------------------------------|
| [@RubenKelevra](https://github.com/RubenKelevra) |
| [@bruvv](https://github.com/bruvv)               |
| [@Cycor](https://github.com/Cycor)               |

<a href="https://www.buymeacoffee.com/kartoffeltoby" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-green.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>
