---
title: Hydraulic balance
description: Deep explanation of decentralized hydraulic balancing in Better Thermostat.
---

Better Thermostat can emulate hydraulic balancing behavior per room without requiring a global boiler signal.

## Core idea

Per room, BT evaluates:

- Temperature error: target minus current temperature
- Short-term temperature trend
- Window-open state

It derives a control signal to reduce overheating and improve distribution between rooms.

## Output behavior

Depending on device capability, BT uses either:

- Direct valve percentage (if exposed by the integration)
- Effective target throttling (setpoint reduction)

## Why this helps

- Reduces overshoot near setpoint
- Avoids one strong room dominating heat flow
- Improves comfort consistency across rooms

## Important limitations

- This is software balancing, not a mechanical replacement.
- Quality depends on stable sensors and correct entity setup.
- Behavior differs by TRV firmware and integration capabilities.

For implementation-level details, formulas, and tuning notes, see the full design document in the repository:

- https://github.com/KartoffelToby/better_thermostat/blob/master/docs/hydraulic_balance_design.md
