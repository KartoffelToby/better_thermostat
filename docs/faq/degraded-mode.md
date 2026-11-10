---
title: Degraded mode
description: What the "degraded mode" repair issue means and how to fix it.
slug: faq/degraded-mode
---

Better Thermostat raises a **degraded mode** repair issue when one or
more of its configured sensors become unavailable — the room temperature
sensor, a window sensor, a humidity sensor, an outdoor sensor, or the
weather entity.

Better Thermostat keeps controlling your heating in degraded mode: if
the room temperature sensor is unavailable it falls back to the TRV's
internal temperature reading, and unavailable optional sensors are
simply left out of the control decisions. Expect less accurate control
until all sensors are back.

TRVs that regulate on a room temperature Better Thermostat writes into
them, such as the Sonoff TRVZB, stop receiving it while the room sensor
gives no reading. Better Thermostat does not keep sending the sensor's last
value, so the TRV falls back to its own sensor after its own timeout (two
hours on a TRVZB). The first reading after the outage is written to the
TRV at once.

During Home Assistant startup, sensors are often briefly unavailable
while their integrations load. Better Thermostat waits out a grace
period before raising the issue, so a warning means a sensor stayed
unavailable beyond startup.

## Weather forecast fallback

When a weather provider goes offline, Better Thermostat can use its last
**timestamped forecast**, stored with the thermostat's other runtime state.
It prefers hourly forecast data when the provider supports it. Each saved
slot is used at its scheduled time, even after a Home Assistant restart.
The cache is limited to **24 hours since the last successful refresh**, and
individual slots expire when their forecast period has passed.

While a cached forecast slot covers the current time, weather unavailability
alone does not trigger degraded mode. Future forecast slots are still available
for heating-season predictions but are not treated as current weather.
Other unavailable sensors still trigger degraded mode normally. Once the
current forecast expires, weather loss is reported again.

The cache supplies the outdoor-temperature and weather-condition fallback
for weather-based controllers, but does not turn a standard PID controller
into a weather-predictive algorithm.

## Common causes

- The sensor's battery is empty or the device lost its radio connection.
- The integration providing the sensor is not loaded or failed to start.
- The sensor was renamed or removed, so the entity id Better Thermostat
  was configured with no longer exists.

## How to fix it

1. The repair issue lists the unavailable sensors. Check each one under
   **Settings → Devices & services**: replace the battery or reconnect
   the device if needed.
2. If an entity id changed, update the Better Thermostat configuration
   to the new entity id (open the Better Thermostat entry and
   reconfigure it).
3. The issue clears on its own once all sensors are available again.
