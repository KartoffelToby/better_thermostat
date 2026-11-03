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
- A window opened or closed while Better Thermostat is still starting up
  is picked up when startup finishes, and the delays apply to it as to
  any other change. The same holds for a door sensor.

## Turning window detection off

Every device with a window or door sensor has a **Window Detection**
switch in the *Configuration* section of its device page. It is on by
default, and the sections above describe what it does then.

Turned off, the window and door sensors no longer affect the heating:

- Heating resumes at once, even while a window is open. A close delay
  that is still running is dropped as well, so a window you have just
  closed stops holding the heating off.
- The thermostat's `window_open` and `door_open` attributes still show
  what the sensors report, and the device triggers for an opened or
  closed window still fire. The delays do not apply to them while the
  switch is off.
- An open window still counts for learning: a heating cycle that runs
  with the window open does not teach the heating power, and the heat
  loss is not learned while it is open.
- The thermostat shows the setting in its `window_detection` attribute.

Turned back on while a window is open, the window counts as just opened:
heating stops once the open delay has run out, as with any other opening.
With an open delay of `0` it stops at once.

The switch keeps its state across restarts. A switch that stays off by
mistake, for example from an automation that never turns it back on,
shows as `window_detection: false` on the thermostat.

### Lower the temperature instead of switching off

Some TRVs drain their battery when they are switched off and on often.
To keep them on at the Eco temperature while a window is open, turn
**Window Detection** off and let an automation set the Eco preset. The
automation saves the thermostat's state when the window opens and puts
it back when the window closes, so the preset you had before returns.
The `for:` lines take the place of the open and close delays.

```yaml
automation:
  - alias: "Living room: Eco while the window is open"
    mode: restart
    triggers:
      - trigger: state
        entity_id: binary_sensor.living_room_window
        to: "on"
        for: "00:00:30"
        id: opened
      - trigger: state
        entity_id: binary_sensor.living_room_window
        to: "off"
        for: "00:00:30"
        id: closed
    actions:
      - choose:
          - conditions:
              - condition: trigger
                id: opened
            sequence:
              - action: scene.create
                data:
                  scene_id: living_room_before_airing
                  snapshot_entities:
                    - climate.living_room
              - action: climate.set_preset_mode
                target:
                  entity_id: climate.living_room
                data:
                  preset_mode: eco
          - conditions:
              - condition: trigger
                id: closed
            sequence:
              - action: scene.turn_on
                target:
                  entity_id: scene.living_room_before_airing
```

The Eco preset must be enabled under **Enabled Presets**.

### A button that resumes heating now

If you close the window and do not want to wait for the close delay, a
script that turns **Window Detection** off and on again resumes heating
at once. Turning it off drops the running close delay; turning it back
on reads the window as closed, so detection stays on for the next
opening.

```yaml
script:
  living_room_resume_heating:
    alias: "Living room: resume heating now"
    sequence:
      - action: switch.turn_off
        target:
          entity_id: switch.living_room_window_detection
      - action: switch.turn_on
        target:
          entity_id: switch.living_room_window_detection
```

Add the script to a dashboard as a button.

### Ignore a faulty window sensor

When a window sensor is stuck on *open*, or reports open while the
window is closed, turn **Window Detection** off until the sensor is
fixed. The switch can sit on a dashboard next to the thermostat. To
make sure it does not stay off for good, an automation can turn it back
on after a while:

```yaml
automation:
  - alias: "Living room: window detection back on after two hours"
    triggers:
      - trigger: state
        entity_id: switch.living_room_window_detection
        to: "off"
        for: "02:00:00"
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.living_room_window_detection
```
