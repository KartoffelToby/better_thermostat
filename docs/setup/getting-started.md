---
title: Getting started
sidebar: 
    order: 1
description: Install Better Thermostat and create your first room in a few minutes.
---

Installing Better Thermostat and setting up your first room takes a few minutes.

## 1) Install the integration

The easiest way to install Better Thermostat is using HACS (Home Assistant Community Store):

<a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=KartoffelToby&amp;repository=better_thermostat&amp;category=integration"><img alt="Open HACS repository" src="https://my.home-assistant.io/badges/hacs_repository.svg" /></a>

After installation, remember to restart Home Assistant.

## 2) Add Better Thermostat

Go to **Settings** → **Devices & Services** → **Integrations** → **Add Integration** → search for **Better Thermostat**.

Or click this button to start directly:

<a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=better_thermostat" target="_blank"><img src="https://my.home-assistant.io/badges/config_flow_start.svg" alt="Start Better Thermostat configuration flow"/></a>

## 3) Gather your devices

Before setting up a room, make sure you have these devices ready in Home Assistant:

- **A smart radiator valve or thermostat** (required)
- **A separate room temperature sensor** (required: the sensor inside the radiator valve sits too close to the heat source to read the room, and the setup form will not let you finish without one)
- *Optional:* A window sensor (to automatically pause heating when you air out the room)
- *Optional:* A weather integration or outdoor temperature sensor (to stop heating when it's warm outside)

## 4) Follow the configuration walkthrough

The [Configuration walkthrough](/setup/configuration-walkthrough/) goes through every option in the setup form.

## 5) Apply recommended defaults

If you are unsure what to pick, start from [Recommended settings](/optimal-settings/recommended-settings/) and adjust from there.
