"""Door event handling and debounce queue helper.

Door sensors behave like window sensors but carry their own debounce
delays, so that briefly opened doors do not shut the heating down. The
door drives its own region of the control kernel, mirroring the window
region: the raw sensor reading is debounced in both directions before it
commits to an open/closed state that suppresses heating.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging

from homeassistant.helpers import issue_registry as ir

from custom_components.better_thermostat import DOMAIN
from custom_components.better_thermostat.core.fsm.window import (
    WindowParams,
    WindowPhase,
    step as window_step,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle

_LOGGER = logging.getLogger(__name__)


async def trigger_door_change(self, event) -> None:
    """Triggered by door sensor event from HA to check if the door is open.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    event :
            Event object from the eventbus. Contains the new and old state from the door (group).

    Returns
    -------
    None
    """

    new_state = event.data.get("new_state")

    if None in (self.hass.states.get(self.door_id), self.door_id, new_state):
        return

    new_state = new_state.state

    if new_state in ("on", "true", "open"):
        new_door_open = True
    elif new_state in ("off", "false", "closed"):
        new_door_open = False
    elif new_state in ("unknown", "unavailable"):
        # A non-active door sensor counts as closed so heating continues:
        # doors are usually closed, and a lost sensor (e.g. a dead battery)
        # must not stop heating. The unavailability is still surfaced as a
        # warning so it does not go unnoticed.
        new_door_open = False
        if new_state == "unknown":
            _LOGGER.warning(
                "better_thermostat %s: Door sensor state is unknown, assuming door is closed",
                self.device_name,
            )
        else:
            _LOGGER.info(
                "better_thermostat %s: Door sensor is unavailable, assuming door is closed",
                self.device_name,
            )
    else:
        _LOGGER.error(
            "better_thermostat %s: New door sensor state '%s' not recognized",
            self.device_name,
            new_state,
        )
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=f"invalid_door_state_{self.device_name}",
            is_fixable=False,
            is_persistent=False,
            learn_more_url="https://better-thermostat.org/faq/door-sensor",
            severity=ir.IssueSeverity.ERROR,
            translation_key="invalid_door_state",
            translation_placeholders={
                "name": str(self.device_name),
                "state": str(new_state),
            },
        )
        return

    # Skip only readings that confirm the committed state while no
    # transition is pending. A flip during a pending transition must
    # reach the region so it can cancel a false positive or restart
    # the debounce.
    region = self.kernel_state.door
    if new_door_open == region.effective_open and region.pending_since is None:
        _LOGGER.debug(
            "better_thermostat %s: Door state did not change, skipping event",
            self.device_name,
        )
        return

    if new_door_open:
        # door was opened, disable heating power calculation for this period
        self._heating_tracker.start_temp = None
        self.async_write_ha_state()

    # Step the door region; the queued task settles it (the region owns
    # the timing). The committed state before the step travels along to
    # seed the queue worker's announced state on its first item.
    was_open = region.effective_open
    self.kernel_state = replace(
        self.kernel_state,
        door=window_step(
            self.kernel_state.door,
            sensor_open=new_door_open,
            now=self.clock.monotonic(),
            params=_door_params(self),
        ),
    )
    try:
        self.door_queue_task.put_nowait(was_open)
    except asyncio.QueueFull:
        # A settle run is already pending; it re-reads the stepped region.
        # Only the first-ever item seeds the announced state, and a full
        # queue implies an earlier item already did or will.
        _LOGGER.debug(
            "better_thermostat %s: door settle already pending, coalescing",
            self.device_name,
        )


def _door_params(self) -> WindowParams:
    """Debounce delays from the entity configuration."""
    return WindowParams(
        open_delay_s=float(self.door_delay or 0),
        close_delay_s=float(self.door_delay_after or 0),
    )


async def _settle_door_region(self) -> None:
    """Drive the door region until no transition is pending.

    The region owns the debounce timing: this helper sleeps exactly the
    remaining delay the region asks for, re-reads the sensor, and
    re-steps. A delay reconfigured mid-flight changes the next sleep,
    and a sensor that reverted cancels the transition (false positive).
    """
    while True:
        region = self.kernel_state.door
        if region.pending_since is None:
            break
        params = _door_params(self)
        delay = (
            params.open_delay_s
            if region.phase == WindowPhase.OPENING
            else params.close_delay_s
        )
        remaining = region.pending_since + delay - self.clock.monotonic()
        if remaining > 0:
            _LOGGER.debug(
                "better_thermostat %s: door %s, waiting %.1f seconds before continuing",
                self.device_name,
                "opened" if region.phase == WindowPhase.OPENING else "closed",
                remaining,
            )
            await asyncio.sleep(remaining)
        sensor = self.hass.states.get(self.door_id)
        # A non-active sensor (missing / unavailable / unknown) counts as
        # closed, mirroring the live event handler.
        sensor_open = sensor is not None and sensor.state in ("on", "true", "open")
        self.kernel_state = replace(
            self.kernel_state,
            door=window_step(
                self.kernel_state.door,
                sensor_open=sensor_open,
                now=self.clock.monotonic(),
                params=_door_params(self),
            ),
        )


async def _announce_door_change(self) -> None:
    """Fire the side effects of a committed door change."""
    from custom_components.better_thermostat.utils.helpers import (
        async_fire_logbook_entry,
    )

    if self.kernel_state.door.effective_open:
        await async_fire_logbook_entry(
            self, "door_open", "turned off because a door was opened"
        )
    else:
        await async_fire_logbook_entry(
            self, "door_close", "resumed heating because a door was closed"
        )
    self.async_write_ha_state()
    if getattr(self, "in_maintenance", False):
        # Keep state up to date during maintenance, but defer control
        # until maintenance ends.
        self._control_needed_after_maintenance = True
    else:
        request_control_cycle(self, replace_pending=True)


async def door_queue(self):
    """Process queued door-open events.

    Each queued item carries the committed state from before its trigger
    step; the first item seeds the announced state. Side effects derive
    from the settled region: only a flip of the effective state against
    the announced state fires the logbook entry and control kick, so
    several queued items sharing one commit announce it exactly once.
    """
    announced: bool | None = None
    try:
        while True:
            queued = await self.door_queue_task.get()
            try:
                if queued is not None:
                    if announced is None:
                        announced = queued
                    await _settle_door_region(self)
                    effective = self.kernel_state.door.effective_open
                    if effective != announced:
                        await _announce_door_change(self)
                        announced = effective
            except asyncio.CancelledError:
                _LOGGER.debug(
                    "better_thermostat %s: Door queue processing cancelled",
                    self.device_name,
                )
                raise
            finally:
                self.door_queue_task.task_done()
    except asyncio.CancelledError:
        _LOGGER.debug(
            "better_thermostat %s: Door queue task cancelled", self.device_name
        )
        raise
