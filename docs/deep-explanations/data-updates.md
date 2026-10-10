---
title: Where the values come from
description: Which entities Better Thermostat reads, when its values change and what runs on a timer.
---

Better Thermostat has no connection to any device of its own. Everything it knows, it reads from the entities you selected in the setup: the TRVs, the room temperature sensor and the optional sensors. Everything it changes, it changes through those entities. How fresh a value is therefore depends first on how often the integration behind the entity updates it. A Zigbee TRV that reports its temperature every ten minutes gives Better Thermostat a new TRV temperature every ten minutes, no matter what Better Thermostat does.

## Data updates from your entities

Better Thermostat does not poll. It listens for state changes of the configured entities in Home Assistant and reacts when one arrives.

| Entity | What Better Thermostat reads | When |
| --- | --- | --- |
| TRV (`climate`) | Its temperature, target temperature, HVAC mode and, where the TRV has them, its valve position and calibration offset | Every state change |
| Room temperature sensor | The room temperature | Every state change, through the filter described below |
| Humidity sensor | The humidity, shown on the thermostat and available to device triggers; the control does not use it | Every state change |
| Window and door sensors | Open or closed | Every state change, then after the open and close delays you configured |
| Outdoor temperature sensor | A damped outdoor temperature: each reading counts for as long as it was current, until the next one arrives, and older readings fade out exponentially with a time constant of 24 hours instead of dropping out after a day. The first check that finds the sensor available fills it from the last 72 hours of the sensor's recorder history when the recorder holds any; without history it starts at the current reading. See [Summer mode](/deep-explanations/summer-mode/) | Every state change adds the reading. The summer-mode decision is checked at startup, on every state change, once an hour and every day at 5:00. If reading the history fails, it is tried again at the first check that comes at least 15 minutes later; until then the live readings alone are damped |
| Weather entity | The forecast for roughly the next two days, and the current temperature, damped like the outdoor sensor's readings | At startup and once an hour |
| Cooler (`climate`) | Its mode and target temperature | Every state change |

### The room temperature filter

A room sensor that reports small jumps back and forth would otherwise make the TRVs move with every reading. Better Thermostat therefore takes a new room temperature when one of these holds:

- it is the first reading after startup,
- it differs from the current value by at least 0.11 °C, and the last accepted reading is more than 5 seconds old,
- smaller changes in the same direction add up to at least 0.11 °C; a reading back on the current value starts the count again, so a sensor flickering between two neighbouring values is not taken,
- a smaller change has stayed the same for two minutes.

A reading that comes too soon is not lost: it is taken as soon as the 5 seconds are over. A reading outside −50 °C to 60 °C, or one that is not a number, is ignored and raises the [invalid external temperature](/faq/invalid-external-temperature) repair issue. `unknown` and `unavailable` are not readings; they count as a missing sensor (see [Degraded mode](/faq/degraded-mode)).

### Changes made on the TRV

When a TRV reports a new target temperature that Better Thermostat did not send, Better Thermostat takes it as your new target for the whole room. It does this while a window or door is open too: the heating stays paused, and the room heats to the new target once every window and door is closed. It does not do this while the child lock option is on or while the TRV is off, and a TRV that Better Thermostat switched off for an open window or door is off. Values Better Thermostat wrote itself and that the TRV reports back are recognised and not taken as your input.

### Changes made on the cooler

A new target temperature that the cooler reports while it stays in cooling mode (`cool`, or the upper bound in `heat_cool`) becomes your new cooling target. Better Thermostat ignores a target temperature that the cooler reports while it is off or in the same update that changes its mode: many integrations show a placeholder for an air conditioner that is off. Tado, for example, shows 5 °C. A cooler can also hold a target on a coarser grid than it reports, for example 22 °C after Better Thermostat sent 22.5 °C. Its first report within 0.5 °C of the value Better Thermostat sent counts as that answer and not as your input, even when it arrives later from a poll or while Better Thermostat is still sending. A change on the cooler after that answer is your input again, and so is a change you make after your own last change on the cooler.

While the cooling target is still unknown, at the first start or after the cooler was unavailable, Better Thermostat takes it from the cooler when the cooler reports `cool` or `heat_cool`. In any other mode, `off` included, it takes the cooling temperature of the active preset instead.

This holds for a change made right after Better Thermostat wrote to the TRV and for one made while a control cycle runs. Better Thermostat tells its own writes from yours by their values, not by when they arrive. A cycle that finds a TRV turned or switched since the cycle began does not write over the change, and reads it once the cycle is over. It still writes over it when a window or door is open, when it parks a TRV without an off mode for a room that is off (unless the option "Use the minimum temperature instead of 'off'" is on for that TRV: then turning it switches the room on), and when the TRV was switched to a mode Better Thermostat does not use, such as auto. A turn is taken even when the TRV is switched off or drops off the network right after it.

Two limits remain. A TRV that moves to a setpoint of its own a few seconds after Better Thermostat wrote to it, for example back to a value from its own schedule, looks exactly like a turn at the knob and is taken as your new target. And a TRV whose calibration offset Better Thermostat writes gets its mode sent again about three seconds later, because some models leave their mode after an offset write. A TRV switched off by hand within those three seconds is switched on again; switch it off once more and the change holds.

## Control cycles

Every change that matters, whether from a sensor, from a TRV or from you on the thermostat entity, asks for a control cycle. A cycle reads the current state of all entities, decides what each TRV should do and writes only what differs from what the TRV already has. Requests that arrive while a cycle is waiting are folded into it, so a burst of changes leads to one decision.

## Timed data updates

Some work does not wait for a state change:

| Interval | What happens |
| --- | --- |
| Every minute | The smoothed room temperature and the temperature slope are updated. The thermostat entity writes a new state for them only when one of them changes at the precision it shows them. The thermostat also checks which sensors are unavailable, so a sensor that went away moves it into [degraded mode](/faq/degraded-mode) without waiting for another event. |
| Every 5 minutes | A control cycle runs, if a TRV uses the External Sensor Offset Only, MPC Predictive, MPC v2, TPI Controller or PID Controller calibration mode. |
| Every 5 minutes | Each TRV's reported state is compared with what Better Thermostat last sent it, and the cooler's mode with the mode Better Thermostat chose for it. A write that got lost is sent again. |
| Every 5 minutes | If valve maintenance is enabled for a TRV, the thermostat checks whether a maintenance run is due. |
| Every 30 minutes | The room temperature is sent again to TRVs that accept an external temperature, so that a TRV that waits for regular updates gets one even while the room temperature does not change. |
| Every hour | The weather entity's forecast and current temperature are read, and the summer-mode decision is checked again. |
| Every day at 5:00 | The outdoor sensor's damped temperature is checked. |

After a restart, Better Thermostat waits for slow integrations before it reports a missing sensor: the degraded mode repair issue stays back for the first five minutes.

## Writes to the TRVs

Writes go out as soon as a cycle decides them, with one limit: each TRV gets at most one write every 30 seconds per channel (target temperature, offset and valve). With the HomematicIP option the spacing is ten minutes. Safety-relevant writes, such as frost protection, turning a TRV off or closing a valve, are sent at once. See [Why does my TRV pick up a change with a delay?](/faq/common-questions/#why-does-my-trv-pick-up-a-change-with-a-delay)

## Better Thermostat's own entities

The thermostat entity and all number, switch and sensor entities of the device are updated by Better Thermostat itself and never polled. The sensors follow the thermostat entity: they update whenever its state changes. See [Entities](/setup/entities/) for what each one shows.
