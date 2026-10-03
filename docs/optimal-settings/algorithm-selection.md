---
title: Algorithm selection
description: Pick the right control algorithm for your room behavior.
---

## Quick decision guide

- AI Time Based: the first choice for most homes.
- External Sensor Offset Only: stable and simple fallback.
- Aggressive: faster warm-up, at the price of more overshoot.
- MPC Predictive (Beta): predictive control, still in testing; moves the valve more than the other modes.
- MPC v2: experimental, for TRVs with direct valve control.
- PID Controller: responsive control with auto-tuning.
- TPI Controller: simple proportional-time behaviour, little overshoot and little valve movement.
- No Calibration: the TRV regulates on its own sensor.

## Decision matrix

| Need | Best mode |
| --- | --- |
| Easiest start | AI Time Based |
| Fastest warm-up | Aggressive |
| Less overshoot | TPI Controller |
| Strong disturbance handling | PID Controller |
| Very simple control model | TPI Controller |

## Advanced note

MPC, MPC v2, PID, TPI and AI Time Based can set the valve opening directly when the device supports direct valve control and the calibration type is Direct Valve Based.

For deeper technical details of balancing behavior and control signals, see [Hydraulic balance](/deep-explanations/hydraulic-balance/).
