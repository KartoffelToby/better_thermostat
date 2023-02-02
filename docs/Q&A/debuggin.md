---
layout: default
title: How do I activate the debug mode?
nav_order: 3
description: "BT."
permalink: qanda/debugging
parent: Q&A
---

# How do I activate the debug mode?
Basically there are two options to enable debug mode.

## Via configuration.yaml
Add the following lines to your configuration.yaml file and restart Home Assistant.
```yaml
logger:
  default: warning
  logs:
    custom_components.better_thermostat: debug
```

## Via services
  [![Open your Home Assistant instance and show your service developer tools.](https://my.home-assistant.io/badges/developer_services.svg)](https://my.home-assistant.io/redirect/developer_services/)

Another option is to use the service `logger.set_level`. Go to Services under Developer Tools, switch to YAML mode and enter the following YAML.

```yaml
service: logger.set_level
data:
  custom_components.better_thermostat: debug
```
Click `Call Service`, no restart of Home Assistant required.
