---
title: Design decisions
description: The structural decisions and the chosen constants — what anchors each value, and what would change it.
sidebar:
  order: 8
---

Two kinds of decisions live in this codebase, and they deserve
different scrutiny. **Anchored thresholds** derive from a physical or
device-given limit — challenging them means challenging the anchor.
**Chosen constants** are engineering trade-offs between two named
pressures — they are legitimate to revisit, and this page names the
pressures so a revisit argues against the right thing.

## Structural decisions

**The snapshot is pulled, not maintained.** A per-cycle observation
guarantees every decision sees one coherent world and makes replay
deterministic. An event-maintained snapshot would buy nothing (the
write path is serialized anyway) and cost torn reads.

**Regions are not persisted.** Window, maintenance, lifecycle, mode,
ladder, reachability all re-derive from live observations within one
debounce window. A persisted region could only pin stale conclusions
whose inputs are gone after a restart. Only state with learning value
(controller models, thermal stats, filters) persists.

**Standby is tracking, never learning.** Entity-level estimates keep
converging while heating is suppressed; controllers neither integrate
error (windup) nor learn parameters from disturbance-flagged samples
(model poisoning: a window-open interval would teach the model that
heating does not work). Re-entry from held controller state plus fresh
estimates is what makes the transfer bumpless.

**Self-healing only for unambiguous pathologies; oscillation is
annunciation-only.** Non-finite state, runaway gains, and wound-up
integrators have crisp definitions and safe resets. An oscillation
detector with an automatic gain backoff would thrash the controller on
a false positive — worse than the oscillation it reacts to. Backoff
stays manual until the detector is validated against the benchmark.

**No external readiness gate in front of actuation.** Closed-loop
learners bootstrap *through* actuation; an external "only actuate when
ready" gate would prevent them from ever warming up. The controllers
gate themselves (standby skips, gap resets), and capability/health is
annunciation.

**Reachability is diagnosis, not control law.** In Home Assistant,
availability is push-based: writing to an unavailable entity does
nothing, and the device's return triggers events that resume control.
A write-backoff consumer would model a polling world that does not
exist here; flapping churn is already bounded by the write budget. The
region's value is the flight-recorder trail during outage analysis.

**HOLD keeps the last commanded state instead of handing over to the
TRV.** With no usable temperature anywhere, both options are blind;
keeping the last commanded state is the predictable one, and the frost
floor stays enforced. For a *single* dead TRV the hand-over happens
naturally: it receives no intent and its native thermostat continues.

**`unknown`/`unavailable` window readings count as open.** With no
trustworthy reading, not heating out of the window is the cheap-failure
direction; the reverse error heats the street.

## Chosen constants

| Value | What | The two pressures | Anchor / revisit trigger |
|---|---|---|---|
| **30 s** | write budget per TRV and channel | battery & radio load ↔ worst-case latency of a fine-tuning write | TRVs are battery devices on contended radio; write bursts are a real failure cause. 30 s caps a channel at ~120 writes/h while keeping deferred writes promptly delivered (safety writes bypass entirely). Faster budgets mainly buy more radio traffic, not better control — room dynamics are far slower. |
| **5 min** | calibration tick (controller modes) | control freshness without events ↔ pointless wake-ups | Room thermal time constants are tens of minutes; 5 min samples the plant several times per time constant. A faster tick could not act faster anyway — actuation is bounded by the 30 s budget. |
| **5 min** | reconciler tick | healing latency for lost writes ↔ cost of observe+decide per tick | Lost writes are rare and not safety-relevant (those bypass and confirm); healing within minutes suffices. |
| **2 min / 5 min** | ladder down-debounce / up-stability | reacting to real outages ↔ flapping on sensor blips | Asymmetric on purpose: degrade quickly enough to matter, re-promote only after sustained recovery — classic reversionary-mode hysteresis. |
| **15 min** | watchdog stall threshold | catching a silent hang ↔ false alarms | Anchored: the 5-minute ticks guarantee a cycle at least every 5 minutes, so 15 minutes = three missed ticks = a real hang, not jitter. |
| **1 h** | maintenance max runtime | letting a slow valve exercise finish ↔ a dead run blocking control forever | A valve exercise takes minutes per TRV; an hour means the run died. The bound exists so maintenance can never block control permanently. |
| **3 s** | post-write propagation wait | reading the device echo as confirmation ↔ misreading it as an external change | Typical Zigbee/MQTT echo latency; without the wait, BT would treat its own write's echo as a user action. |
| **0.05 K + half device step** | reconcile setpoint tolerance | detecting lost writes ↔ fighting device quantization | Anchored: a device snapping a value onto its own grid moves it at most half a step; below that is float noise. Fighting quantization would re-send every 5 minutes and drain batteries. |
| **5 points** | reconcile valve tolerance | detecting lost valve writes ↔ fighting device-side modulation | Real lost writes look like 0 vs 80, not 77 vs 80. |
| **4 reversals / ≥20-point swings / 10 samples** | oscillation detector | catching thrash ↔ false positives | Deliberately biased toward quiet (annunciation-only makes a miss cheap and a false alarm noisy). To be tightened against benchmark data. |
| **×10 band around default gains** | runaway-gain reset (PID auto-tune) | never clipping a legitimate tune ↔ catching divergence | Legitimate room dynamics vary roughly 3–5×; a 10× band never bites a real tune. Deliberately generous — tighten once benchmark/telemetry data justifies it. |
| **5 min** | startup degraded-mode grace | alarming on real outages ↔ alarming on slow cloud integrations at boot | Weather/cloud entities routinely need minutes to come online after a restart. |
| **50 entries** | flight-recorder ring | covering the window around an incident ↔ diagnostics download size | With background ticks every 5 minutes, 50 tuples span hours around the moment a user hits "download diagnostics". |

When one of these constants changes, the table entry is the review
checklist: name which pressure moved and what evidence moved it.
