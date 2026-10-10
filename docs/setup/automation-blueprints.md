---
title: Automation Blueprints
sidebar: 
    order: 4
description: Ready-made Home Assistant automation blueprints for Better Thermostat's purpose-specific device triggers.
---

# Automation Blueprints

Better Thermostat ships a collection of **ready-made automation blueprints**. Most of
them use Better Thermostat's purpose-specific device triggers; the night mode, presence
and weekly schedule blueprints react to helpers, presence entities and the clock instead.  
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
ventilation switch and/or send a notification. Both actions are off until you
enable them.

| Input | Description | Default |
|---|---|---|
| Better Thermostat device | Device to monitor | – |
| Humidity threshold | % above which the trigger fires | `60` |
| Notification target | `notify.*` service to call | `notify.notify` |
| Enable notifications | Send a notification when humidity is high | `false` |
| Ventilation switch | `switch.*` to turn on; replace the placeholder with a real switch | `switch.bt_ventilation_placeholder` |
| Enable ventilation switch control | Turn on the ventilation switch when humidity is high | `false` |

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Fblob%2Fmaster%2Fblueprints%2Fhumidity_high_alert.yaml)

---

### 3 · Low TRV battery notification

**File:** `blueprints/battery_low_notify.yaml`

Sends a push notification when the minimum battery level of all TRVs associated
with a Better Thermostat device drops below the configured threshold (default
**20 %**). The alert is throttled to once every 24 hours per automation to
avoid spam: a report within 24 hours of the previous notification is ignored.

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
  error, which happens when one of its TRVs becomes unavailable.
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
from unnecessarily switching the heating. If someone returns during the delay,
the pending switch to Away is cancelled and the home preset is restored. After
the delay expires, presence is re-checked once more, and the Away preset is
only applied if still nobody is home. Attribute-only updates of the presence
entities (e.g. a new GPS position) do not restart the delay.

