---
title: Upgrading from 1.9 to 2.0
sidebar:
    order: 5
description: What carries over from Better Thermostat 1.9, what you may need to change, and how 2.0 behaves differently.
---

Your rooms, entities and learned values carry over to 2.0. You don't need
to set anything up again. Most of the changes are in how Better Thermostat
reacts when a sensor, a thermostat or the weather service stops answering.
This page compares 2.0 with 1.9.2, the last 1.9 release.

## Before you upgrade

- **Home Assistant 2026.9.0 or newer is required.** 1.9.2 ran on
  2026.7.2. Update Home Assistant first.
- **Take a Home Assistant backup.** You can go back to 1.9.2 without one
  (see [Going back to 1.9.2](#going-back-to-192)), but a backup is the
  safe way back.

## What carries over

- **Configuration:** every room keeps its settings. 2.0 does not rewrite
  the stored configuration when it starts.
- **Entities:** entity ids and their history stay the same.
- **Learned values:** heating power, heat loss, and the PID, TPI, MPC and
  MPC v2 state continue where 1.9.2 left off.
- **Services:** the same three services exist with the same fields.
- **Calibration modes:** the list of modes and the default (AI Time Based)
  are the same. Your selected mode stays selected.

## What you may need to change

### A thermostat in more than one room

A thermostat (TRV) can now belong to only one Better Thermostat. Two
entries writing to the same valve fight each other, so 2.0 no longer lets
you set that up:

- Creating a room or adding a thermostat to one is refused when another
  Better Thermostat already controls that thermostat.
- A setup that already shares a thermostat keeps running, but Home
  Assistant shows a repair issue, "controlled by more than one Better
  Thermostat".

Remove the thermostat from every room but one. The repair issue clears by
itself.

### Templates and automations

- **`saved_temperature` is gone.** The climate entity no longer has this
  attribute. Nothing in 1.9 set it, so it only ever held an old value or
  nothing. The temperature a preset returns to is still in
  `preset_temperature`.
- **Device triggers fire less often.** "Thermostat switches to heating",
  "Thermostat stops heating", "Window opened" and "Window closed" now fire
  only on a real change. A thermostat or sensor that goes unavailable and
  comes back no longer fires them. If an automation relied on that, trigger on
  availability instead.
- **Invalid service calls now fail.** Setting an HVAC mode other than heat,
  heat/cool or off, or a temperature that isn't a number, raises an error.
  1.9.2 logged these calls and ignored them. An automation that sent such
  values now shows the error in its trace.
- **Fahrenheit systems:** `target_temp_step` is now Home Assistant's own
  attribute, in °F. 1.9.2 overwrote it with the step in °C.

### Cooling devices

The option "Minimum seconds between repeated cooler commands" is gone.
Better Thermostat now holds back an unchanged temperature or mode command
if it sent the same value less than 240 seconds earlier, to protect the
compressor. A changed value goes out with the next control cycle; if the
previous command failed, it waits at least 30 seconds after that failure. A
failed command is retried with its own growing pause, from 30 seconds up to
30 minutes. You don't need to do anything; a
value you set before is ignored.

### Entities of a calibration mode you no longer use

When you switch a thermostat to another calibration mode, 2.0 removes the
old mode's entities, such as the PID numbers and switches. 1.9.2 left them
behind as unavailable. Automations or dashboards that still point at such
an entity lose it.

## What behaves differently

### A window or door sensor stops reporting

1.9.2 treated an unavailable window or door sensor as **open**: heating
paused until the sensor came back. 2.0 treats it as **closed**, so the
room keeps heating, and reports the sensor through the
[degraded mode](/faq/degraded-mode/) repair issue.

If a window sensor with a flat battery used to stop your heating and you
relied on that, keep an eye on your battery levels.

### The room temperature sensor stops reporting

1.9.2 kept controlling on the last reading it had. 2.0 steps down:

1. After about two minutes without a reading, it controls on the average
   of the thermostats' own temperatures.
2. If no temperature is usable at all, it stops adjusting and keeps the
   last state.
3. Once the room sensor has reported steadily for five minutes, it goes
   back to normal control.

The climate entity shows the current step in `control_mode` (`optimal`,
`sensor_fallback` or `hold`) and how long it has been degraded in
`degraded_for_s`. A thermostat's own sensor sits next to the hot valve, so
control is less accurate while the fallback runs.

### A thermostat doesn't come back after a restart

In 1.9.2 a room waited for every thermostat. One that never came back left
the whole room uncontrolled, windows and sensors included. In 2.0 the room
starts with the thermostats that answer after a two-minute grace period.
The missing one joins when it reports.

### The weather service fails

Without an outdoor sensor, Better Thermostat uses the weather forecast to
decide whether the room needs heat. 1.9.2 switched heating on as soon as a
forecast failed. 2.0 keeps its last decision for three hours. If that
decision was not to heat, it then resumes heating with the logbook entry
"resumed heating because the weather forecast is unavailable".

### A thermostat keeps rejecting commands

1.9.2 retried a failed control cycle right away, over and over. 2.0 doubles
the pause after each failure, up to five minutes. With the HomematicIP
option, its ten minutes between writes can make a retry wait longer. A
retry that gets through, a new target temperature or a new HVAC mode resets
the pause. Your logs get much quieter.

### Smaller changes

- **Target temperatures** are clamped to the room's range and rounded to
  its step when you set them.
- **Fahrenheit systems** show tenths of a degree.
- **`min_temp` and `max_temp`** on the climate entity now cover the heating
  and the cooling range together.

## What is new

- **Sensors for MPC v2:** thermostats on the MPC v2 calibration mode get
  four diagnostic sensors: virtual temperature, coupling, disturbance and
  room time constant.
- **Climate attributes:** `control_mode` and `degraded_for_s` (see above),
  and `calibrator_health`, which reports per thermostat whether its
  controller is healthy.
- **Direct valve control** is now also offered for a Sonoff TRVZB outside
  Zigbee2MQTT, for example one paired through ZHA, as long as its valve
  opening shows up as an enabled number entity on the same device.

## Going back to 1.9.2

Both versions store the configuration and the learned values in the same
format, so you can install 1.9.2 again through HACS and keep your rooms.

What you lose by going back:

- **MPC v2 starts learning again.** 1.9.2 can't read the controller state
  2.0 saves, and also drops the room model and temperature filters that
  only 2.0 keeps. Everything else you learned stays.
- **The behaviour changes on this page are undone.** For example, 1.9.2
  again treats an unavailable window sensor as open.
