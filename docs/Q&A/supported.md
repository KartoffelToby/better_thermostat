---
layout: default
title: Which devices are currently supported?
nav_order: 3
description: "BT."
permalink: qanda/supported
parent: Q&A
---

# Which devices are currently supported?

Generally speaking, all devices supported by Home Assistant as "climate  entities" are supported. 
However, if you would like to use the "local calibration mode", Better Thermostat needs to support the thermostat integration of your device and your device needs to support this feature.
Currently, these are the integrations compatible with local calibration mode:

- Tado
- Zigbee2MQTT
- Deconz

Please keep in mind that even if BT supports your integration, if your device does not support "local_temperature_calibration" this feature will not be available to you. You can check your device compatibility via Zigbee2MQTT.
If your preferred integration isn’t currently available for local calibration please open a GitHub issue.