> **Prerequisite:** The Away preset, and the preset restored on arrival if it
> is not `none`, must be enabled in Better Thermostat's configuration for each
> targeted thermostat (see [Presets](/setup/presets/#choosing-which-presets-appear)).
> A new configuration enables Eco only, so enable Away first; otherwise Home
> Assistant rejects the preset and the automation stops with
> `Preset mode away is not valid`.

| Input | Description | Default |
|---|---|---|
| Thermostats | Better Thermostat devices / climate entities to control | – |
| Presence entities | `person.*`, `device_tracker.*`, or `binary_sensor.*` entities | – |
| Delay before Away (min) | Wait this long after last person leaves | `10` |
| Preset when home | Preset to restore on arrival | `none` |
| Notification target | `notify.*` service to call | `notify.notify` |
| Enable notifications | Send a notification on departure and arrival | `false` |

> **Tip:** For multi-person households, select all person entities: the
> blueprint treats the list as an OR (anyone home = stay normal).

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

| Slot | Suggested name | Default time Mon–Fri | Default time Sat | Default time Sun | Default preset weekday | Default preset Sat/Sun |
|---|---|---|---|---|---|---|
| Slot 1 | Wake up | 06:30 | 08:00 | 08:00 | `comfort` | `sleep` (lie-in) |
| Slot 2 | Daytime | 08:30 | 10:00 | 10:00 | `eco` (nobody home) | `comfort` |
| Slot 3 | Evening | 17:00 | 17:00 | 17:00 | `comfort` | `comfort` |
| Slot 4 | Night | 22:30 | 23:30 | 22:30 | `sleep` | `sleep` |

All 8 BT presets are available per slot per day type: `none`, `eco`, `away`,
`boost`, `comfort`, `home`, `sleep`, `activity`.

> **Prerequisite:** Every preset you choose, the vacation preset included,
> must be enabled in Better Thermostat's configuration for each targeted
> thermostat (see [Presets](/setup/presets/#choosing-which-presets-appear)).
> A new configuration enables Eco only, while the default slots use
> `comfort`, `sleep` and `eco` and the vacation preset is `away`. Enable those
> presets, or pick enabled ones for every slot; otherwise Home Assistant
> rejects the preset at the first slot change and the schedule changes
> nothing.

#### Additional features

| Feature | How it works |
|---|---|
| **Presence-based away mode** | Enable + select one or more `person.*` / `device_tracker.*` / `binary_sensor.*` entities (leave empty to disable). While none of them is home the *Vacation preset* is applied instead of the schedule. Returns to the correct slot automatically on arrival. |
| **Schedule pause switch** | Point to one or more `input_boolean` helpers (leave empty to disable). The schedule is frozen while at least one of them is on; turning them off immediately re-applies the correct slot. |
| **HA restart recovery** | After a restart, waits 30 s for entities to load, then applies the currently correct slot (or vacation preset). |
| **Notifications** | Once *Enable notifications* is on, the `notify.*` service (default `notify.notify`) receives a message on every slot change, presence event, and startup recovery. |

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
| Presence | Enable toggle · presence entities · vacation preset |
| Pause | Enable toggle · input_boolean helpers |
| Notifications | Enable toggle · notify.* target |

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Fblob%2Fmaster%2Fblueprints%2Fweekly_heating_schedule.yaml)

---

## How device triggers work

The trigger-based blueprints use Better Thermostat's **purpose-specific device triggers**.
Each trigger maps directly to an attribute or action of the
Better Thermostat climate entity:

| Trigger type | Fires when |
|---|---|
| `heating_active` | `hvac_action` changes from another action to `heating` |
| `heating_stopped` | `hvac_action` changes from `heating` to another action |
| `window_opened` | `window_open` attribute changes from `false` to `true` |
| `window_closed` | `window_open` attribute changes from `true` to `false` |
| `humidity_high` | `current_humidity` attribute rises above the threshold |
| `battery_low` | The lowest battery level of the TRVs drops below the threshold |
| `device_error` | `errors` attribute goes from no error to at least one |
| `target_temp_reached` | Current temperature becomes equal to or higher than the target temperature, because the room warmed up or the target was lowered; with a cooler, the lower end of the target range counts as the target |

Three more triggers are available for your own automations:

| Trigger type | Fires when |
|---|---|
| `hvac_mode_changed` | The HVAC mode changes from another mode to the one set in `to` |
| `current_temperature_changed` | The current temperature changes, or crosses the `above` or `below` value when one is set |
| `current_humidity_changed` | The current humidity changes, or crosses the `above` or `below` value when one is set |

Without `above` or `below`, `current_temperature_changed` and
`current_humidity_changed` fire on every change of the value. A value that goes
missing or comes back counts as no change, so a sensor that drops out fires
nothing, and neither does the thermostat coming back from `unavailable`.

With a threshold they fire when the value crosses it, not on every change:
`current_temperature_changed` with `above: 22` fires when the room goes
from 22 °C or less to more than 22 °C, and fires again only after the
temperature has dropped back to 22 °C or below and risen once more. The same
holds for `humidity_high`, `battery_low`, `device_error` and
`target_temp_reached`. While the thermostat is `unavailable` these triggers
reset, so they fire when it comes back with the value past the threshold. The
triggers that watch a change of state, `heating_active`, `heating_stopped`,
`window_opened`, `window_closed` and `hvac_mode_changed`, do not fire when the
thermostat comes back from `unavailable`.

`humidity_high` and `current_humidity_changed` appear only for a thermostat
configured with a humidity sensor.

You can also use these triggers directly in your own automations via the
**Automation editor → Add trigger → Device**. Select your Better Thermostat
device and choose the desired trigger type from the list.

### Trigger parameters

Every trigger takes these keys:

| Key | Required | Value |
|---|---|---|
| `trigger` | yes | `device` |
| `domain` | yes | `better_thermostat` |
| `device_id` | yes | The Better Thermostat device |
| `type` | yes | One of the trigger types above |
| `entity_id` | no | The Better Thermostat climate entity. Without it, the trigger watches the device's climate entity. |

On top of these, each type takes its own fields:

| Trigger type | Field | Required | Value | Default |
|---|---|---|---|---|
| `heating_active`, `heating_stopped`, `window_opened`, `window_closed`, `device_error`, `target_temp_reached` | `for` | no | Duration | none |
| `humidity_high` | `above` | no | Humidity in % | `60` |
| | `for` | no | Duration | none |
| `battery_low` | `below` | no | Battery level in % | `20` |
| | `for` | no | Duration | none |
| `hvac_mode_changed` | `to` | yes | One HVAC mode of the thermostat: `heat` or `off`, with a cooling device `heat_cool` or `off` | none |
| | `for` | no | Duration | none |
| `current_temperature_changed` | `above`, `below` | no | Temperature in Home Assistant's unit | none |
| | `for` | no | Duration | none |
| `current_humidity_changed` | `above`, `below` | no | Humidity in % | none |
| | `for` | no | Duration | none |

`for` makes the trigger fire only once the new state has held that long. It is a
mapping of `days`, `hours`, `minutes`, `seconds` or `milliseconds`, such as
`for: {minutes: 5}`. Home Assistant rejects an automation that gives `for` as a
string like `"00:05:00"`, or that sets a field the trigger does not have, such
as `from` on `hvac_mode_changed`.

A trigger that lacks its required field does not fire. `hvac_mode_changed`
without `to` logs the error "names no mode to watch for" and leaves the
automation's other triggers working.

A complete automation with a classic trigger:

```yaml
alias: Living room switched off
triggers:
  - trigger: device
    domain: better_thermostat
    device_id: 0123456789abcdef0123456789abcdef
    entity_id: climate.living_room
    type: hvac_mode_changed
    to: "off"
    for:
      minutes: 10
actions:
  - action: notify.notify
    data:
      message: The living room thermostat has been off for 10 minutes.
```

Quote `"off"`: unquoted, YAML reads it as `false`.

---

## Writing your own blueprint

The trigger-based blueprints follow the same trigger pattern:

```yaml
triggers:
  - trigger: device
    domain: better_thermostat
    device_id: !input thermostat_device
    type: heating_active        # replace with any trigger type from the table above
```

The threshold triggers additionally accept their fields from
[Trigger parameters](#trigger-parameters): `humidity_high` takes `above:`,
`battery_low` takes `below:`, and both take an optional `for:` duration, for
example:

```yaml
triggers:
  - trigger: device
    domain: better_thermostat
    device_id: !input thermostat_device
    type: humidity_high
    above: 65
    for:
      minutes: 5
```

See the [Home Assistant blueprint documentation](https://www.home-assistant.io/docs/blueprint/)
for full authoring guidance.
