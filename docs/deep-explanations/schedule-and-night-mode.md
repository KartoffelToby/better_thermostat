---
title: Schedule and night mode
description: Migrate temporary target schedules from the removed Better Thermostat save/restore actions to climate.set_preset_mode.
---

Better Thermostat has no save, set-temporary or restore actions of its own. Temporary target
schedules run on the standard Home Assistant action `climate.set_preset_mode`: entering a preset
stores the current target temperature, returning to `preset_mode: none` puts it back.

The stored value is published as the `preset_temperature` state attribute and read back when the
entity starts, so it survives a Home Assistant restart.

## What to call instead

| Removed action | Replacement |
| --- | --- |
| `better_thermostat.save_current_target_temperature` | Nothing to call: `climate.set_preset_mode` stores the current target as it enters a preset |
| `better_thermostat.set_temp_target_temperature` | `climate.set_preset_mode` with the preset you want, optionally preceded by `number.set_value` on that preset's temperature entity |
| `better_thermostat.restore_saved_target_temperature` | `climate.set_preset_mode` with `preset_mode: none` |

## Step 1: enable the preset

Presets are opt-in per thermostat. Open **Settings → Devices & Services → Better Thermostat →
Configure** and tick the preset you want under **Enabled Presets**. The available choices are
`eco`, `away`, `boost`, `comfort`, `home`, `sleep` and `activity`.

A thermostat only accepts presets that are enabled for it. Home Assistant checks the preset name
before Better Thermostat sees it and rejects anything else with `Preset mode <name> is not valid.
Valid preset modes are: …`. The action fails, the thermostat is left untouched and the rest of the
automation does not run, so this step is not optional.

## Step 2: set the preset temperature

Every enabled preset gets its own number entity in the *Configuration* section of the Better
Thermostat device, for example `number.bt_living_room_sleep`. Look the exact entity id up on the
device page, then set the temperature you want the preset to apply.

With a cooler configured, each preset has a pair instead: `<Preset> Min` carries the heating
target and `<Preset> Max` the cooling target, with `_min` and `_max` entity id suffixes.

## Step 3: rewrite the automation

Before:

```yaml
- action: better_thermostat.set_temp_target_temperature
  target:
    entity_id: climate.bt_living_room
  data:
    temperature: 18
# … when the schedule ends
- action: better_thermostat.restore_saved_target_temperature
  target:
    entity_id: climate.bt_living_room
```

After:

```yaml
- action: climate.set_preset_mode
  target:
    entity_id: climate.bt_living_room
  data:
    preset_mode: sleep
# … when the schedule ends
- action: climate.set_preset_mode
  target:
    entity_id: climate.bt_living_room
  data:
    preset_mode: none
```

The night temperature now lives in `number.bt_living_room_sleep` instead of in the automation.

## When the temperature is computed at runtime

The removed action carried a `temperature:` field, which is what a template-driven schedule used.
Write the value to the preset's number entity first, then activate the preset:

```yaml
- action: number.set_value
  target:
    entity_id: number.bt_living_room_sleep
  data:
    value: "{{ states('input_number.night_temp') }}"
- action: climate.set_preset_mode
  target:
    entity_id: climate.bt_living_room
  data:
    preset_mode: sleep
```

Writing the number while its preset is already active retargets the thermostat immediately and
leaves the stored restore point untouched, so a schedule may adjust the value mid-window.

## When you do not need a restore

If nothing has to be put back afterwards, skip the presets and set the target directly:

```yaml
- action: climate.set_temperature
  target:
    entity_id: climate.bt_living_room
  data:
    temperature: 18
```

## If you do not want to spend a preset slot

A scene snapshot restores the target without occupying one of the seven presets:

```yaml
- action: scene.create
  data:
    scene_id: living_room_before_night
    snapshot_entities:
      - climate.bt_living_room
- action: climate.set_temperature
  target:
    entity_id: climate.bt_living_room
  data:
    temperature: 18
# … when the schedule ends
- action: scene.turn_on
  target:
    entity_id: scene.living_room_before_night
```

This restores more than the removed action did: the snapshot also carries `hvac_mode` and the
preset, and turning the scene on puts both back. Presets are the supported route; reach for
scenes only when you need the extra state or have run out of preset slots.

## What the round trip does and does not restore

- Entering a preset from `none` stores the current target temperature.
- Switching from one preset straight to another keeps the originally stored value; only the
  target changes.
- Returning to `none` applies the stored value and clears the store. A second `none` call is a
  no-op.
- **Changing the target manually while a preset is active cancels the preset.** Better Thermostat
  keeps the manual value, drops back to `none` and discards the stored restore point, so a later
  `preset_mode: none` changes nothing. The removed restore action behaved differently here: it
  overrode a manual change. Automations that relied on that need the manual value written back
  explicitly.
- Writing a preset's number entity while that preset is active is not a manual change: the preset
  stays active and the restore point survives.

## Ready-made automations

The bundled [automation blueprints](/setup/automation-blueprints/) already use presets. The night
mode blueprint drives the `sleep` preset from a Home Assistant `schedule` helper and returns to
`none` when the window closes:

<a href="https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https://github.com/KartoffelToby/better_thermostat/blob/master/blueprints/night_mode.yaml" target="_blank"><img src="https://my.home-assistant.io/badges/blueprint_import.svg" alt="Import night mode blueprint" /></a>
