[![Active installations](https://badge.t-haber.de/badge/better_thermostat?kill_cache=1)](https://github.com/KartoffelToby/better_thermostat/)
[![GitHub issues](https://img.shields.io/github/issues/KartoffelToby/better_thermostat?style=for-the-badge)](https://github.com/KartoffelToby/better_thermostat/issues)
[![Version - 1.0.0-beta](https://img.shields.io/badge/Version-1.0.0beta-009688?style=for-the-badge)](https://github.com/KartoffelToby/better_thermostat/releases)
[![Discord](https://img.shields.io/discord/925725316540923914.svg?style=for-the-badge)](https://discord.gg/9BUegWTG3K)
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

# Better Thermostat

**Important Notice: Consider this software as unfinished as it has not reached version 1.0.**
**Don't select master on download, check `show beta version` install the latest one.**

### Requirements

- Minimum required Home Assistant version: `2022.8.0` 
  (_Latest tested version: `2022.9.4`_)

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

**We support all thermostats which are compatible with Home Assistant as long as they are shown up as a climate entity**

At this time following models are tested and recommended:

| Vendor     | Product Name         | Product Number          | HA Manufacturer | HA Model                                                    | Whitelabel                                                                                                      | Support local calibration |
|------------|----------------------|-------------------------|-----------------|-------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|---------------------------|
| Eurotronic | Spirit Zigbee        | SPZB0001                | `Eurotronic`    | `Spirit Zigbee wireless heater thermostat (SPZB0001)`       |                                                                                                                 |           YES             |
| Moes       | ZigBee3.0 Thermostat | SEA801&#8209;Zigbee     | `Saswell`       | `Thermostatic radiator valve (SEA801-Zigbee/SEA802-Zigbee)` | -&nbsp;HiHome&nbsp;WZB&#8209;TRVL<br>-&nbsp;Hama&nbsp;00176592<br>-&nbsp;RTX&nbsp;ZB&#8209;RT1                  |           YES             |
| Moes       | ZigBee3.0 Thermostat | SEA802&#8209;Zigbee     | `Saswell`       | `Thermostatic radiator valve (SEA801-Zigbee/SEA802-Zigbee)` | -&nbsp;HiHome&nbsp;WZB&#8209;TRVL<br>-&nbsp;Hama&nbsp;00176592<br>-&nbsp;RTX&nbsp;ZB&#8209;RT1                  |           YES             |
| TuYa       | TS0601               | TS0601_thermostat       | `TuYa`          | `Radiator valve with thermostat (TS0601_thermostat)`        | -&nbsp;Moes&nbsp;HY368<br>-&nbsp;Moes&nbsp;HY369RT<br>-&nbsp;SHOJZJ&nbsp;378RT<br>-&nbsp;Silvercrest&nbsp;TVR01 |           YES             |
| TuYa       | TV02-Zigbee          | TV02-Zigbee             | `TuYa`          | `Thermostat radiator valve (TV02-Zigbee)`                   | -&nbsp;Moes&nbsp;TV01-ZB<br>-&nbsp;Tesla&nbsp;Smart&nbsp;TSL-TRV-TV01ZG<br>-&nbsp;Unknown/id3.pl&nbsp;GTZ08     |           NO              |
| Siterwell  | Radiator Thermostat  | GS361A&#8209;H04        |                 |                                                             |                                                                                                                 |           NO              |
|            |                      |                         |                 |                                                             |                                                                                                                 |                           |

### Howto Setup

This custom component uses the config flow in Home Assistant.

After you installed this integration and restart HA, you can add it via the integrations page in Home Assistant.

**Click Settings > Devices & Services > Integrations > Add Integration > Better Thermostat**

**IMPORTANT: the weather and outdoor_sensor are not required, but you need one of them if you want to use this function, if not remove them**


Some nice to know config tipps for the configuration.yaml
#### Example Window/Door - Sensor config

```yaml
group:
  livingroom_windows:
    name: Livingroom Windows
    icon: mdi:window-open-variant
    all: false
    entities:
      - binary_sensor.openclose_1
      - binary_sensor.openclose_2
      - binary_sensor.openclose_3
```

#### Combine multiple TRV to one (Group)

Install the HACS [climate_group](https://github.com/daenny/climate_group) from @daenny

As each TRV has an individual local_temperature and must be individually calibrated, you need to create a better_thermostat entity for each TRV and then group them:

Example:

```yaml
climate:
  - platform: climate_group
    name: "TRV - Office"
    temperature_unit: C
    entities:
      - climate.bt_trv_office_1
      - climate.bt_trv_office_2
```

---

# ☕ Support

If you want to support this project, you can ☕ [**buy a coffee here**](https://www.buymeacoffee.com/kartoffeltoby).

<a href="https://www.buymeacoffee.com/kartoffeltoby"><img src="https://img.buymeacoffee.com/button-api/?text=Buy me a coffee&emoji=&slug=kartoffeltoby&button_colour=0ac982&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff"></a>

---

## 🧙 Maintainers

**[@KartoffelToby](https://github.com/KartoffelToby)**

**[@RubenKelevra](https://github.com/RubenKelevra)**

---

## ‎‍💻 Code Contributors

| User                                             |
|:-------------------------------------------------|
| [@RubenKelevra](https://github.com/RubenKelevra) |
| [@bruvv](https://github.com/bruvv)               |
| [@Cycor](https://github.com/Cycor)               |


