---
title: Calibration offset at its limit
description: What the "calibration offset stays at its limit" repair issue means and how to fix it.
slug: faq/calibration-offset-at-limit
---

Better Thermostat raises this repair issue when a TRV that is calibrated
with *Offset Based* keeps its calibration offset at the limit of the range
the TRV accepts, for example at 12.7 on a TRV whose offset runs from -12.7
to 12.7, and the offset is still not enough. The issue names the TRV.

## When it is raised

On every control cycle, Offset Based calibration takes the gap between the
room temperature and the TRV's own reading and adds it to the offset the
TRV holds. A TRV that applies the offset reports a reading that moves with
it, the gap closes and the offset settles. A TRV that does not apply it
keeps reporting the same reading, so each cycle adds the same gap again and
the offset climbs until the TRV's range stops it.

An offset can reach the limit without anything being wrong. It does so for
a while when a cold room heats up. Some calibration modes, and overheating
protection, also drive it to a limit on purpose to open or close the valve.
In those cases the TRV's reading carries the offset, and the gap to the
room asks for no correction beyond the range.

The issue is raised only when, on every control cycle for an hour:

- the offset Better Thermostat asks for and the offset the TRV reports are
  both within one offset step of the same limit, and
- the gap between the room temperature and the TRV's reading asks for an
  offset more than one step beyond that limit.

A cycle that does not observe all of this starts the hour again: for
example a cycle while the room is off, while the TRV is unreachable or
released, or while its offset entity reports nothing. A restart of Home
Assistant starts it again too.

Better Thermostat does not change what it writes because of this issue. It
keeps calibrating the offset as before.

## Common causes

- The TRV accepts the offset but does not apply it, so its reading never
  moves. Some Matter thermostats behave like this.
- The TRV applies offsets only within a narrower range than the one it
  publishes to Home Assistant, and ignores larger ones.
- The TRV's own reading is further from the room temperature than its
  offset range reaches, for example because it sits in a niche right above
  the radiator.

## How to fix it

Switch the TRV to the calibration type ***Target Temperature Based***:
open Settings → Devices & services → Better Thermostat, choose
**Configure** on this thermostat and change the calibration type of the
TRV. Target Temperature Based calibration adjusts the target temperature
sent to the TRV and does not depend on the offset.

## When it clears

The issue clears on its own as soon as a control cycle finds the offset off
the limit, or finds that the gap no longer asks for more than the range
holds. While the room is off, no cycle looks at the offset, and the issue
stays. It also goes when the TRV no longer uses Offset Based calibration,
and with every restart of Home Assistant or reload of the thermostat; if the
TRV still does not apply its offset, it comes back an hour later.
