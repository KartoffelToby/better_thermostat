---
title: Calibration
description: The two calibration channels, the per-mode traits table, the controllers, and the standby contract.
sidebar:
  order: 5
---

A TRV measures temperature next to its own hot valve, so it throttles
too early. Calibration corrects this using the external room sensor,
through one of two **channels** depending on what the device supports:

- **Local offset** — the device exposes a calibration entity; Better
  Thermostat writes an offset so the TRV's internal reading matches the
  room sensor.
- **Setpoint** — no calibration entity; Better Thermostat shifts the
  *target* it sends so the device's internal logic lands on the real
  room target.

Both channels share one cascade — base value, controller contribution,
per-mode adjustments, direction-aware rounding — and differ only in
direction and reference values.

## The traits table

Everything mode-specific is data, not branches: `MODE_TRAITS` maps each
calibration mode to its traits — whether a controller (balance
strategy) contributes, whether the tolerance band applies, whether
post-adjustments run, and an optional per-mode adjustment hook that
serves both channels through a `ChannelAdjustment` (direction, neutral
reference, hold value, legacy fallback).

| Mode | Controller | Tolerance band | Post-adjustments |
|---|---|---|---|
| Default | — | — | — |
| MPC / TPI / PID | ✓ | ✓ | — |
| Aggressive | — | ✓ | boost hook, no tolerance delay |
| Heating power | — | ✓ | learned-power hook (decides its own skip) |
| No calibration | — | ✓ | plain cascade |

Within the tolerance band (room between target − tolerance and target)
the calibration holds its last value, but a controller keeps its valve
data fresh — so leaving the band resumes from a current model, not a
stale one.

## The controllers

MPC, TPI, and PID are pure functions in `utils/calibration/`:
`compute_*(input, params, state) -> (output, state')`. Each strategy
owns its state; the `StateManager` is the only persistence authority.
The strategy layer (`BalanceStrategy`) wraps each computation behind
the core `Calibrator` contract (observe / actuate / capability /
health) for the eventual move into the core.

## The standby contract

While heating is suppressed (open window, OFF, HOLD), **observe means
tracking, never learning**:

- the *entity-level* estimates (temperature EMA, slope) keep converging
  — sensor events are processed regardless of window state,
- the controllers do not integrate error (no windup on the growing
  error of a cooling room: PID and TPI simply do not step),
- MPC drops its in-flight learning interval — a half-heated interval
  would teach the model that heating does not work — while the learned
  parameters survive,
- re-entry resumes from held controller state plus fresh estimates,
  which is what makes the transfer bumpless.

This contract is pinned as a named test
(`tests/unit/test_standby_contract.py`); a regression in any layer
reads as a standby-contract break, not as an unrelated unit failure.

There is deliberately **no external readiness gate** in front of
actuation: closed-loop learners bootstrap *through* actuation — an
external "only actuate when ready" gate would prevent them from ever
warming up. The controllers gate themselves (standby skips, gap resets,
warmup bootstrapping), and capability/health reporting is annunciation.

## Verifying changes

Calibration is the most behavior-sensitive code in the project. Two
nets pin it: the per-mode unit suites, and the seeded calibration
benchmark — a pure thermal simulation across all controllers and ~37
scenarios whose output is deterministic and therefore diffable. A refactoring of this code is
proven by a byte-identical benchmark before and after.
