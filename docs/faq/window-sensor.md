---
title: Window sensor
description: How Better Thermostat reads the window sensor and how the open/close delays behave.
slug: qanda/window_sensor
---

When the configured window sensor reports **open**, Better Thermostat
turns the heating off; when it reports **closed**, heating resumes. TRVs
that cannot be switched off receive their minimum temperature instead.

## Sensor states

Better Thermostat expects a binary sensor:

- `on`, `true`, `open` — window open.
- `off`, `false`, `closed` — window closed.
- `unknown` and `unavailable` count as **closed** so heating continues:
  windows are usually closed and a lost sensor (e.g. a dead battery) must
  not stop heating. The frost floor still applies and the unavailability
  is still reported.

Any other state raises a repair issue — normalize the entity to one of
the values above, for example with a
[group helper](https://www.home-assistant.io/integrations/group/) or a
[template binary sensor](https://www.home-assistant.io/integrations/template/).

## The open and close delays

Two options debounce the sensor:

- **"Delay before the thermostat turns off when the window is opened"**
- **"Delay before the thermostat turns on when the window is closed"**

A state change only takes effect after it has persisted for the whole
delay. A window that closes again within the open delay (or reopens
within the close delay) changes nothing — short flaps, such as a door
slamming or a quick airing check, are filtered out.

- With a delay of `0` the change takes effect immediately with the event.
- While the delay is running, the displayed window state keeps showing
  the previous, committed state.
- Changing a delay in the options applies to a wait that is already in
  progress: the remaining time is recomputed from the new value.
