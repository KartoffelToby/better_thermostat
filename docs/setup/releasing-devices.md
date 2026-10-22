---
title: Releasing the devices
sidebar:
    order: 9
description: The Release devices switch hands a room's TRVs and cooler to you or an automation, a TRV's Release switch hands over that TRV alone, and how Better Thermostat takes them back.
---

Better Thermostat holds the devices of a room to what it decided. A TRV turned at its knob or an air conditioner switched at its remote is put back, or its setting becomes the room's new target. That is what you want while it controls the room, and it gets in the way when you want to run a device yourself for a while: an air conditioner's self-cleaning sequence, radiators held open while the heating system is filled, or an air conditioner run by hand while Better Thermostat is off.

The **Release devices** switch on the Better Thermostat device is for that. You find it in the *Configuration* section of the device page, and an automation can switch it like any other switch. To hand over one TRV of a room and leave the others to Better Thermostat, use that TRV's own **Release** switch, described [below](#releasing-a-single-trv).

## While the switch is on

The devices of the room are yours: every TRV and the cooler, if one is configured.

- Better Thermostat sends them nothing: no mode, no target temperature, no calibration offset, no valve position, no room temperature for a TRV that regulates on it, no valve maintenance run. A valve maintenance that is due waits until the switch is off again. The *Run valve maintenance now* action and the *Child Lock* switch refuse with a message, because both write to a device.
- Nothing a device reports changes Better Thermostat's own mode or targets. Switching a TRV on at the device does not switch the room on.
- Better Thermostat learns nothing from the room, the same as with [Pause Learning](/setup/pause-learning/). The heating power and heat loss it is following are given up, because what the room does now is not the result of its control. The *Pause Learning* switch itself stays as it is, and the `learning_paused` attribute does not change.
- A device that goes offline raises no repair issue. One that is already raised is withdrawn and comes back once the switch is off, if the device is still missing.

The thermostat itself stays available and keeps its own mode, target temperatures and preset. You can still change them; they apply when the switch is turned off. Its HVAC action keeps showing whether the room calls for heat by those targets, whatever the released devices do. Its attribute `devices_released` is `true`, so a template or a condition can read it.

A TRV that takes the room temperature from Better Thermostat, such as a Sonoff TRVZB, keeps the last value it was sent and falls back to its own sensor once its silence window is over.

## Turning the switch off

Better Thermostat takes the devices back from the state they are in and brings them onto its own mode and targets with one control cycle. That cycle still learns nothing, so no learning interval spans the time the devices were yours; learning resumes with the cycle after it, from the values held. Better Thermostat does not take over what the devices were set to while they were released: a radiator held at 30 °C for a maintenance job or an air conditioner left in a cleaning mode is not where the room should stay.

The switch keeps its state over a restart. A Better Thermostat that starts with its devices released does not set them up and writes nothing to them; it does both when the switch is turned off. It also starts when none of its TRVs is online, on its saved mode and targets.

## Releasing a single TRV

Each TRV of a Better Thermostat has a **Release** switch of its own in the *Configuration* section of the device page, named after the TRV, for example *Bathroom radiator Release*; with a single TRV it is called *TRV Release*. While it is on, that TRV is yours and Better Thermostat treats it like a TRV that is offline. The room's other TRVs are controlled as before.

- Better Thermostat sends the released TRV nothing: no mode, no target temperature, no calibration offset, no valve position, no room temperature, no valve maintenance run, no child lock. Its *Child Lock* switch refuses a change with a message. A valve maintenance run exercises the other valves; one that would only exercise released valves waits, and the *Run valve maintenance now* action refuses it.
- Nothing the released TRV reports changes Better Thermostat's mode or targets. Switching it on at the device does not switch the room on, and turning it down does not change the room's target. Whether the room switches off because all its TRVs are switched off at the device is decided by the TRVs Better Thermostat still controls.
- Better Thermostat learns nothing while any TRV is released, as with **Release devices**: the released radiator heats the room as well, so the room's response is not the result of the control alone. The *Pause Learning* switch and the `learning_paused` attribute stay as they are.
- The released TRV going offline raises no repair issue, and one raised before the release is withdrawn.
- The cooler stays under control. Only a device wired as a TRV and as the cooler at once is released in both roles by its switch, because both write to the same device.

The thermostat's `released_trvs` attribute lists the released TRVs by entity id, and `active_overrides` holds `trvs_released` while at least one of them is. Its HVAC action and its call for heat still follow the room as a whole, so a released radiator that heats can still show the room as heating.

When every TRV of a room is released, Better Thermostat controls no TRV, and `heating_blocked_by` is `devices_released`, as with **Release devices**. The two differ in the cooler: with each TRV released on its own, the air conditioner of the room is still controlled. Use **Release devices** to hand over the air conditioner as well.

Turning a TRV's switch off takes that TRV back the same way **Release devices** takes all of them back: one control cycle brings it onto the room's mode and targets, nothing it was set to while released becomes the room's, and learning resumes one cycle later. The switch keeps its state over a restart, and a Better Thermostat that starts with a TRV released does not set that TRV up until the switch is turned off. At the start, Better Thermostat also reads no target, mode or temperature from that TRV, and it waits for one of the room's other TRVs to come online. With every TRV released, it reads the TRVs at the start, as it does for a room with its devices released.

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

The **Release devices** switch releases every device of a Better Thermostat. Where one Better Thermostat drives both the radiators and the air conditioner of a room, release the radiators with their own **Release** switches instead to keep the air conditioner controlled. An air conditioner wired as a TRV can be released on its own.

### Run one radiator by hand for a while

A radiator in a room with several heads sometimes has to do something else for a few hours: a towel radiator heated up for drying, or a radiator turned off next to a sleeping baby. Release it, set it, and take it back after two hours; the other radiators of the room stay under Better Thermostat's control the whole time.

```yaml
alias: Bathroom towel radiator by hand for two hours
sequence:
  - action: switch.turn_on
    target:
      entity_id: switch.bathroom_towel_radiator_release
  - action: climate.set_temperature
    target:
      entity_id: climate.bathroom_towel_radiator
    data:
      hvac_mode: heat
      temperature: 28
  - delay: "02:00:00"
  - action: switch.turn_off
    target:
      entity_id: switch.bathroom_towel_radiator_release
mode: restart
```

Taking the radiator back sets it to the bathroom's target again.
