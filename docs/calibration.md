---
layout: default
title: Calibration types
nav_order: 2
description: "BT."
permalink: calibration
---

# Calibrations

## TYPE: target_temp_based (Target Temperature)

This is the default calibration type. It uses the target temperature to calculate the calibration value. The calibration value is calculated as follows:

```python
trv_target_temp = target_set_temp - external_sensor_temp + internal_trv_temp
```
So its totaly normal that the TRV displays a differen target temperature than the target temperature set in Home Assistant on Better Thermostat.

## TYPE: local_calibration_based (Local Calibration)

uses the local calibration or offset attr from the TRV to equalise the TRV internel temperature sensor to the external temperature sensor.
sadly this is not easy because every climate integration has a different way to set the offset. so we need to find a way to make this work for all climate integrations. every integration has a own "adapter" to make this work.
currently we have the following adapters:

  * zigbee2mqtt