---
title: Thermostat in more than one Better Thermostat
description: What the "controlled by more than one Better Thermostat" repair issue means and how to fix it.
slug: faq/shared-trv
---

Better Thermostat raises this repair issue when one TRV belongs to two or more Better Thermostat devices. The issue names the TRV and the Better Thermostats that control it.

## Why it is a problem

Each Better Thermostat regulates its own room and writes its own target temperature, HVAC mode, offset or valve opening to its TRVs. A TRV that belongs to two of them gets two sets of writes. The two overwrite each other, so the TRV never settles on what either of them wants.

## How it happens

The setup and the settings refuse a TRV that another Better Thermostat already controls. Two Better Thermostats can only share a TRV when they were set up before Better Thermostat checked for this. They keep running, and the issue is raised every time one of them starts.

## How to fix it

1. Decide which Better Thermostat the TRV belongs to. Usually that is the one for the room the radiator is in.
2. For every other Better Thermostat named in the issue, open **Settings → Devices & services → Better Thermostat**, choose **Configure** on that entry and remove the TRV from **The real thermostat**. If it was that entry's only TRV, delete the entry instead (see [Removing Better Thermostat](/setup/removing/)).
3. Save. The entry reloads and the repair issue clears itself once only one Better Thermostat controls the TRV.

If you want one thermostat for several TRVs, put all of them into the same Better Thermostat. It controls them together, with one target temperature for the room.
