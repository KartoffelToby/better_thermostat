# Better Thermostat

[![Active installations](https://badge.t-haber.de/badge/better_thermostat?kill_cache=1)](https://github.com/KartoffelToby/better_thermostat/)
[![GitHub issues](https://img.shields.io/github/issues/KartoffelToby/better_thermostat?style=for-the-badge)](https://github.com/KartoffelToby/better_thermostat/issues)
[![Version](https://img.shields.io/github/v/release/KartoffelToby/better_thermostat?style=for-the-badge&label=Version&color=009688)](https://github.com/KartoffelToby/better_thermostat/releases)
[![Discord](https://img.shields.io/discord/925725316540923914.svg?style=for-the-badge)](https://discord.gg/9BUegWTG3K)
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

**Full documentation & Interactive Configurator: <https://better-thermostat.org/>**

## What is Better Thermostat?

Every smart radiator thermostat (TRV) measures temperature millimeters away from its own hot water pipe, causing it to read several degrees too warm and shut off while your room is still cold.

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

- **True room-center regulation:** Drives TRVs via **Direct Valve (`0–100 %`)**, **Local Offset**, or **Target Temperature** calibration, plus automatic external room-temperature mirroring on supported models (such as Sonoff `TRVZB` & `TRV-ZBT`).
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

| Algorithm | Best For | Trade-off |
|---|---|---|
| **`(AI) Time Based (Default)`** | Everyday rooms — learns your room's actual warming speed locally over 2–3 cycles | Needs a few cycles to adapt after setup |
| **`TPI Controller`** | Zero-learning comfort, bedrooms, well-insulated rooms, and multi-radiator rooms (holds a smooth proportional opening, no noisy on/off clicking) | Slightly gentler initial heat-up than PID/Aggressive |
| **`(AI) MPC v2`** *(experimental)* | **Direct-valve TRVs** (Sonoff `TRVZB`, Shelly BLU TRV, Eurotronic Spirit) — Kalman observer + QP predictive braking | Experimental; designed for direct-valve hardware |
| **`PID Controller`** *(Beta)* | Kitchens, sunny rooms, and hallways with sudden disturbances (`Direct Valve` + `0.1 °C` room sensor recommended) | Higher valve motor activity; Auto-Tune is in beta |
| **`Aggressive`** | Drafty, poorly insulated rooms that warm up too slowly (`+2.5 °C` boost while heating) | Higher overshoot risk; non-direct-valve only |
| **`External Sensor Offset Only`** | Smart TRVs (e.g. tado°) whose internal controller you want to keep—pure 1:1 sensor bias fix | No tolerance band or extra controller modulation |
| **`MPC Predictive`** *(Beta)* | First-generation 30-min predictive model | Higher valve wear; prefer **TPI** or **MPC v2** |
| **`No Calibration`** | Using Better Thermostat only for window/door pause, Summer Mode, presets, or multi-TRV grouping | Passes target temperature unchanged |

Not sure which combination fits your room? Try the **[Interactive Configurator](https://better-thermostat.org/setup/configurator/)**.

---

## Supported Hardware & Integrations

Any thermostat exposed as a Home Assistant `climate` entity with a writable target temperature works out of the box. Tested integrations include:

- **Zigbee2MQTT** *(recommended for Zigbee TRVs — unlocks local offsets and direct valve position)*
- **ZHA (Zigbee Home Automation)** & **deCONZ**
- **Shelly** *(including dedicated direct-valve support for **Shelly BLU TRV** via BLU Gateway)*
- **Homematic(IP) Local / OpenCCU** *(supports `select` temperature offsets + 10-minute radio duty-cycle protection)*
- **Z-Wave JS** *(including Eurotronic Spirit Z / Aeotec `ZWA021` direct-valve mode)*
- **tado°** & **`generic_thermostat`**

Built-in quirk fixes are included for 15+ popular models (`TRVZB`, `TRV-ZBT`, `SPZB0001`, `ZWA021`, `COZB0001`, `SHTRV-01`, `SEA801`/`802`, `BTH-RM`, `ME167`, `TS0601`, `TV02-Zigbee`, `BHT-002-GCLZB`, TermoWeb `Heater`, and more)—see [Supported devices & compatibility](https://better-thermostat.org/working-devices/compatibility/).

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
