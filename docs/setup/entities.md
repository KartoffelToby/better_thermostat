---
title: Entities
description: Every entity a Better Thermostat device creates, what its value means and when it exists.
---

Each Better Thermostat you set up is one device in Home Assistant. Besides the thermostat itself, the device carries number, switch and sensor entities. Some of them exist for every device; others only exist while a preset is enabled, a cooler is configured or a TRV uses a particular calibration mode. Better Thermostat creates and removes those as you change the settings.

The names below are the ones shown in the frontend. Where a device controls more than one TRV, the entities that belong to one TRV carry that TRV's name in front, for example *Living room left PID Kp (Proportional)*; with a single TRV the name is left out.

## Climate

The thermostat entity carries the name of the device. You set the target temperature, the HVAC mode and the preset on it, and it shows the room temperature from your temperature sensor and, if you configured one, the humidity.

You can change the thermostat's entity ID in the entity settings. Better Thermostat then reloads the device and carries on under the new ID with the same targets, preset and learned state.

### Why the room is not heated

The thermostat's `heating_blocked_by` attribute names what keeps Better Thermostat from heating the room. It comes from the same decision that sets the TRVs, so it changes when the TRVs are told to, and it holds one of these values:

| Value | The room is not heated because |
| --- | --- |
| `devices_released` | The **Release devices** switch is on, and the devices are yours. See [Releasing the devices](/setup/releasing-devices/). |
| `startup` | Better Thermostat is starting. The thermostat is unavailable until the startup ends and shows no attributes meanwhile, so the value is listed for completeness. |
| `valve_maintenance` | A valve maintenance run is exercising the valves. |
| `hvac_off` | The thermostat's mode is off. |
| `window` | A window is open and window detection is on. With a window and a door open, the window is named. |
| `door` | A door is open and window detection is on. |
| `no_call_for_heat` | The outdoor temperature or the weather forecast is above the *outdoor temperature when the thermostat should turn off*. See [Summer mode](/deep-explanations/summer-mode/). |

The attribute is empty (`null`) while Better Thermostat heats the room towards its target. That does not mean a valve is open: once the room is warm enough, the TRVs close on their own. The HVAC action shows whether the room is heating at the moment.

The `active_overrides` attribute lists the switches that are away from their default, whether or not they block the heating at the moment. It is an empty list when none is:

| Value | Switch |
| --- | --- |
| `devices_released` | **Release devices** is on. |
| `learning_paused` | **Pause Learning** is on. |
| `window_detection_off` | **Window Detection** is off. |

Pause Learning and a window detection that is off never stop the heating, so they show here and not in `heating_blocked_by`. The list lets an automation notice a switch that another automation turned and never turned back. This one sends a notification when any of the switches has been held for twelve hours:

```yaml
alias: Living room switch left on
triggers:
  - trigger: template
    value_template: >-
      {{ state_attr('climate.living_room', 'active_overrides') | default([], true) | length > 0 }}
    for:
      hours: 12
actions:
  - action: notify.notify
    data:
      message: >-
        The living room thermostat still holds
        {{ state_attr('climate.living_room', 'active_overrides') | join(', ') }}.
```

A template reads the block the same way, for example `{{ state_attr('climate.living_room', 'heating_blocked_by') == 'window' }}` to show a card only while an open window holds the heating off. Automations compare against the values in both tables, so they are kept as they are; a later version may add a value but does not rename one.

## Numbers

All numbers are in the *Configuration* section of the device page.

### Preset temperatures

Every preset you enable under **Enabled Presets** gets a number that holds its temperature: **Eco**, **Away**, **Boost**, **Comfort**, **Home**, **Sleep** and **Activity**. A preset that is not enabled has no number, and disabling a preset removes its number. The value is shown in your system's temperature unit. See [Presets](/setup/presets/) for what each preset does.

With a cooler configured, each enabled preset has two numbers instead of one:

| Number | Holds |
| --- | --- |
| Eco Min, Away Min, Boost Min, Comfort Min, Home Min, Sleep Min, Activity Min | The heating target of the preset |
| Eco Max, Away Max, Boost Max, Comfort Max, Home Max, Sleep Max, Activity Max | The cooling target of the preset |

The Min number replaces the plain preset number, so a device with a cooler has *Eco Min* and *Eco Max* but no *Eco*. While a preset is active, a Max value at or below the heating target is raised to one step above it.

### PID gains

