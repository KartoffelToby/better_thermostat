---
title: Automation Blueprints
sidebar: 
    order: 4
description: Ready-made Home Assistant automation blueprints for Better Thermostat's purpose-specific device triggers.
---

# Automation Blueprints

Better Thermostat ships a collection of **ready-made automation blueprints** that take
advantage of the purpose-specific device triggers introduced in HA 2025.12.  
Each blueprint can be imported into Home Assistant with one click and customised through
the standard UI – no YAML editing required.

---

## Available blueprints

### 1 · Notify when heating starts

**File:** `blueprints/heating_active_notify.yaml`

Sends a push notification every time a Better Thermostat device switches its
`hvac_action` to `heating`. Helpful for tracking unexpected heating cycles or
monitoring energy consumption patterns.

| Input | Description | Default |
|---|---|---|
| Better Thermostat device | Device to monitor | – |
| Notification target | `notify.*` service to call | `notify.notify` |
| Message | Notification body (supports templates) | "🔥 … started heating." |

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Fblob%2Fmaster%2Fblueprints%2Fheating_active_notify.yaml)

---

### 2 · High humidity alert

**File:** `blueprints/humidity_high_alert.yaml`

Fires when the humidity reported by a Better Thermostat device stays above a
threshold (default **60 %**) for more than 2 minutes. Can optionally turn on a
ventilation switch and/or send a notification.

| Input | Description | Default |
|---|---|---|
| Better Thermostat device | Device to monitor | – |
| Humidity threshold | % above which the trigger fires | `60` |
| Notification target | Optional `notify.*` service | *(empty)* |
| Ventilation switch | Optional `switch.*` to turn on | *(empty)* |

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Fblob%2Fmaster%2Fblueprints%2Fhumidity_high_alert.yaml)

---

### 3 · Low TRV battery notification

**File:** `blueprints/battery_low_notify.yaml`

Sends a push notification when the minimum battery level of all TRVs associated
with a Better Thermostat device drops below the configured threshold (default
**20 %**). The alert is throttled to once every 24 hours to avoid spam.

| Input | Description | Default |
|---|---|---|
| Better Thermostat device | Device to monitor | – |
| Battery threshold | % below which the trigger fires | `20` |
| Notification target | `notify.*` service to call | `notify.notify` |

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Fblob%2Fmaster%2Fblueprints%2Fbattery_low_notify.yaml)

---

### 4 · Device error and target temperature alerts

**File:** `blueprints/device_error_notify.yaml`

A combined blueprint with two optional alerts:

- **Device error** – fires when Better Thermostat records at least one device
  error (e.g. a TRV goes unavailable or reports a fault code).
- **Target temperature reached** – fires after the room temperature has stayed
  at or above the setpoint for a configurable number of minutes.

| Input | Description | Default |
|---|---|---|
| Better Thermostat device | Device to monitor | – |
| Notification target | `notify.*` service to call | `notify.notify` |
| Alert on device errors | Enable/disable the error alert | `true` |
| Alert when target reached | Enable/disable the temp-reached alert | `false` |
| Delay before "target reached" alert | Minutes at setpoint before firing | `5` |

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Fblob%2Fmaster%2Fblueprints%2Fdevice_error_notify.yaml)

---

### 5 · Night mode (Sleep preset)

**File:** `blueprints/night_mode.yaml`

Activates the **Sleep preset** on one or more Better Thermostat devices while a
[Schedule helper](https://www.home-assistant.io/integrations/schedule/) is active,
and restores the normal (`none`) preset when the schedule ends.

The Sleep preset temperature is configured directly in the Better Thermostat
settings — no temperature input is needed here.

> **Prerequisite:** The Sleep preset must be enabled in Better Thermostat's
> configuration for each targeted thermostat.

| Input | Description |
|---|---|
| Schedule helper | A `schedule.*` entity that defines the night window |
| Thermostats | One or more Better Thermostat devices / climate entities |

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Fblob%2Fmaster%2Fblueprints%2Fnight_mode.yaml)

---

### 6 · Away preset when nobody is home

**File:** `blueprints/presence_away_preset.yaml`

Activates the **Away preset** on Better Thermostat devices when all tracked
persons or device trackers are away from home, and restores a configurable
preset (default: `none`) when someone arrives back.

A configurable departure delay prevents short absences (e.g. walking the dog)
from unnecessarily switching the heating. After the delay expires, presence is
re-checked before applying the Away preset.

