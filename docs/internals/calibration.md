---
title: Calibration
description: The calibration channels (offset, setpoint, direct valve), the per-mode traits table, the controllers, and the standby contract.
sidebar:
  order: 5
---

A TRV measures temperature next to its own hot valve, so it throttles
too early. Calibration corrects this using the external room sensor,
through one of three **channels** depending on what the device supports:

- **Direct valve** — when `Calibration type` is `DIRECT_VALVE_BASED`, the
  device exposes a writable valve channel, and the configured mode is in
  `VALVE_CALIBRATION_MODES` (or `Boost` is active), Better Thermostat writes
  the `0–100 %` valve opening (capped by `Valve Max Opening`) directly.
- **Local offset** — the device exposes a calibration entity; Better
  Thermostat writes an offset so the TRV's internal reading matches the
  room sensor.
- **Setpoint** — no calibration or valve entity; Better Thermostat shifts the
  *target* it sends so the device's internal logic lands on the real
  room target.

The offset and setpoint channels share one cascade (base value, controller
contribution, per-mode adjustments, direction-aware rounding) and differ only
in direction and reference values.

## The traits table

Mode-specific behavior lives in data rather than branches.
`MODE_TRAITS` maps each calibration mode to its traits: whether a
controller (balance strategy) contributes, whether the tolerance band
applies, whether post-adjustments run, and an optional per-mode
adjustment hook that serves both channels through a `ChannelAdjustment`
(direction, neutral reference, hold value, legacy fallback).

| Mode | Controller | Tolerance band | Post-adjustments |
|---|---|---|---|
| Default | — | — | — |
| MPC / MPC v2 / TPI / PID | ✓ | ✓ | — |
| Aggressive | — | ✓ | boost hook, no tolerance delay |
| Heating power | — | ✓ | learned-power hook (decides its own skip) |
| No calibration | — | ✓ | plain cascade |

Within the tolerance band (room between target − tolerance and target)
the calibration holds its last value, but a controller keeps its valve
data fresh, so leaving the band resumes from a current model instead of
a stale one.

## The controllers

MPC, MPC v2, TPI, and PID are state-threading helpers in
`utils/calibration/` (`compute_mpc`, `compute_mpc_v2`, `compute_tpi`,
`compute_pid`). Each takes the measurements, its parameters and the
caller-owned controller state, and returns its output together with
that state; PID also returns a debug record, as
`(percent, PIDDebugInfo, PIDState)`. The clock is their one other
input: TPI, PID and MPC v2 read it unless the caller passes `now`, and
MPC always reads `time()`, so two identical calls repeat exactly only
for TPI, PID and MPC v2 with the same `now`. The returned state is the object
passed in, mutated in place, and standby paths such as PID's
`observe_standby()` update it the same way. MPC, TPI and PID require a
state; `compute_mpc_v2` also accepts `state=None`, in which case it
creates a fresh `MpcV2State` and returns that new object, so the
identity guarantee holds only when the caller supplies a state. Each strategy owns its
state; the `StateManager` is the only persistence authority.

`BalanceStrategy` pairs a mode's computation with its result accessor.
`BalanceCalibrator` adapts a strategy to the core `Calibrator` protocol
(`observe` / `is_ready` / `actuate`) and also reports `capability` and
`health`, the two methods of the `AnnunciatingCalibrator` extension.

## The standby contract

While heating is suppressed (open window, OFF, HOLD), **observe means
tracking, never learning**:

- the *entity-level* estimates (temperature EMA, slope) keep converging,
  since sensor events are processed regardless of window state,
- the controllers do not integrate error (no windup on the growing
  error of a cooling room: PID and TPI simply do not step),
- MPC drops its in-flight learning interval (a half-heated interval
  would teach the model that heating does not work) while the learned
  parameters survive,
- re-entry resumes from held controller state plus fresh estimates,
  which is what makes the transfer bumpless.

This contract is pinned as a named test
(`tests/unit/test_standby_contract.py`), so a regression in any layer
reads as a standby-contract break rather than an unrelated unit failure.

## Paused learning

The *Pause Learning* switch sets `learning_paused` on the entity, which
publishes it as a state attribute and restores it from its own saved
state during startup. The learners read `learning_held` instead: it turns
on with the pause and off at the end of the first control cycle after
it, which the switch requests when it turns off. That cycle restarts
every learning interval, so none spans the pause. Every learner receives
the flag the way it receives its other context: the heating-power and
heat-loss trackers as an `update()` argument, MPC and MPC v2 in their
input dataclasses, PID as a `compute_pid()` argument. Unlike standby, a
paused controller keeps actuating on the values it holds:

- the trackers abort the cycle in progress and start none,
- PID holds its integral and gains and restarts the auto-tune context,
- MPC skips adaptation, forced loss calibration and dead-zone, profile
  and performance-curve evidence, and restarts its learning interval
  every cycle,
- MPC v2 holds the disturbance estimate and the error integral, records
  one paused sample per pause that cuts the re-identification buffer,
  starts no fit and discards one that completes during the pause.

State estimates (the EMA, the slope, the MPC virtual temperature, the
MPC v2 observer, the PID measurement chain) keep tracking the room, so
learning resumes from fresh estimates and no interval spans the pause.

The *Release devices* switch holds the learners through the same flag.
`learning_held` is on while learning is paused, the devices are released
or any TRV is released on its own, and turns off at the end of the first
control cycle after all of them have ended. For a release that is the cycle the take-back requests, the
one that writes the devices once, so neither PID, MPC nor MPC v2 learns
over the released time and the re-identification buffer is cut there.
`learning_paused` stays the pause switch's own state; a release does not
set it.

There is **no external readiness gate** in front of actuation:
closed-loop learners bootstrap *through* actuation, and an external
"only actuate when ready" gate would prevent them from ever warming
up. The controllers gate themselves (standby skips, gap resets,
warmup bootstrapping), and capability/health reporting is annunciation.

## Verifying changes

Calibration is the most behavior-sensitive code in the project. Two
nets pin it: the per-mode unit suites, and the seeded calibration
benchmark, a pure thermal simulation across all controllers and ~37
scenarios whose output is deterministic and therefore diffable. A
refactoring of this code is proven by a byte-identical benchmark before
and after.
