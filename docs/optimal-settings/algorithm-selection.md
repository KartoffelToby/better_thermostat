---
title: Algorithm selection
description: Pick the right control algorithm for your room behavior.
---

## Quick decision guide

- **AI Time Based**: Best first choice for most homes.
- **Normal**: Stable and simple fallback.
- **Aggressive**: Faster warm-up, potentially more overshoot.
- **MPC Predictive**: Best efficiency and overshoot prevention.
- **PID Controller**: Advanced control with auto-tuning.
- **TPI Controller**: Simple proportional-time behavior.

## Decision matrix

| Need | Best mode |
| --- | --- |
| Easiest start | AI Time Based |
| Fastest warm-up | Aggressive |
| Lowest overshoot | MPC Predictive |
| Strong disturbance handling | PID Controller |
| Very simple control model | TPI Controller |

## Advanced note

MPC and PID benefit strongly from direct valve control capable devices.

For deeper technical details of balancing behavior and control signals, see [Hydraulic balance](/deep-explanations/hydraulic-balance/).