| Input | Description | Default |
|---|---|---|
| Thermostats | Better Thermostat devices / climate entities to control | – |
| Presence entities | `person.*`, `device_tracker.*`, or `binary_sensor.*` entities | – |
| Delay before Away (min) | Wait this long after last person leaves | `10` |
| Preset when home | Preset to restore on arrival | `none` |
| Notification target | Optional `notify.*` service | *(empty)* |

> **Tip:** For multi-person households, create a group or use a `binary_sensor`
> that combines all person entities — the blueprint treats the list as an OR
> (anyone home = stay normal).

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Fblob%2Fmaster%2Fblueprints%2Fpresence_away_preset.yaml)

---

### 7 · Weekly Heating Schedule

**File:** `blueprints/weekly_heating_schedule.yaml`

The most powerful blueprint in the collection. Manages up to **4 independent time
slots** per day with fully separate preset assignments for **weekdays (Mon–Fri)**,
**Saturday** and **Sunday**.

#### Time slots

Each slot has a configurable start time and an independent preset per day type.
Slot 1 is the earliest in the day; slots must be ordered chronologically.

| Slot | Suggested name | Default time | Default weekday | Default Sat/Sun |
|---|---|---|---|---|
| Slot 1 | Wake up | 06:30 | `comfort` | `sleep` (lie-in) |
| Slot 2 | Daytime | 08:30 | `eco` (nobody home) | `comfort` |
| Slot 3 | Evening | 17:00 | `comfort` | `comfort` |
| Slot 4 | Night | 22:30 | `sleep` | `sleep` |

All 8 BT presets are available per slot per day type: `none`, `eco`, `away`,
`boost`, `comfort`, `home`, `sleep`, `activity`.

#### Additional features

| Feature | How it works |
|---|---|
| **Presence-based away mode** | Enable + select a `person.*` / `device_tracker.*` / `binary_sensor.*`. While nobody is home the *Vacation preset* is applied instead of the schedule. Returns to the correct slot automatically on arrival. |
| **Schedule pause switch** | Point to an `input_boolean` helper. Turning it on freezes the schedule; turning it off immediately re-applies the correct slot. |
| **HA restart recovery** | After a restart, waits 30 s for entities to load, then applies the currently correct slot (or vacation preset). |
| **Notifications** | Optional `notify.*` service receives a message on every slot change, presence event, and startup recovery. |

> **Tip:** Combine this blueprint with the *Away preset when nobody is home*
> blueprint by pointing both to the same presence entity — or simply use the
> built-in presence mode in this blueprint and skip the separate one.

| Input group | Inputs |
|---|---|
| Target | Thermostats (device / entity target) |
| Slot 1 | Start times & presets (Weekday / Saturday / Sunday) |
| Slot 2 | Start times & presets (Weekday / Saturday / Sunday) |
| Slot 3 | Start times & presets (Weekday / Saturday / Sunday) |
| Slot 4 | Start times & presets (Weekday / Saturday / Sunday) |
| Presence | Enable toggle · presence entity · vacation preset |
| Pause | Enable toggle · input_boolean helper |
| Notifications | notify.* target |

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Fblob%2Fmaster%2Fblueprints%2Fweekly_heating_schedule.yaml)

---

## How device triggers work

These blueprints use Better Thermostat's **purpose-specific device triggers**
(HA 2025.12+). Each trigger maps directly to an attribute or action of the
Better Thermostat climate entity:

| Trigger type | Fires when |
|---|---|
| `heating_active` | `hvac_action` becomes `heating` |
| `heating_stopped` | `hvac_action` leaves `heating` |
| `window_opened` | `window_open` attribute becomes `true` |
| `window_closed` | `window_open` attribute becomes `false` |
| `humidity_high` | `humidity` attribute exceeds configured threshold |
| `battery_low` | Minimum TRV battery drops below configured threshold |
| `device_error` | `errors` attribute contains at least one error |
| `target_temp_reached` | Current temperature ≥ target temperature |

You can also use these triggers directly in your own automations via the
**Automation editor → Add trigger → Device**. Select your Better Thermostat
device and choose the desired trigger type from the list.

---

## Writing your own blueprint

All blueprints follow the same trigger pattern:

```yaml
trigger:
  - platform: device
    domain: better_thermostat
    device_id: !input thermostat_device
    type: heating_active        # replace with any trigger type from the table above
```

Threshold-based triggers (`humidity_high`, `battery_low`) additionally accept
`above:` / `below:` fields and an optional `for:` duration, for example:

```yaml
trigger:
  - platform: device
    domain: better_thermostat
    device_id: !input thermostat_device
    type: humidity_high
    above: 65
    for:
      minutes: 5
```

See the [Home Assistant blueprint documentation](https://www.home-assistant.io/docs/blueprint/)
for full authoring guidance.
