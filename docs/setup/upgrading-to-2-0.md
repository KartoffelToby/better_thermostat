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
- **Take a Home Assistant backup.** You can go back to 1.9.3 without one
  (see [Going back to 1.9](#going-back-to-19)), but a backup is the
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
- **Temperature differences in the learning attributes are renamed.** In
  `heating_cycle_last`, the key `delta_t` is now `delta_kelvin`. In
  `heat_loss_stats`, the key `dT` is now `delta_kelvin`. The values are the
  same. A template that reads the old key gets nothing and has to use the
  new one.
- **The preset attributes lose their `bt_` prefix.** `bt_preset_cool_temperature`,
  `bt_preset_cool_temperatures` and `bt_preset_heat_temperatures` are now
  `preset_cool_temperature`, `preset_cool_temperatures` and
  `preset_heat_temperatures`. 2.x publishes the old names as well, with the
  same values, so templates keep working; switch them to the new names before
  3.0, which drops the old ones.
- **Two more attributes are renamed the same way.** `external_temp_ema` is now
  `room_temperature_filtered`, and `temp_slope_K_min` is now
  `temperature_slope_kelvin_per_min`. 2.x publishes the old names as well;
  3.0 drops them.
- **The PID and MPC v2 diagnostic attributes spell out their units.**
  `pid_e_K` is now `pid_error_kelvin`, `pid_meas_smooth_C`
  `pid_measurement_filtered`, `pid_d_meas_K_per_min`
  `pid_measurement_slope_kelvin_per_min`, `pid_dt_s` `pid_dt_seconds`,
  `mpc_v2_D_hat_K_per_min` `mpc_v2_disturbance_kelvin_per_min`,
  `mpc_v2_tau_room_min` `mpc_v2_tau_room_minutes` and `mpc_v2_group_valve_pct`
  `mpc_v2_group_valve_percent`. 2.x publishes the old names as well; 3.0
  drops them.
- **More diagnostic attributes are renamed.** `mpc_v2_T_room_hat` is now
  `mpc_v2_room_temperature_estimate`, `mpc_v2_T_rad_hat`
  `mpc_v2_radiator_temperature_estimate`, `mpc_v2_coupling_rad_room`
  `mpc_v2_radiator_room_coupling` and `heating_power_norm`
  `heating_power_normalized`. 2.x publishes the old names as well; 3.0 drops
  them.

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
`degraded_for_seconds`. A thermostat's own sensor sits next to the hot valve, so
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

### Summer mode with an outdoor sensor

1.9.2 averaged the outdoor sensor's readings per calendar day, for today
and yesterday. Every reading counted once, so a sensor that reports often
while the sun heats it pulled the average up, and the average jumped at
midnight. 2.0 averages over roughly the last day by time: each reading
counts for as long as it was current. A warm afternoon no longer turns the
heating off on its own, and a single warm reading takes hours to show.

Summer mode also has a switch-back margin now. It starts when the outdoor
temperature reaches the threshold and ends once it is 1 °C (1.8 °F) below
it, for the outdoor sensor and the weather entity alike. 1.9.2 switched
back at the threshold itself. See [Summer mode](/deep-explanations/summer-mode/).

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
- **Climate attributes:** `control_mode` and `degraded_for_seconds` (see above),
  and `calibrator_health`, which reports per thermostat whether its
  controller is healthy.
- **Direct valve control** is now also offered for a Sonoff TRVZB outside
  Zigbee2MQTT, for example one paired through ZHA, as long as its valve
  opening shows up as an enabled number entity on the same device.

## Going back to 1.9

Go back to **1.9.3 or newer**. 2.0 keeps a thermostat's settings in the
entry's options instead of its data, and 1.9.3 is the first 1.9 release that
reads them there. Install it again through HACS and you keep your rooms and
the learned values; if you change a setting in 1.9.3, 2.0 takes the changed
value over when you update again.

1.9.2 and older read the settings from the old place only. They load your
rooms without thermostats or sensors, and saving the settings there replaces
what 2.0 stored.

What you lose by going back:

- **MPC v2 starts learning again.** 1.9 can't read the controller state
  2.0 saves, and also drops the room model and temperature filters that
  only 2.0 keeps. Everything else you learned stays.
- **The behaviour changes on this page are undone.** For example, 1.9
  again treats an unavailable window sensor as open.
