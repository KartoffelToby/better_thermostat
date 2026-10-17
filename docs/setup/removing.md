---
title: Removing Better Thermostat
sidebar:
    order: 7
description: How to remove one Better Thermostat or the whole integration, and what is left on your devices afterwards.
---

## Remove a single Better Thermostat

1. Go to **Settings → Devices & Services → Better Thermostat**.
2. Open the menu (⋮) of the thermostat you want to remove and choose **Delete**.

Better Thermostat deletes what it stored for that thermostat: its learned values, its saved state and the repair issues it raised. Its entities and its device disappear with it.

Automations, scripts and dashboards that use its entities are not changed. Home Assistant shows them with an unknown entity until you edit them.

## Remove the integration

1. Delete every Better Thermostat as described above.
2. In **HACS**, open Better Thermostat, open the menu (⋮) and choose **Remove**.
3. Restart Home Assistant.

If you installed it by hand, delete the folder `custom_components/better_thermostat` from your configuration directory instead of step 2.

## What stays on your devices

Removing Better Thermostat writes nothing to your thermostats. Each one keeps what Better Thermostat last sent it:

- the target temperature and the HVAC mode,
- with offset-based calibration, the last calibration offset,
- with valve-based calibration, the last valve position,
- the child lock, if Better Thermostat switched it on.

Set these back in the thermostat's own integration if you want its factory behaviour. An offset left on the device makes the thermostat read the room warmer or colder than it is, so check the offset first.
