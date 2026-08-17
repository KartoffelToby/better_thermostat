---
title: Algorithm selection
description: Pick the right control algorithm for your room behavior.
---

## Quick decision guide

- AI Time Based: the first choice for most homes.
- Normal: stable and simple fallback.
- Aggressive: faster warm-up, at the price of more overshoot.
- MPC Predictive: aims at a steady arrival rather than a fast one.
- PID Controller: responsive control with auto-tuning.
- TPI Controller: simple proportional-time behaviour.

## Decision matrix

| Need | Best mode |
| --- | --- |
| Easiest start | AI Time Based |
| Fastest warm-up | Aggressive |
| Lowest overshoot | MPC Predictive |
| Strong disturbance handling | PID Controller |
| Very simple control model | TPI Controller |

## Advanced note

MPC and PID both gain a lot from a device that supports direct valve control.

For deeper technical details of balancing behavior and control signals, see [Hydraulic balance](/deep-explanations/hydraulic-balance/).
