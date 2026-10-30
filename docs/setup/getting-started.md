---
title: 5-minute quickstart
sidebar:
  order: 1
description: Install Better Thermostat, place your room sensor properly, and set up your first room in five minutes.
---

Setting up Better Thermostat takes about five minutes per room. Following the steps below avoids 90 % of common setup mistakes.

## 1) Check your prerequisites & place your room sensor

Before creating a Better Thermostat room, make sure these entities already work inside Home Assistant (for example via Zigbee2MQTT, ZHA, Shelly, HomematicIP, Z-Wave JS, or tado°):

- **1 or more radiator thermostats (`climate.*`)** — required. Multiple radiators in the same room can be grouped into one Better Thermostat.
- **1 separate room temperature sensor (`sensor.*`)** — **required**. Setup cannot finish without an external room sensor, because the thermometer built into a radiator valve sits next to the hot pipe and reads several degrees too warm.
- **Window and/or door contact sensors (`binary_sensor.*`)** — *optional*, pauses heating automatically while airing the room. Have several windows in one room? Combine them into one helper first under **Settings → Devices & Services → Helpers → Create Helper → Group → Binary sensor group**.
- **Outdoor temperature sensor (`sensor.*`) or Weather entity (`weather.*`)** — *optional*, turns heating off automatically during warm spring/summer weather ([Summer mode](/deep-explanations/summer-mode/)).
- **Air conditioner / cooling device (`climate.*`)** — *optional*, lets one Better Thermostat coordinate both heating and cooling without the two fighting each other.

:::caution[Place your room sensor where you actually live—not near the radiator!]
Every algorithm is only as good as the temperature it reads:
- Place the sensor at **sitting or chest height (1.2–1.5 m)** on an interior wall or shelf.
- Keep it **away from radiator heat plumes, direct sunlight, drafty window frames, and warm electronics** (TVs, routers, PCs).
- Pick a sensor that reports changes of **`0.1–0.2 °C`** promptly (sensors that only send updates on `0.5–1.0 °C` jumps make smooth control much harder).
- Once a learning algorithm has adapted to the room, avoid moving the sensor to a new spot without resetting learned data ([Actions and conditions](/setup/actions-and-conditions/)).
:::

:::tip[Prepare your physical TRVs once before wrapping them]
Turn **off** any internal time schedules, internal open-window detection, or aggressive eco modes inside the TRV's own app or Zigbee2MQTT/ZHA device settings so the TRV does not fight Better Thermostat's commands. If you use **Zigbee2MQTT**, check that the TRV's `local_temperature_calibration` entity shows a number (e.g. `0`) rather than `null`/`unknown`.
:::

## 2) Install the integration

Better Thermostat 2.0 requires **Home Assistant `2026.9.0` or newer**. The easiest way to install Better Thermostat is through **HACS** (Home Assistant Community Store):

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=KartoffelToby&amp;repository=better_thermostat&amp;category=integration"><img alt="Open HACS repository" src="https://my.home-assistant.io/badges/hacs_repository.svg" /></a>

