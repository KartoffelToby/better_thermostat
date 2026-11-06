# Better Thermostat

[![Active installations](https://badge.t-haber.de/badge/better_thermostat?kill_cache=1)](https://github.com/KartoffelToby/better_thermostat/)
[![GitHub issues](https://img.shields.io/github/issues/KartoffelToby/better_thermostat?style=for-the-badge)](https://github.com/KartoffelToby/better_thermostat/issues)
[![Version](https://img.shields.io/github/v/release/KartoffelToby/better_thermostat?style=for-the-badge&label=Version&color=009688)](https://github.com/KartoffelToby/better_thermostat/releases)
[![Discord](https://img.shields.io/discord/925725316540923914.svg?style=for-the-badge)](https://discord.gg/9BUegWTG3K)
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

**Full documentation & Interactive Configurator: <https://better-thermostat.org/>**

## What is Better Thermostat?

1. **Fixes the physical radiator sensor flaw:** Every smart radiator thermostat (TRV) measures temperature millimeters away from its own `55–70 °C` hot water pipe, causing it to read several degrees too warm and shut off while your room is still cold.
2. **Replaces expensive proprietary ecosystems & subscriptions (Bosch, tado°, etc.):** Commercial ecosystems lock you into `50–80 €` brand-specific room sensors, proprietary bridges, and paid subscriptions for basic features like open-window shutoff. **Better Thermostat is 100 % free, local (as far as your devices and integrations run locally—tado° setups rely on tado°'s internet API), and subscription-free**—letting you combine **any TRV brand** with **any room temperature, window, door, or outdoor sensor** in Home Assistant with the same or an even more advanced feature set.

**Better Thermostat** wraps one or more radiator valves (plus an optional air-conditioning cooler) and a **separate room temperature sensor** into **one smart virtual `climate` entity** in Home Assistant. It regulates your room accurately through whichever hardware channel each TRV supports (**Direct Valve `0–100 %`**, **Local Offset**, or **Target Setpoint**), pauses heating for open windows and doors, shuts off automatically during warm weather, and protects TRV batteries by spacing radio writes.

## Requirements

- Minimum required Home Assistant version: `2026.9.0`
  (_continuously tested against Home Assistant_ [![Tested Home Assistant version](https://img.shields.io/badge/dynamic/regex?url=https%3A%2F%2Fraw.githubusercontent.com%2FKartoffelToby%2Fbetter_thermostat%2Fdevelop%2Fpyproject.toml&search=homeassistant%3E%3D%28%5B0-9.%5D%2B%29&replace=%241&label=&color=009688)](pyproject.toml))
- At least **1 radiator/wall thermostat (`climate.*`)** and **1 separate room temperature sensor (`sensor.*`)** per room.

### Companion UI Card

Display open-window status, Summer Mode, ambient humidity, and heating/cooling state with our companion frontend card via HACS: [better-thermostat-ui-card](https://github.com/KartoffelToby/better-thermostat-ui-card) *(note: in UI Card v3.0+, use `custom:better-thermostat-normal-climate-card` or `custom:better-thermostat-mini-climate-card` in YAML)*.

- Questions or setup help: [GitHub Discussions](https://github.com/KartoffelToby/better_thermostat/discussions)
- Bug reports or feature/quirk requests: [GitHub Issues](https://github.com/KartoffelToby/better_thermostat/issues)
- Contributing & developer internals: [CONTRIBUTING.md](CONTRIBUTING.md) and [Internals documentation](https://better-thermostat.org/internals/architecture/)

---

## Key Features (v2.0)

- **True room-center regulation:** Drives TRVs via **Direct Valve (`0–100 %`)**, **Local Offset**, or **Target Temperature** calibration, plus automatic external room-temperature mirroring on supported models (such as Sonoff `TRVZB` & `TRV-ZBT`, Aqara `SRTS-A01`, Danfoss Ally `014G2461`, and `Shelly BLU TRV`).
- **Multi-TRV grouping & optional AC cooling:** Synchronizes multiple radiators in one room (even mixed brands) and optionally coordinates an air conditioner without heating/cooling fights.
- **Window & separate Door pause:** Independent open/close debounce delays; offline contact sensors count as closed so dead coin-cell batteries never freeze a room.
- **24-hour damped Summer Mode:** Smooths outdoor sensors or weather forecasts over 24 hours with a `1.0 °C` (`1.8 °F`) hysteresis band so sunny sensor spikes or cool spring mornings don't flip heating erratically.
- **Selectable Presets with live sliders:** Enable `Eco`, `Comfort`, `Sleep`, `Away`, `Home`, `Activity`, or `Boost`—each backed by persistent `number` entities (`Min`/`Max` pairs when a cooler is configured).
- **Pause Learning & Device Release switches:** Freeze model adaptation while a fireplace burns or a boiler heats tap water, or hand TRVs back to manual control with one toggle.
- **9 ready-made Automation Blueprints:** Including a 4-slot [Weekly Heating Schedule](https://better-thermostat.org/setup/automation-blueprints/), Night Mode, Presence Away, Pause Learning, and battery/humidity alerts.
- **Fail-soft & observable:** Steps down gracefully (`optimal` → `sensor_fallback` → `hold`) if sensors drop offline, spaces radio writes (30 s default / 10 min HomematicIP) to protect batteries, and includes a 50-cycle deterministic flight recorder in Home Assistant Diagnostics.

---

## Control Algorithms (`Calibration mode`)

Better Thermostat offers **8 control algorithms**—see [Calibration types & algorithms](https://better-thermostat.org/calibration_algorithms/) for concrete numerical examples:

- **AI Time Based**: Uses a custom algorithm based on simple measurements and calculations (not actual AI) to calculate the required heating power and adjusts the TRV calibration to achieve it. This improves upon the standard TRV internal algorithm. It is the default.
- **TPI (Time Proportional Integral)**: Turns the distance from the target into a duty cycle and holds the valve at that opening, which keeps the temperature stable and reduces overshoot.
- **MPC v2** (experimental): A predictive controller with a Kalman observer and a quadratic-program optimiser. It writes the valve directly with Direct Valve Based and otherwise works through the target temperature or the offset.
- **PID Controller**: A classic Proportional-Integral-Derivative controller that learns your room's characteristics to maintain a stable temperature. It features auto-tuning (currently in beta) to automatically find the best parameters (Kp, Ki, Kd) for your room.
- **Aggressive**: Pushes the TRV harder while heating for a faster warm-up, at the price of more overshoot.
- **External Sensor Offset Only**: Corrects the TRV's internal sensor with your room sensor and nothing more.
- **MPC (Model Predictive Control)**: Uses a physical model of your room and radiator to predict future temperature changes and optimize valve opening.
- **No Calibration**: Passes your target temperature to the TRV unchanged.

Start with **AI Time Based** (the default) or **TPI**. Try **PID** for tighter tracking in a stable room, or **MPC** for disturbance anticipation. The [algorithm documentation](https://better-thermostat.org/calibration_algorithms/) describes every mode, or use the **[Interactive Configurator](https://better-thermostat.org/setup/configurator/)** to get a tailored room setup.

---

## Supported Hardware & Integrations

Any thermostat exposed as a Home Assistant `climate` entity with a writable target temperature works out of the box. Tested integrations include:

- **Zigbee2MQTT** *(recommended for Zigbee TRVs — unlocks local offsets and direct valve position)*
- **ZHA (Zigbee Home Automation)** & **deCONZ**
- **Shelly** *(including dedicated direct-valve support for **Shelly BLU TRV** via BLU Gateway)*
- **Homematic(IP) Local / OpenCCU** *(supports `select` temperature offsets + 10-minute radio duty-cycle protection)*
- **Z-Wave JS** *(including Eurotronic Spirit Z / Aeotec `ZWA021` direct-valve mode and parameter 8 local offsets)*
- **tado°** & **`generic_thermostat`**

Built-in quirk fixes are included for 15+ popular models (`TRVZB`, `TRV-ZBT`, `SRTS-A01`, `014G2461`, `SPZB0001`, `ZWA021`, `COZB0001`, `SHTRV-01`, `SEA801`/`802`, `BTH-RM`, `ME167`, `TS0601`, `TV02-Zigbee`, `BHT-002-GCLZB`, TermoWeb `Heater`, and more)—see [Supported devices & compatibility](https://better-thermostat.org/working-devices/compatibility/).

---

## Quick Setup

1. Install **Better Thermostat** via **HACS** (or copy `custom_components/better_thermostat` from the [latest release](https://github.com/KartoffelToby/better_thermostat/releases/latest)) and **restart Home Assistant**.
2. If a room has multiple window or door contacts, combine them first under **Settings → Devices & Services → Helpers → + Create Helper → Group → Binary sensor group**.
3. Go to **Settings → Devices & Services → + Add Integration → Better Thermostat** and follow the [5-minute quickstart](https://better-thermostat.org/setup/getting-started/).
4. Upgrading from version 1.9? Read [Upgrading from 1.9 to 2.0](https://better-thermostat.org/setup/upgrading-to-2-0/).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## ☕ Support

If you want to support this project, you can ☕ [**buy a coffee here**](https://www.buymeacoffee.com/kartoffeltoby).

[![Buy me a coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=&slug=kartoffeltoby&button_colour=0ac982&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff)](https://www.buymeacoffee.com/kartoffeltoby)
