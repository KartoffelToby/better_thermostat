---
title: Presets
description: What each Better Thermostat preset does, what it defaults to and how to change it.
---

A preset is a named target temperature. Switching from `none` into a preset applies that preset's temperature and remembers the target you had; switching back to `none` puts that target back. No preset name carries a schedule, a timer or any other logic behind it, so `away` and `sleep` differ only in the number attached to them.

## The shipped defaults

| Preset | Default |
| --- | --- |
| Away | 16 °C |
| Sleep | 18 °C |
| Eco | 19 °C |
| Home | 20 °C |
| Comfort | 21 °C |
| Activity | 22 °C |
| Boost | 24 °C |

These are starting values, not fixed meanings. Set Away to 12 °C and Away becomes a 12 °C preset, as long as 12 °C is inside the thermostat's own range.

## Choosing which presets appear

Presets are opt-in per thermostat. Open **Settings → Devices & Services → Better Thermostat → Configure** and tick the ones you want under **Enabled Presets**. A new configuration starts with Eco alone.

Home Assistant validates the preset name before Better Thermostat sees it, so calling `climate.set_preset_mode` with a preset you have not enabled fails with `Preset mode <name> is not valid` and leaves the thermostat untouched.

## Changing a preset temperature

Every enabled preset gets its own number entity in the *Configuration* section of the Better Thermostat device, named after the preset: an enabled Eco preset gives you an `Eco` number, typically `number.<your_thermostat>_eco`. Write to it and the preset applies that temperature from then on. Writing it while its preset is already active retargets the thermostat straight away.

The value survives a restart. Home Assistant refuses a write outside the thermostat's own minimum and maximum, and activating a preset clamps the target into that range, so a preset can never ask for a temperature the device cannot reach.

With a cooler configured, each preset has two numbers instead of one: `<Preset> Min` carries the heating target and `<Preset> Max` the cooling target.

## Saving and restoring

The save happens on the way *into* a preset, and only from `none`:

- From `none` into any preset, the current target is stored and the preset temperature applied.
- Between two presets, nothing is stored again, so the value kept is still the target you had before the first preset.
- Back to `none`, the stored target is restored and forgotten.

The stored value is published as the `preset_temperature` state attribute and read back at startup, so a restart in the middle of an away period does not lose your normal target.

## Boost is the one exception

Every other preset is only a temperature. Boost also changes how the valve is driven, but only where Better Thermostat can drive it directly: with calibration type *Direct Valve Based*, Boost holds the valve at its *Valve Max Opening* (100 % unless you have capped that TRV) for as long as the room is below target, rather than modulating towards it. On the other calibration types Boost is its temperature and nothing more.

## Coming from the old save and restore actions

The three Better Thermostat actions that set and restored a temporary target were removed in 1.8.0, and the mechanism above replaces them. [Schedule and night mode](/deep-explanations/schedule-and-night-mode/) lists each retired action next to what to call instead.