1. Open **HACS** in Home Assistant, search for **Better Thermostat**, and click **Download**.
2. *(Manual alternative: copy `custom_components/better_thermostat` from the [latest GitHub release](https://github.com/KartoffelToby/better_thermostat/releases/latest) into your Home Assistant `config/custom_components/` folder.)*
3. After downloading, **restart Home Assistant** so the integration loads cleanly.

## 3) Add your first room with safe Day-1 settings

Go to **Settings → Devices & Services → Integrations → Add Integration** and search for **Better Thermostat**, or click:

<a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=better_thermostat" target="_blank"><img src="https://my.home-assistant.io/badges/config_flow_start.svg" alt="Start Better Thermostat configuration flow"/></a>

Setup has **two screens** (see the full [Setup dialog walkthrough](/setup/configuration-walkthrough/) or try the [Interactive Configurator](/setup/configurator/)):

### Screen 1 — Room, sensors & comfort band

1. Pick your **real thermostat(s)** and your **room temperature sensor** (plus optional window, door, outdoor/weather, or cooling entities).
2. **Set `Tolerance` to `0.3` °C** *(important: the form starts at `0.0`, which reacts to every tiny `0.1 °C` dip and moves the valve motor more often; `0.3 °C` gives quiet, battery-friendly comfort for radiator rooms, or `0.5 °C` for underfloor heating)*.
3. If you added a **Window sensor**, set both open and close delays to **`120`–`180` seconds** (2–3 minutes) so briefly opening a balcony door does not cycle the valve, and cold air near the window can mix before heating resumes.
4. If you added an **Outdoor sensor** or **Weather entity**, set the outdoor shutdown threshold to **`16`–`17` °C** *(Better Thermostat smooths outdoor temperatures over 24 hours including cool nights, so the default `20 °C` keeps heating active even on warm `23 °C` spring days!)*.

### Screen 2 — How Better Thermostat drives each TRV

For each TRV in the room, you choose two things (explained with numerical examples in [Calibration types & algorithms](/calibration_algorithms/)):

- **`Calibration type`** *(the hardware communication channel)*:
  - Choose **`Direct Valve Based`** if offered for your TRV (e.g. Sonoff TRVZB, Shelly BLU TRV, Eurotronic Spirit)—highest precision.
  - Otherwise keep **`Offset Based`** (if offered), **unless** your radiator sits behind heavy curtains/furniture or your TRV firmware limits offsets to a tiny range (such as `±2.5 °C` or `±5 °C`), in which case choose **`Target Temperature Based`**.
- **`Calibration mode`** *(the control algorithm)*:
  - Keep **`(AI) Time Based (Default)`** (learns how fast your room heats over a few cycles) **or** select **`TPI Controller`** (steady proportional control with zero learning wait and minimal overshoot).
- Keep **`Overheating protection?`** enabled (**On**).
- Enable the **HomematicIP** toggle *only* if your TRV is a HomematicIP device (spaces non-urgent radio writes to 10 minutes to respect the 868 MHz 1 % airtime limit).

## 4) Put it on your dashboard

Better Thermostat creates a standard Home Assistant `climate.<room_name>` entity, so it works immediately with:

- Home Assistant's built-in **Thermostat card** and **Tile card**
- Voice assistants (**Assist**, **Google Home**, **Amazon Alexa**, **Apple HomeKit**)
- The optional companion **[Better Thermostat UI Card](https://github.com/KartoffelToby/better_thermostat_ui_card)** (available in HACS under *Frontend*), which displays window status, summer mode, ambient humidity, and heating/cooling state right on the dial.

:::note[Using Better Thermostat UI Card v3.0 or newer in YAML?]
Version 3.0 split the card into two separate custom elements. Use `type: custom:better-thermostat-normal-climate-card` (full dial) or `type: custom:better-thermostat-mini-climate-card` (compact row) instead of the legacy `custom:better-thermostat-ui-card` tag.
:::

## 5) Follow the Golden Rule & give it 24–48 hours

1. **Control only Better Thermostat from now on:** Hide the raw TRV climate cards from your main dashboard and point all schedules, automations, and voice assistants exclusively at `climate.<your_better_thermostat>`. (Turning the physical TRV knob by hand still syncs back automatically!)
2. **Add a schedule if you want automatic day/night temperatures:** Better Thermostat does not include a built-in clock schedule—import the official **[Weekly Heating Schedule blueprint](/setup/automation-blueprints/)** in one click (remember to enable `Comfort` and `Sleep` under **Enabled Presets** in Better Thermostat's options first).
3. **Give it 2–3 heating cycles:** Self-learning modes like `(AI) Time Based (Default)` need a day or two of normal heating and cooling to measure your room's thermal speed. Also remember that Better Thermostat spaces routine radio writes by **30 seconds** (or **10 minutes** on HomematicIP) to protect your TRV batteries—so a small delay before the valve moves is normal!
