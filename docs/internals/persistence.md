---
title: Persistence
description: What survives a restart, what is rebuilt from live data, and how poisoned state is absorbed.
sidebar:
  order: 6
---

Of what Better Thermostat stores itself, only state with learning value
persists. Everything that can be re-derived from live observations is
rebuilt after a restart instead of restored, because a persisted
conclusion could only pin stale knowledge whose inputs are gone. The
user inputs Home Assistant restores on its own sit outside that line.

| Data | Lifecycle | Home |
|---|---|---|
| Configuration (sensors, delays, tolerances) | set once at setup | config entry → `BtConfig` |
| Live operating values (temperatures, computed setpoints, flags) | rebuilt per observation | `BtRuntime` + the regions |
| Controller state (PID/MPC/TPI), thermal stats, filters | learned, persists | `StateManager` (HA Store) |
| PID parameters and the PID auto-tune switch | persists | `StateManager` (HA Store), inside the PID controller state |
| Climate target and HVAC mode | persists | `RestoreEntity` (HA-owned) |
| User inputs on the other helper entities (preset temperatures, valve max opening, child-lock switch) | persists | `RestoreEntity` (HA-owned) |

The discrete mode flags (window open, startup, maintenance, degraded)
live in the kernel's regions and are exposed as derived read-only
properties. They have no second home, and none of the regions is
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

The filters describe the room only for as long as the downtime is short.
The EMA is stored with the wall-clock time of its last update, and the
first live reading after a restart is blended in over the real interval,
so a long stop hands the filter to that reading. The slope comes back
only when the stop was shorter than the EMA's time constant. A filter
stored without that time has an unknown age and is not restored; the
first live reading seeds it.

The PID parameter numbers and the PID auto-tune switch read their value
from the PID controller state in the store and write changes back
through it; they do not restore Home Assistant's last state, so their
values come back once the climate entity has loaded the store.

`RestoreEntity` remains only for data Home Assistant owns itself:
the climate entity's target/mode and the user inputs on the preset,
valve max opening and child-lock helpers. The legacy attribute fallback in the
restore path stays as a migration window for installations that predate
the store. It works per field: each old entity attribute (heating power,
heat loss) is read only when the store holds no value for that field. The
temperature EMA and slope are not read from attributes, since an
attribute carries no time to judge their age by.

## Poison resistance

Persisted state is treated as untrusted input, absorbed at three
layers:

1. **Per field at load:** deserialization skips a wrong-typed field
   individually: it keeps its default and a warning names it. A
   non-finite value, or a stored null where the field's type has no
   `None`, poisons the whole stored entry, which then falls back to its
   defaults; a section or entry of the wrong shape and a
   re-identification result outside its plausible band are discarded
   the same way.
2. **Per store at load:** if deserialization itself breaks on an
   unexpected shape, the store starts fresh with a warning instead of
   killing the startup task, since relearning replaces anything a
   poisoned store could offer.

   The stored payload is copied aside only when load discards stored
   values as a whole: an unreadable store, or an entry or section reset
   by a poisoned value or a wrong shape. A field skipped for its type
   triggers no copy, so its default replaces the stored value on the
   next save. The copy goes into a `.corrupt` store next to the live one
   (up to `QUARANTINE_COPIES`, three copies per config entry, one per
   distinct payload; with all three taken, the newest is replaced).

   The copy is an attempt, not a guarantee. It counts once it loads back
   from disk as the payload. When the write fails or does not read back,
   whatever load discarded still falls back to defaults, the rest of the
   store keeps its loaded values, but the payload is held and every
   save is skipped, so the live store keeps it. The copy is tried again
   before each explicit save and, from the delayed save, after a backoff
   that starts at 60 seconds and doubles up to one hour. While Home
   Assistant stops, the Store only queues writes, so the retry waits for
   the final write, where the entity saves any held-back state after the
   copy is confirmed.
3. **Per cycle at compute:** the sanitize step heals whatever still
   reaches a controller (non-finite state, runaway gains, wound-up
   integrators) and annunciates the verdict as
   [calibrator health](/internals/safety-and-degradation/).
