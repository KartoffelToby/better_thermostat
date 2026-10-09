---
title: Window sensor
description: How Better Thermostat reads the window sensor and how the open/close delays behave.
slug: faq/window-sensor
---

When the configured window sensor reports **open**, Better Thermostat
turns the heating off; when it reports **closed**, heating resumes. TRVs
that cannot be switched off receive their minimum temperature instead.

## Changing the target while the window is open

A target you set in Home Assistant while the window is open is kept. A
knob turn is kept too when Better Thermostat holds that TRV at its minimum
temperature for the window; a TRV it switches off takes no target from its
knob, as the list below explains. Heating stays paused, and once the
window closes the room heats to the kept target, unless the turn switched
the room off.

- A TRV that Better Thermostat holds at its minimum temperature for the
  window, because it has no off mode or because **Use the minimum
  temperature instead of 'off'** is on, is turned straight back to that
  minimum after the turn. With that option on, turning the knob to the
  minimum switches the room off and turning it up switches the room on,
  as it does with the window closed.
- A TRV that Better Thermostat switches off for the window takes no target
  from its knob: Better Thermostat ignores what a TRV reports while it is
  off, so set the target in Home Assistant instead.

## Sensor states

Better Thermostat expects a binary sensor:

- `on`, `true`, `open` — window open.
- `off`, `false`, `closed` — window closed.
- `unknown` and `unavailable` count as **closed** so heating continues:
  windows are usually closed and a lost sensor (e.g. a dead battery) must
  not stop heating. The frost floor still applies and the unavailability
  is still reported. A sensor removed from Home Assistant (disabled,
  deleted or renamed in the entity registry) counts as closed in the
  same way.

Any other state raises a repair issue. Normalize the entity to one of
the values above, for example with a
[group helper](https://www.home-assistant.io/integrations/group/) or a
[template binary sensor](https://www.home-assistant.io/integrations/template/).

## The open and close delays

Two options debounce the sensor:

- **"Delay before the thermostat should turn off when the window is opened"**
- **"Delay before the thermostat should turn on when the window is closed"**

A state change only takes effect after it has persisted for the whole
delay. A window that closes again within the open delay (or reopens
within the close delay) changes nothing: short flaps, such as a door
slamming or a quick airing check, are filtered out.

- With a delay of `0` the change takes effect immediately with the event.
- While the delay is running, the displayed window state keeps showing
  the previous, committed state.
- Saving the options reloads Better Thermostat. A wait that was in
  progress is dropped, and the window state the sensor reports at that
  moment applies right away.
