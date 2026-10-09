---
title: What you can use it for
description: Typical rooms and problems Better Thermostat solves, and the feature behind each one.
---

Each section below starts from a situation in a real home and names the part of Better Thermostat that handles it.

## The TRV reads the radiator, not the room

A TRV measures the temperature right next to the radiator, which is warmer than the rest of the room. The TRV shuts off early and the room stays too cold, or you turn it up and the room overshoots later.

Give Better Thermostat a temperature sensor placed in the room. It regulates on that sensor and corrects the TRV through its calibration offset, its target temperature or its valve, whichever the TRV supports. This is the **Temperature sensor** and the **Calibration type** in the [setup](/configuration/), and the [calibration modes](/calibration_algorithms/) decide how the correction is computed.

## Several radiators in one room

A large living room with two or three radiators should have one thermostat, not three that you set one by one.

Select all TRVs of the room under **The real thermostat**. Better Thermostat controls them together, with one target temperature and one room sensor. Each TRV keeps its own calibration settings, so a TRV with a writable valve can use direct valve control next to one that only takes a target temperature.

## Airing the room

Opening a window in winter with the heating on wastes energy, because the TRV feels the cold air and keeps heating.

Add a window sensor, or a group of window sensors. When it reports open, Better Thermostat turns the heating off; when every window is closed again, heating resumes. The two delays keep a short opening from switching anything. A door sensor works the same way with delays of its own. See [Window sensor](/faq/window-sensor/) and [Door sensor states](/faq/door-sensor/).

## Heating only while it is cold outside

In spring and autumn the heating does not need to run on a mild day.

Add an outdoor temperature sensor or a weather entity and set **The outdoor temperature when the thermostat should turn off**. While it is warmer outside than that, the room does not heat. Better Thermostat does not compare the current reading but a damped outdoor temperature: a running average over roughly the last day, in which each reading counts for as long as it was current. One warm afternoon therefore does not stop the heating. See [Summer mode](/deep-explanations/summer-mode/).

## Heating and cooling in the same room

A room with radiators and an air conditioner should not heat and cool against itself.

Select the air conditioner as **The cooling device**. Better Thermostat then keeps a heating target and a cooling target, runs the TRVs below the one and the air conditioner above the other, and gives each preset a heating and a cooling temperature. The **Tolerance** setting sets how far the room may drift before the cooler starts.

## A lower temperature at night or while you are away

Presets give a temperature a name, such as Sleep, Away or Eco. Enable the ones you want and set their temperatures on the device; see [Presets](/setup/presets/). Switching between them is up to you or an automation. The [automation blueprints](/setup/automation-blueprints/) include a night mode, an away preset when nobody is home and a weekly heating schedule.

## A room that overshoots or heats too slowly

A heavy radiator keeps heating after the valve closes; a cold, poorly insulated room takes hours to warm up. Pick the calibration mode for the room: TPI Controller for a room that overshoots, Aggressive for one that heats slowly, and AI Time Based, the default, for most others. In AI Time Based and Aggressive, **Overheating protection** helps with TRVs that do not close fully. See [Algorithm selection](/optimal-settings/algorithm-selection/).

## Limiting one radiator

One radiator heats much faster than the others in the house, or it is oversized for its room. With direct valve control, the **Valve Max Opening** number caps how far Better Thermostat opens that TRV. See [Hydraulic balance](/deep-explanations/hydraulic-balance/).

## Children or guests turn the knob

Enable **Ignore all inputs on the TRV like a child lock**. Better Thermostat then reverts any change made on the TRV and accepts changes only on its own thermostat entity. The *Child Lock* switch on the device turns this on and off, together with the TRV's own child lock where it has one.

## TRVs that stick after the summer

A valve that has not moved for months can get stuck. If your TRV has no maintenance routine of its own, enable **If your thermostat has no own maintenance mode, you can use this one**. Better Thermostat then opens and closes the valve from time to time.

## HomematicIP and its radio limit

HomematicIP devices share a tight radio duty cycle that too many writes use up. With the **HomematicIP** option enabled, Better Thermostat writes to the TRV at most once every ten minutes; the first change after you adjust the thermostat still goes out at the normal pace.

## Driving the boiler from the rooms

Better Thermostat does not control a boiler, but each room reports whether it wants heat. An automation can watch the rooms and switch a central thermostat on while any room is heating. [A central thermostat alongside TRVs](/deep-explanations/central-heating-thermostat/) has a complete example.

## Use cases it does not cover

Better Thermostat does not switch a boiler or set a flow temperature itself, has no built-in schedule and does not regulate on humidity. It also needs a separate room temperature sensor. The [known limitations](/working-devices/compatibility/#known-limitations) list these and the device-related limits.
