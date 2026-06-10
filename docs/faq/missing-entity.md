---
title: Missing entity
description: What the "related entity is missing" repair issue means and how to fix it.
slug: qanda/missing_entity
---

Better Thermostat raises a **missing entity** repair issue when one of
the entities it was configured with — a TRV, the room temperature
sensor, a window sensor, or another configured device — is not available
in Home Assistant.

## Common causes

- The device's battery is empty or the device lost its radio connection.
- The integration providing the entity is not loaded or failed to start.
- The entity was renamed or removed, so the entity id Better Thermostat
  was configured with no longer exists.

## How to fix it

1. Open **Settings → Devices & services** and find the entity named in
   the issue. Check the device's battery and reconnect it if necessary.
2. If the entity id changed, either rename it back or update the Better
   Thermostat configuration to the new entity id (open the Better
   Thermostat entry and reconfigure it).
3. Once the entity is back, confirm the repair issue — it also clears on
   its own when the entity becomes available again.
