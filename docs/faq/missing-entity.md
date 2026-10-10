---
title: Missing entity
description: What the "related entity is missing" repair issue means and how to fix it.
slug: faq/missing-entity
---

Better Thermostat raises a **missing entity** repair issue when one of
the TRVs it was configured with is not available in Home Assistant.
Unavailable sensors, such as the room temperature sensor or a window
sensor, raise a [degraded mode](/faq/degraded-mode) issue instead.

## Common causes

- The device's battery is empty or the device lost its radio connection.
- The integration providing the entity is not loaded or failed to start.
- The entity was renamed or removed, so the entity id Better Thermostat
  was configured with no longer exists.

## How to fix it

1. Open **Settings → Devices & services** and find the entity named in
   the issue. Check the device's battery and reconnect it if necessary.
2. If the entity was renamed or the device replaced, its entity id
   changed and Better Thermostat still looks for the old one. Either
   rename the entity back, or open the Better Thermostat under
   **Settings → Devices & services → Better Thermostat → Configure** and
   select the new entity in place of the old one.
3. If the TRV is gone for good, remove it from the Better Thermostat
   configuration.

The issue clears on its own when the entity is available again, or when it
is removed from the Better Thermostat configuration. When Home Assistant
starts or the Better Thermostat entry reloads, for example after its
settings were saved, a TRV gets a few minutes to come up before the issue is
raised again.
