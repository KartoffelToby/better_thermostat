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
- The entity was removed, so the entity id Better Thermostat was
  configured with no longer exists. A renamed entity does not cause this:
  Better Thermostat follows a new entity id on its own.

## How to fix it

1. Open **Settings → Devices & services** and find the entity named in
   the issue. Check the device's battery and reconnect it if necessary.
2. If the device was replaced by one with a different entity id, select
   the new entity in the Better Thermostat options.
3. Once the entity is back, confirm the repair issue — it also clears on
   its own when the entity becomes available again.
