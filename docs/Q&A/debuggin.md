---
layout: default
title: How do I activate the debug mode?
nav_order: 3
description: "BT."
permalink: qanda/debugging
parent: Q&A
---

# How do I activate the debug mode?

Add the following lines to your configuration.yaml file and restart Home Assistant.
```
logger:
  default: warning
  logs:
    custom_components.better_thermostat: debug
```
