---
title: Summer mode
description: How Better Thermostat decides from the outdoor temperature whether a room heats, why a sensor in the sun can keep the heating off, and how to check the decision.
---

With an outdoor sensor or a weather entity and an outdoor temperature threshold, Better
Thermostat turns the thermostats off when it is warm outside. This is summer mode. The
thermostats stay off until the outdoor temperature has dropped again, even if you switch the
Better Thermostat entity to heat by hand: the entity keeps its mode, and its thermostats follow
the outdoor decision.

## With an outdoor sensor

Better Thermostat does not compare the sensor's current reading with the threshold. It uses a
damped outdoor temperature, the way central heating controllers do: a running average over
roughly the last day.

- **Each reading counts for as long as it was current.** A sensor reports when its value
  changes, so it sends many readings while the temperature moves and few while it holds. A
  reading that held for six hours of night weighs six hours; fifty readings during a sunny
  quarter of an hour weigh a quarter of an hour.
- **A change takes time to show.** After a day of steady warm weather the damped temperature
  has covered about two thirds of the way to it, after two days about 86 %. A single warm
  afternoon moves it a few degrees at most.
- **It is filled from history at startup.** When Better Thermostat starts, it runs the last three
  days of the sensor's recorded history through the average. Without recorded history, for
  example right after adding a new sensor or with the recorder disabled, it starts at the
  current reading and builds up from there.

### Where to mount the sensor

The damped temperature cannot tell sunshine on the sensor from warm air. A sensor in direct sun
easily reads 30 °C or more on a cool autumn day, and while the average weighs those hours
correctly, it still counts them as warm. Mount the sensor in the shade, ideally on a north wall,
with a little distance from the wall and protection from rain. If you cannot avoid the sun, use a
weather entity instead. Raising the threshold also helps, but it raises it on cloudy days too:
the heating then stays on longer into spring.

## With a weather entity

Without an outdoor sensor, Better Thermostat uses two numbers from the weather entity, and the
room heats while either one is below the threshold:

- **The damped current temperature.** The entity's current temperature passes through the same
  running average as an outdoor sensor's readings, filled from its recorded history at startup.
  A cold night reading therefore does not resume heating in May, and a warm afternoon does not
  stop it in October.
- **The forecast for roughly the next two days.** A daily forecast gives each day's high and low;
  their mean stands for the day. Hourly and twice-daily forecasts are averaged as they come.

The first lets a cold building heat although warm days are forecast; the second resumes heating
early when a cold spell is coming. Both stand for whole days, nights included, so they stay well
below the afternoon high: a May day forecast at 24 °C by day and 9 °C at night counts as 16.5 °C.
Choose the threshold with daily means in mind. Better Thermostat checks once an hour. If the weather entity
gives no forecast for three hours, the room heats until it does again.

When both are configured, the outdoor sensor decides and the weather entity is ignored.

## Switching off and back on

Summer mode starts once the outdoor temperature reaches the threshold. It ends once the
temperature has dropped 1 °C (1.8 °F) below the threshold. With a threshold of 18 °C, the
thermostats turn off at 18 °C and back on at 17 °C. An outdoor temperature that hovers around
the threshold therefore does not switch the thermostats on and off repeatedly.

## Checking the decision

- **The `call_for_heat` attribute.** Open **Developer Tools → States** and select the Better
  Thermostat climate entity, not one of its thermostats. `call_for_heat: false` means summer
  mode is on.
- **The logbook.** Better Thermostat writes "turned off because the outdoor temperature is too
  high" when summer mode starts and "resumed heating because the outdoor temperature dropped"
  when it ends.
- **The diagnostics download.** Its `summer_mode` section lists the damped temperature of the
  outdoor sensor and of the weather entity, the forecast mean, the last reading of each and when
  it arrived, the threshold the room is held against, and whether the recorded history was read.
  Attach it when you report a problem with summer mode.
- **The debug log.** With debug logging enabled, every check logs the damped outdoor
  temperature and the temperature below which the room heats.

If summer mode is on although it feels cold, look at the sensor's history for the last two days.
High peaks around noon point to a sensor in the sun.
