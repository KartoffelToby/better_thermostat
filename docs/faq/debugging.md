---
title: Debugging
description: Enable debug logs and verify Better Thermostat behavior.
---

## Enable debug logging via configuration.yaml

```yaml
logger:
  default: warning
  logs:
    custom_components.better_thermostat: debug
```

Restart Home Assistant after editing `configuration.yaml`.

## Enable debug logging without restart

Use Home Assistant service `logger.set_level`:

```yaml
service: logger.set_level
data:
  custom_components.better_thermostat: debug
```

Open Services directly:

<a href="https://my.home-assistant.io/redirect/developer_services/" target="_blank"><img src="https://my.home-assistant.io/badges/developer_services.svg" alt="Open developer services" /></a>

## What to check first

- External sensor updates are fresh and stable.
- Window sensor states are correct.
- The right algorithm is selected for the room.
- TRV supports the chosen calibration type.
