---
title: Recommended settings
description: Practical defaults that work for most users.
---

Use this profile as your starting point for each room.

## Baseline profile

- **Calibration type**: Target temperature based (or offset based if your TRV supports it reliably)
- **Calibration mode**: AI Time Based
- **Tolerance**: 0.3°C
- **Window delay**: 2-5 minutes
- **Overheating protection**: Off by default; enable only if needed

## When to change it

Switch to Aggressive if the room heats very slowly, to MPC Predictive if it overshoots the target regularly, and to PID if it faces strong disturbances such as sun, draughts or an often-opened door.

## Sensor placement

Put the room sensor away from the radiator and out of direct sun, clear of draughts and exterior doors. Then leave it where it is for a few days: the learning modes treat a moved sensor as a change in the room.

## What to expect

AI Time Based needs a short learning phase before its results settle. MPC and PID both depend on clean, regular sensor updates, so an infrequently reporting sensor costs them more than it costs the simpler modes. Change one tuning value at a time, or you will not know which one did what.
