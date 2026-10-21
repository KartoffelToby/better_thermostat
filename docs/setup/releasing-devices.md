---
title: Releasing the devices
sidebar:
    order: 9
description: The Release devices switch hands a room's TRVs and cooler to you or an automation, and how Better Thermostat takes them back.
---

Better Thermostat holds the devices of a room to what it decided. A TRV turned at its knob or an air conditioner switched at its remote is put back, or its setting becomes the room's new target. That is what you want while it controls the room, and it gets in the way when you want to run a device yourself for a while: an air conditioner's self-cleaning sequence, radiators held open while the heating system is filled, or an air conditioner run by hand while Better Thermostat is off.

The **Release devices** switch on the Better Thermostat device is for that. You find it in the *Configuration* section of the device page, and an automation can switch it like any other switch.

## While the switch is on

The devices of the room are yours: every TRV and the cooler, if one is configured.

- Better Thermostat sends them nothing: no mode, no target temperature, no calibration offset, no valve position, no room temperature for a TRV that regulates on it, no valve maintenance run. A valve maintenance that is due waits until the switch is off again. The *Run valve maintenance now* action and the *Child Lock* switch refuse with a message, because both write to a device.
- Nothing a device reports changes Better Thermostat's own mode or targets. Switching a TRV on at the device does not switch the room on.
- Better Thermostat learns nothing from the room. The heating power and heat loss it is following are given up, because what the room does now is not the result of its control.
- A device that goes offline raises no repair issue. One that is already raised is withdrawn and comes back once the switch is off, if the device is still missing.

The thermostat itself stays available and keeps its own mode, target temperatures and preset. You can still change them; they apply when the switch is turned off. Its HVAC action keeps showing whether the room calls for heat by those targets, whatever the released devices do. Its attribute `devices_released` is `true`, so a template or a condition can read it.

A TRV that takes the room temperature from Better Thermostat, such as a Sonoff TRVZB, keeps the last value it was sent and falls back to its own sensor once its silence window is over.

## Turning the switch off

Better Thermostat takes the devices back from the state they are in and brings them onto its own mode and targets with one control cycle. It does not take over what the devices were set to while they were released: a radiator held at 30 °C for a maintenance job or an air conditioner left in a cleaning mode is not where the room should stay.

The switch keeps its state over a restart. A Better Thermostat that starts with its devices released does not set them up and writes nothing to them; it does both when the switch is turned off. It also starts when none of its TRVs is online, on its saved mode and targets.

## Examples

The entity ids below are examples. Use the ones of your devices.

### Run an air conditioner by hand while Better Thermostat is off

Better Thermostat switches an air conditioner off again while its thermostat is off. With this automation, switching the thermostat off hands the air conditioner to you, and switching it on takes it back.

```yaml
alias: Living room air conditioner by hand while the thermostat is off
triggers:
  - trigger: state
    entity_id: climate.living_room
    to: "off"
    id: released
  - trigger: state
    entity_id: climate.living_room
    from: "off"
    id: taken_back
actions:
  - action: >-
      switch.turn_{{ 'on' if trigger.id == 'released' else 'off' }}
    target:
      entity_id: switch.living_room_release_devices
mode: queued
```

### Let an air conditioner run its self-cleaning sequence

A cleaning sequence that switches the air conditioner on, changes its mode and then starts the cleaning is interrupted by Better Thermostat after its first step. Release the devices for the length of the sequence.

```yaml
alias: Living room air conditioner self-cleaning
sequence:
  - action: switch.turn_on
    target:
      entity_id: switch.living_room_release_devices
  - action: climate.set_hvac_mode
    target:
      entity_id: climate.living_room_ac
    data:
      hvac_mode: fan_only
  - action: button.press
    target:
      entity_id: button.living_room_ac_self_clean
  - wait_template: "{{ is_state('climate.living_room_ac', 'off') }}"
    timeout: "01:00:00"
  - action: switch.turn_off
    target:
      entity_id: switch.living_room_release_devices
mode: single
```

### Hold the radiators open for maintenance or over the summer

Filling or bleeding a heating system needs the valves open for hours or days, and an opened window must not close them. Release the devices, then open the valves.

```yaml
alias: Radiators open for maintenance
sequence:
  - action: switch.turn_on
    target:
      entity_id:
        - switch.living_room_release_devices
        - switch.bedroom_release_devices
  - action: climate.set_hvac_mode
    target:
      entity_id:
        - climate.living_room_radiator
        - climate.bedroom_radiator
    data:
      hvac_mode: heat
  - action: climate.set_temperature
    target:
      entity_id:
        - climate.living_room_radiator
        - climate.bedroom_radiator
    data:
      temperature: 30
mode: single
```

Turn the switches off again when the work is done; Better Thermostat then puts the radiators back on the room's targets.

### Run an air conditioner by hand at night

An air conditioner with a Better Thermostat of its own is kept off at night by switching that thermostat off; that needs no release. Release it instead when you want to run it by hand at night without Better Thermostat interfering, and take it back in the morning:

```yaml
alias: Bedroom air conditioner by hand at night
triggers:
  - trigger: time
    at: "22:00:00"
    id: released
  - trigger: time
    at: "07:00:00"
    id: taken_back
actions:
  - action: >-
      switch.turn_{{ 'on' if trigger.id == 'released' else 'off' }}
    target:
      entity_id: switch.bedroom_ac_release_devices
```

The switch releases every device of a Better Thermostat. Where one Better Thermostat drives both the radiators and the air conditioner of a room, releasing it releases the radiators as well; releasing a single device of such a room is not possible yet.