**PID Kp (Proportional)**, **PID Ki (Integral)** and **PID Kd (Derivative)** exist for each TRV that uses the PID Controller calibration mode. They show the gains the controller currently uses and let you set them. A value you set applies to the TRV at every target temperature. Switching the TRV to another calibration mode removes them. See [Auto-tuning and manual tuning](/calibration_algorithms/#auto-tuning-and-manual-tuning).

### Valve Max Opening

**Valve Max Opening** exists for each TRV whose calibration type is *Direct Valve Based*. It caps the valve opening Better Thermostat writes to that TRV, from 0 to 100 %. The default is 100 %.

### Outdoor Temperature Threshold and Tolerance

**Outdoor Temperature Threshold** exists while an outdoor sensor or a weather entity is configured, and **Tolerance** exists for every device. They hold the *outdoor temperature when the thermostat should turn off* and the *tolerance* from the device's settings: the number and the settings show the same value, and a change in either shows in the other and takes effect at once, without restarting the device. Both are in your system's temperature unit; the tolerance is a temperature difference. The threshold is stored in whole degrees, as the settings store it, so a value between two degrees is rounded. The tolerance goes from 0 to 10 °C (18 °F).

Set them from an automation to change several devices at once, for example to follow an `input_number` helper. See [Configuration](/configuration/#first-step) for what the two values do and [Summer mode](/deep-explanations/summer-mode/#changing-the-threshold) for an example.

## Switches

**Release devices** exists for every device. Turned on, it hands the room's TRVs and cooler to you: Better Thermostat writes nothing to them, takes over nothing they report and learns nothing until it is turned off, which brings them back onto the thermostat's own settings with one control cycle. It keeps its state over a restart. See [Releasing the devices](/setup/releasing-devices/).

**Pause Learning** exists for every device. While it is on, Better Thermostat keeps regulating but learns nothing from the room: the heating power, the heat loss and the state of every calibration controller stay as they are. Turn it on from an automation while the room behaves in a way that would teach the controllers something wrong, such as a boiler heating hot water or a lit fireplace. See [Pausing learning](/setup/pause-learning/).

**Child Lock** exists for every TRV. It turns the *Ignore all inputs on the TRV like a child lock* option on and off and, where the TRV has a child lock entity of its own, that one too. While the devices are released it cannot be changed. Better Thermostat looks for a switch or lock on the TRV's device named for the child lock first; only when there is none does it use one whose name merely contains "lock". The switch and the startup sync of the option pick the same entity.

**Window Detection** exists for every device with a window or door sensor. It is on by default, and then an open window or door turns the heating off as described in [Window sensor](/faq/window-sensor/). Turned off, the window and door sensors no longer affect the heating, and a close delay that is still running is dropped, so heating resumes at once. The thermostat shows the setting in its `window_detection` attribute, and the switch keeps its state across restarts. See [Turning window detection off](/faq/window-sensor/#turning-window-detection-off).

**PID Auto Tune** exists for each TRV that uses the PID Controller calibration mode. Turned on, the controller adjusts its gains itself; turned off, the gains the TRV uses at that moment stay as they are until you set them. The switch applies to the TRV at every target temperature, including one it has never run at.

## Sensors

These sensors exist for every device.

| Sensor | Unit | What it shows |
| --- | --- | --- |
| Temperature EMA | system temperature unit | The room temperature after smoothing: an exponential moving average of your temperature sensor with a time constant of five minutes, updated with every accepted reading and once a minute; a new value shows when it moves by at least 0.01 °C. |
| Temperature EMA 1h | system temperature unit | The same room temperature smoothed again with a time constant of one hour. The sensor computes it itself and starts over from the current temperature after a restart. |
| Temperature Slope | K/min | How fast the smoothed room temperature changes, in kelvin per minute. Positive while the room warms up, negative while it cools down. |
| Heating Power | K/min | How fast the room warms up while it heats, as learned from past heating cycles. |
| Heat Loss | K/min | How fast the room cools down while it does not heat, as learned from past idle periods. |

## Algorithm sensors

These sensors show the internal values of a calibration algorithm. They exist only while at least one TRV of the device uses that algorithm, and switching the last TRV away from it removes them. There is one set per device; with several TRVs on the same algorithm, the sensor shows the value of the first TRV that reports one. While a window or door is open, the thermostat is off or Better Thermostat itself is unavailable, they are unavailable.

On new installations these sensors start disabled. Enable the ones you want to watch on the device page: open the sensor and turn on **Enabled** in its settings. Installations that had them before keep them enabled, and a sensor you enabled stays enabled when you switch the calibration mode away and back again.

### MPC Predictive

| Sensor | Unit | What it shows |
| --- | --- | --- |
| Virtual Temperature | system temperature unit | The room temperature the MPC model predicts, corrected by each new sensor reading. The controller plans from this value instead of the raw sensor. |
| MPC Gain | K/min | How fast the model expects the room to warm up with the valve fully open. |
| MPC Loss | K/min | How fast the model expects the room to cool down with the valve closed. |
| MPC Insulation (Ka) | 1/min | The heat loss per kelvin of difference between room and outside. A lower value means a better insulated room. It needs an outdoor temperature sensor or a weather entity. |

### MPC v2

| Sensor | Unit | What it shows |
| --- | --- | --- |
| MPC v2 Virtual Temperature | system temperature unit | The room temperature the MPC v2 observer estimates from the sensor readings and its room model. |
| MPC v2 Coupling | none | How strongly the radiator heats the room, relative to how strongly the room loses heat to the outside. The room model starts at 1. |
| MPC v2 Disturbance | K/min | Heat the room model does not explain, such as sun, people or cooking. Positive values mean extra heat, negative values extra loss. |
| MPC v2 Room Time Constant | min | How slowly the room follows a change in heating. A larger room, or one that holds its heat better, has a longer time constant. The *MPC v2 room size* option sets where it starts. |

### PID Controller

| Sensor | Unit | What it shows |
| --- | --- | --- |
| PID Output | % | The sum of the three PID terms. Better Thermostat limits it to the range from 0 % to the TRV's Valve Max Opening before it becomes the valve opening, so the sensor can show values outside that range. |
| PID Error | K | The target temperature minus the room temperature. Positive while the room is too cold. |

The gains in use are shown by the PID gain numbers above, which also set them.
