---
title: Observability and testing
description: The flight recorder, the diagnostics download, the annunciation attributes, and the test strategy.
sidebar:
  order: 7
---

## Mode annunciation

Degradation is never silent. The climate entity exposes:

- `control_mode` — the fail-soft rung (`optimal`, `sensor_fallback`,
  `hold`),
- `degraded_for_s` and `unavailable_sensors` — what is degraded, since
  when,
- `calibrator_health` — per TRV, whether its calibration controller is
  healthy, has self-healed, or shows oscillating output.

Entering degraded mode raises a repair issue (suppressed during the
five-minute startup grace window so slow cloud integrations do not
alarm) which clears itself on recovery.

## The flight recorder

Because `decide()` is pure, recording the tuple
`(snapshot, pre-decide state, desired)` per control cycle is enough to
reproduce any decision offline. `core/recorder.py` keeps a bounded ring
of the last 50 decision tuples; the **diagnostics download**
(Settings → Devices & services → Better Thermostat → Download
diagnostics) exports it alongside the configuration and device states.

`replay()` feeds an exported tuple back through the kernel and compares
— a bug report's diagnostics file answers *why* Better Thermostat did
what it did, deterministically, on the developer's machine. A
completeness test forces every new field of the recorded types through
the export/reconstruct round-trip, so the replay side cannot silently
drift from the dataclasses.

## Test strategy

Four nets with distinct failure modes they catch:

1. **Pure unit tests** (`tests/unit/`, the bulk) — the core is HA-free,
   so the kernel, the regions, the safety hull, and the recorder are
   tested without mocks; the shell is tested against `MagicMock`
   entities. The core sits at 100% coverage.
2. **Integration tests** (`tests/integration/`) — a real config entry
   against a real (simulated) climate entity in a real Home Assistant
   instance. They exist because a control path that silently writes
   nothing keeps every unit test green: the assertions are the service
   calls that arrive at the device. Covered end to end: startup sync,
   window → OFF, restore after restart, unload/reload, and the
   reconciler healing a dropped write.
3. **The calibration benchmark** (`tests/benchmark/`) — a pure thermal
   simulation comparing all controllers across ~37 scenarios. Seeded
   and deterministic, so two runs are byte-identical: refactorings of
   the calibration code are proven behavior-preserving by diffing the
   benchmark output before and after.
4. **Golden replays** (`tests/fixtures/replay_corpus/`) — one committed
   decision tuple per kernel tier, pinned byte-stable. An intentional
   kernel change regenerates them (`BT_REGEN_GOLDENS=1`) and the diff
   shows exactly which decisions changed.

Named contracts deserve named tests: the standby contract, the
no-raw-dict-access guard, and the controller state-threading contract
each live in their own file, so a regression reads as a broken contract
rather than an incidental unit failure.
