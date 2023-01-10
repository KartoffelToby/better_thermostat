---
layout: default
title: Configuration
nav_order: 2
has_children: false
permalink: configuration
---

# Create a new Better Thermostat device

** Goto: `Settings` -> `Devices & Services` -> `Integrations` -> `+ Add Integration` -> `Better Thermostat` **

or just click on the button below:

<a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=better_thermostat" target="_blank"><img src="https://my.home-assistant.io/badges/config_flow_start.svg" alt="Open your Home Assistant instance and start setting up a new integration." /></a>


# Configuration

## First step

![first step](../../assets/config_1.png)

**Name** This is a required field. It is the name of the virtual climate. It is used to as a entity key name.

**The real thermostat** This is a required field. This is the real climate entity you want to control with BT, if you have more than one climate in your room you can select multiple climate entities, fillout the first filed and a second one will appear.

**Temperature sensor** This is a required field. This is the temperature sensor you want to use to control the real climate entity. It's used to get a more accurate temperature reading as it with the sensor in the real climate entity. Because you can place it in the middel of the room and not close to the radiator.

**Humidity sensor** This is an optional field. For now the Humidity is only used to display it in the UI. In the future it will be used make a better calculation of the temperature or set it up to a called feels like temperature.

**If you have a outdoor sensor...** This is an optional field. If you have a outdoor sensor you can use it to get the outdoor temperatures, this is used to set the thermostat on or off, if the threshold (last option in this screen) is reached. it uses a mean of the last 3 days and checks it every morning at 5:00 AM.

**Window Sensor** This is an optional field. If you have a window sensor you can use it to turn of the thermostat if the window is open and turn it on again when the window is closed. If you have more than one window in a room, you can also select window groups (see the github page for more info).

**Your weather entity for outdoor temperature** This is an optional field. And shoud be empty if you have a outdoor sensor. This is the weather entity you want to use to get the outdoor temperature. It uses the mean of the last 3 days and checks it every morning at 5:00 AM.

**Window delay** This is an optional field. If you didn't want to turn of the thermostat instantly when the window is open, you can set a delay. This goes in both directions, so if you want to turn it on again after the window is closed, you can set a delay here too.

**The outdoor temperature threshold** This is an optional field. If you have a outdoor sensor or a weather entity, you can set a threshold. If the outdoor temperature is higher than the threshold, the thermostat will be turned off. If the outdoor temperature is lower than the threshold, the thermostat will be turned on. If you don't have a outdoor sensor or a weather entity, this field will be ignored.

## Second step

![second step](../../assets/config_2.png)

**Calibration Type** This is a required field. How the calibration should be applied on the TRV (Target temp or offset)

***Target Temperature Based***: Apply the calibration to the target temperature.

***Offset Based***: Apply the calibration to the offset.



**Calibration Mode**  This is a required field. The kind how the calibration should be calculated

***Normal***: In this mode the TRV internal temperature sensor is fixed by the external temperature sensor.

***Aggresive***: In this mode the TRV internal temperature sensor is fixed by the external temperature sensor but set much lower/higher to get a quicker boost.

***AI Time Based***: In this mode the TRV internal temperature sensor is fixed by the external temperature sensor, but the value is calculated by a custom algorithm to improve the TRV internal algorithm.


**Overheating protection** This shoud be only checked if you have any problems with a strong overheating

**If your TRV can't handle the off mode, you can enable this to use target temp insted** If your TRV model haven't a off mode, BT will use the min target temp of this device instead, this option is only needed if you have problems, known models that haven't a off mode are auto detected by BT.

**If auto means heat for your TRV and you want to swap it** Some climates in HA used the mode auto for default heating, and a boost when mode is heat, this isn't what we want, so if this is the case for you, check this option

**Ignore all inputs on the TRV like a childlock** If this option is enabled, all changes on the real TRV even over HA will be ignored or reverted, only input on the BT Entity are accpeted.

**If you use HomaticIP you should check this...** If your entity is a HomaticIP entity this option should be enabled, to prevent a duty cirlce overload