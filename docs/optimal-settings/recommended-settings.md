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

## When to change this profile

- Room heats very slowly → switch to **Aggressive**.
- Room frequently overshoots target → switch to **MPC Predictive**.
- Room has strong disturbances (sun, drafts, doors) → try **PID**.

## Sensor placement tips

- Place room sensor away from radiator and direct sun.
- Avoid drafts and exterior doors.
- Keep sensor position stable for at least a few days while learning.

## Expectations after setup

- AI Time Based needs a short learning phase.
- MPC and PID perform best with clean, regular sensor updates.
- Avoid changing multiple tuning values at once.
