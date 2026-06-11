---
title: Persistence
description: What survives a restart, what is rebuilt from live data, and how poisoned state is absorbed.
sidebar:
  order: 6
---

The dividing line: **only state with learning value persists.**
Everything that can be re-derived from live observations is rebuilt
after a restart instead of restored — a persisted conclusion could only
pin stale knowledge whose inputs are gone.

| Data | Lifecycle | Home |
|---|---|---|
| Configuration (sensors, delays, tolerances) | set once at setup | config entry → `BtConfig` |
| Live operating values (temperatures, targets, flags) | rebuilt per observation | `BtRuntime` + the regions |
| Controller state (PID/MPC/TPI), thermal stats, filters | learned, persists | `StateManager` (HA Store) |
| User inputs on helper entities (presets) | persists | `RestoreEntity` (genuinely HA-owned) |

The discrete mode flags (window open, startup, maintenance, degraded)
live in the kernel's regions and are exposed as derived read-only
properties — they have no second home, and none of the regions is
persisted: lifecycle re-derives through the startup sequence,
window/maintenance/mode from the first events, the ladder within one
debounce window.

## One persistence axis

The `StateManager` is the single authority for learned state: one HA
Store per config entry, holding the per-key controller states
(PID/MPC/TPI), the thermal stats (heating power, heat loss), and the
runtime filters (temperature EMA, slope). The entity pushes its held
values into the store through one seam before every debounced save,
and hydrates from it at startup.

`RestoreEntity` remains only for data Home Assistant genuinely owns:
the climate entity's target/mode and the helper number entities' user
inputs. The legacy attribute fallback in the restore path stays as a
migration window for installations that predate the store — it reads
old entity attributes only when the store has nothing.

## Poison resistance

Persisted state is treated as untrusted input, absorbed at three
layers:

1. **Per field at load:** deserialization skips wrong-typed and
   non-finite values field by field; a corrupt section yields that
   section's defaults.
2. **Per store at load:** if deserialization itself breaks on an
   unexpected shape, the store starts fresh with a warning instead of
   killing the startup task — relearning replaces anything a poisoned
   store could offer.
3. **Per cycle at compute:** the sanitize step heals whatever still
   reaches a controller (non-finite state, runaway gains, wound-up
   integrators) and annunciates the verdict as
   [calibrator health](/internals/safety-and-degradation/).
