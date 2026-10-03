---
title: Hydraulic balance
description: Deep explanation of decentralized hydraulic balancing in Better Thermostat.
---

Better Thermostat can emulate hydraulic balancing behavior per room without requiring a global boiler signal. This runs in the controller calibration modes: MPC Predictive, MPC v2, TPI Controller and PID Controller. The other modes do not compute a valve opening this way.

## Core idea

Per room, BT evaluates:

- Temperature error: target minus current temperature
- Short-term temperature trend
- Window-open state

It derives a control signal to reduce overheating and improve distribution between rooms.

## Output behavior

The controller produces a valve opening in percent. Depending on device capability and calibration type, BT uses it as either:

- Direct valve percentage, with the Direct Valve Based calibration type (if the integration exposes a writable valve position). On a Sonoff TRVZB, BT writes the valve opening degree and, where the device exposes it, sets the closing degree to 100 minus the opening.
- A mapped setpoint or offset otherwise: BT places the setpoint the TRV sees between the TRV's current temperature (0 %) and its maximum temperature (100 %). Depending on the percentage, that setpoint can lie above or below your target.

## Why this helps

- Reduces overshoot near setpoint
- Avoids one strong room dominating heat flow
- Improves comfort consistency across rooms

## Important limitations

- This is software balancing, not a mechanical replacement.
- Quality depends on stable sensors and correct entity setup.
- Behavior differs by TRV firmware and integration capabilities.

For how the controllers compute the valve opening, see [Calibration Algorithms](/calibration_algorithms/) and [Internals: Calibration](/internals/calibration/).
