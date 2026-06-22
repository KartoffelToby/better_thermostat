---
title: Debugging
description: Download diagnostics, enable debug logs, and verify Better Thermostat behavior.
---

## Download diagnostics

For bug reports, the diagnostics download is the most useful artifact —
attach it before reaching for debug logs.

Go to **Settings → Devices & services → Better Thermostat**, open the
three-dot menu of the entry, and choose **Download diagnostics**. The
file contains:

- the configuration of the entry and the state of every configured TRV
  and sensor,
- the **flight recorder**: the last control decisions, each as the
  observation, the controller state, and the resulting intent. It shows
  *why* Better Thermostat did what it did, which a log line usually
  cannot (see [Internals: Observability](/internals/observability-and-testing/)
  for how it works).

## Enable debug logging via configuration.yaml

```yaml
logger:
  default: warning
  logs:
    custom_components.better_thermostat: debug
```

Restart Home Assistant after editing `configuration.yaml`.

## Enable debug logging without restart

Use the Home Assistant action `logger.set_level`:

```yaml
action: logger.set_level
data:
  custom_components.better_thermostat: debug
```

Open **Developer tools → Actions** directly:

<a href="https://my.home-assistant.io/redirect/developer_services/" target="_blank"><img src="https://my.home-assistant.io/badges/developer_services.svg" alt="Open Developer tools → Actions" /></a>

## What to check first

- External sensor updates are fresh and stable.
- Window sensor states are correct.
- The right algorithm is selected for the room.
- TRV supports the chosen calibration type.
