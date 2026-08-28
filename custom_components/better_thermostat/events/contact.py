"""Shared contact sensor event handling and debounce queue.

Window and door sensors follow the same contract: a binary contact whose
confirmed "open" state suppresses heating after a configurable delay. Each
kind drives its own region of the control kernel, so the two remain
independent and can be pending at the same time; only the behavior around
them is shared. This module implements that behavior once, and
:mod:`events.window` and :mod:`events.door` bind it to the entity
attributes and the kernel region of their kind.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
from typing import TYPE_CHECKING, Final, Literal

from homeassistant.helpers import issue_registry as ir

from custom_components.better_thermostat import DOMAIN
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.core.fsm.window import (
    WindowParams,
    WindowPhase,
    WindowState,
    step as window_step,
)
from custom_components.better_thermostat.utils.helpers import async_fire_logbook_entry
from custom_components.better_thermostat.utils.scheduler import request_control_cycle

if TYPE_CHECKING:
    from homeassistant.core import Event, EventStateChangedData

    from custom_components.better_thermostat.climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

# Words a contact sensor may use for a confirmed open/closed reading.
OPEN_WORDS: Final = ("on", "true", "open")
CLOSED_WORDS: Final = ("off", "false", "closed")
# A non-active sensor is not a reading, but it is a recognized state.
INACTIVE_WORDS: Final = ("unknown", "unavailable")


def _window_region(state: KernelState) -> WindowState:
    """Return the window region of the kernel state."""
    return state.window


def _with_window_region(state: KernelState, region: WindowState) -> KernelState:
    """Return the kernel state carrying a stepped window region."""
    return replace(state, window=region)


def _door_region(state: KernelState) -> WindowState:
    """Return the door region of the kernel state."""
    return state.door


def _with_door_region(state: KernelState, region: WindowState) -> KernelState:
    """Return the kernel state carrying a stepped door region."""
    return replace(state, door=region)


@dataclass(frozen=True)
class ContactRole:
    """Binding of the shared contact logic to one sensor kind.

    The attribute names say where the configuration of this kind of contact
    lives on the BetterThermostat instance; the two region accessors say
    which kernel region it drives. Naming the region through a pair of
    functions keeps the two regions separate types-wise, so a window event
    cannot reach the door region by a typo in a string.
    """

    kind: Literal["window", "door"]
    entity_id_attr: str
    delay_attr: str
    delay_after_attr: str
    queue_attr: str
    region_of: Callable[[KernelState], WindowState]
    with_region: Callable[[KernelState, WindowState], KernelState]
    issue_translation_key: str
    learn_more_url: str


WINDOW: Final = ContactRole(
    kind="window",
    entity_id_attr="window_id",
    delay_attr="window_delay",
    delay_after_attr="window_delay_after",
    queue_attr="window_queue_task",
    region_of=_window_region,
    with_region=_with_window_region,
    issue_translation_key="invalid_window_state",
    learn_more_url="https://better-thermostat.org/faq/window-sensor",
)

DOOR: Final = ContactRole(
    kind="door",
    entity_id_attr="door_id",
    delay_attr="door_delay",
    delay_after_attr="door_delay_after",
    queue_attr="door_queue_task",
    region_of=_door_region,
    with_region=_with_door_region,
    issue_translation_key="invalid_door_state",
    learn_more_url="https://better-thermostat.org/faq/door-sensor",
)


def _issue_id(self: BetterThermostat, role: ContactRole) -> str:
    """Return the repair issue id for this contact of this thermostat."""
    return f"{role.issue_translation_key}_{self.device_name}"


def _contact_params(self: BetterThermostat, role: ContactRole) -> WindowParams:
    """Debounce delays from the entity configuration."""
    return WindowParams(
        open_delay_s=float(getattr(self, role.delay_attr) or 0),
        close_delay_s=float(getattr(self, role.delay_after_attr) or 0),
    )


async def trigger_contact_change(
    self: BetterThermostat, role: ContactRole, event: Event[EventStateChangedData]
) -> None:
    """Handle a contact sensor state event and step the matching region.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    role :
            Binding that selects the window or door attributes.
    event :
            Event object from the eventbus. Contains the new and old state
            from the contact sensor (group).

    Returns
    -------
    None
    """

    entity_id = getattr(self, role.entity_id_attr)
    new_state = event.data.get("new_state")

    # The entity id is checked before it is used as a lookup key: the state
    # machine does not accept None and would raise on it.
    if entity_id is None or new_state is None:
        return
    if self.hass.states.get(entity_id) is None:
        return

    new_state = new_state.state

    if new_state in OPEN_WORDS:
        new_contact_open = True
    elif new_state in CLOSED_WORDS:
        new_contact_open = False
    elif new_state in INACTIVE_WORDS:
        # A non-active contact sensor counts as closed so heating continues:
        # windows and doors are usually closed, and a lost sensor (e.g. a dead
        # battery) must not stop heating. The unavailability is still surfaced
        # as a warning so it does not go unnoticed.
        new_contact_open = False
        if new_state == "unknown":
            _LOGGER.warning(
                "better_thermostat %s: %s sensor state is unknown, assuming %s is closed",
                self.device_name,
                role.kind.capitalize(),
                role.kind,
            )
        else:
            _LOGGER.info(
                "better_thermostat %s: %s sensor is unavailable, assuming %s is closed",
                self.device_name,
                role.kind.capitalize(),
                role.kind,
            )
    else:
        _LOGGER.error(
            "better_thermostat %s: New %s sensor state '%s' not recognized",
            self.device_name,
            role.kind,
            new_state,
        )
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=_issue_id(self, role),
            is_fixable=False,
            is_persistent=False,
            learn_more_url=role.learn_more_url,
            severity=ir.IssueSeverity.ERROR,
            translation_key=role.issue_translation_key,
            translation_placeholders={
                "name": str(self.device_name),
                "state": str(new_state),
            },
        )
        return

    # A recognized state clears the repair issue an unrecognized one raised.
    # It happens before the dedup below, because a sensor recovering to the
    # state it already had is exactly the case that leaves the issue behind.
    ir.async_delete_issue(self.hass, DOMAIN, _issue_id(self, role))

    # Skip only readings that confirm the committed state while no
    # transition is pending. A flip during a pending transition must
    # reach the region so it can cancel a false positive or restart
    # the debounce.
    region = role.region_of(self.kernel_state)
    if new_contact_open == region.effective_open and region.pending_since is None:
        _LOGGER.debug(
            "better_thermostat %s: %s state did not change, skipping event",
            self.device_name,
            role.kind.capitalize(),
        )
        return

    if new_contact_open:
        # contact was opened, disable heating power calculation for this period
        self._heating_tracker.start_temp = None
        self.async_write_ha_state()

    # Step the region; the queued task settles it (the region owns the
    # timing). The committed state before the step travels along to seed
    # the queue worker's announced state on its first item.
    was_open = region.effective_open
    self.kernel_state = role.with_region(
        self.kernel_state,
        window_step(
            region,
            sensor_open=new_contact_open,
            now=self.clock.monotonic(),
            params=_contact_params(self, role),
        ),
    )
    try:
        getattr(self, role.queue_attr).put_nowait(was_open)
    except asyncio.QueueFull:
        # A settle run is already pending; it re-reads the stepped region.
        # Only the first-ever item seeds the announced state, and a full
        # queue implies an earlier item already did or will.
        _LOGGER.debug(
            "better_thermostat %s: %s settle already pending, coalescing",
            self.device_name,
            role.kind,
        )


async def _settle_contact_region(self: BetterThermostat, role: ContactRole) -> None:
    """Drive one contact region until no transition is pending.

    The region owns the debounce timing: this helper sleeps exactly the
    remaining delay the region asks for, re-reads the sensor, and
    re-steps. A delay reconfigured mid-flight changes the next sleep,
    and a sensor that reverted cancels the transition (false positive).
    """
    while True:
        region = role.region_of(self.kernel_state)
        if region.pending_since is None:
            break
        params = _contact_params(self, role)
        delay = (
            params.open_delay_s
            if region.phase == WindowPhase.OPENING
            else params.close_delay_s
        )
        remaining = region.pending_since + delay - self.clock.monotonic()
        if remaining > 0:
            _LOGGER.debug(
                "better_thermostat %s: %s %s, waiting %.1f seconds before continuing",
                self.device_name,
                role.kind,
                "opened" if region.phase == WindowPhase.OPENING else "closed",
                remaining,
            )
            await asyncio.sleep(remaining)
        sensor = self.hass.states.get(getattr(self, role.entity_id_attr))
        # A non-active sensor (missing / unavailable / unknown) counts as
        # closed, mirroring the live event handler.
        sensor_open = sensor is not None and sensor.state in OPEN_WORDS
        self.kernel_state = role.with_region(
            self.kernel_state,
            window_step(
                role.region_of(self.kernel_state),
                sensor_open=sensor_open,
                now=self.clock.monotonic(),
                params=_contact_params(self, role),
            ),
        )


async def _announce_contact_change(self: BetterThermostat, role: ContactRole) -> None:
    """Fire the side effects of a committed contact change."""
    if role.region_of(self.kernel_state).effective_open:
        await async_fire_logbook_entry(
            self, f"{role.kind}_open", f"turned off because a {role.kind} was opened"
        )
    else:
        await async_fire_logbook_entry(
            self,
            f"{role.kind}_close",
            f"resumed heating because a {role.kind} was closed",
        )
    self.async_write_ha_state()
    if getattr(self, "in_maintenance", False):
        # Keep state up to date during maintenance, but defer control
        # until maintenance ends.
        self._control_needed_after_maintenance = True
    else:
        request_control_cycle(self, replace_pending=True)


async def contact_queue(self: BetterThermostat, role: ContactRole) -> None:
    """Process queued contact-open events for one sensor kind.

    Each queued item carries the committed state from before its trigger
    step; the first item seeds the announced state. Side effects derive
    from the settled region: only a flip of the effective state against
    the announced state fires the logbook entry and control kick, so
    several queued items sharing one commit announce it exactly once.
    """
    announced: bool | None = None
    try:
        while True:
            queue = getattr(self, role.queue_attr)
            queued = await queue.get()
            try:
                if queued is not None:
                    if announced is None:
                        announced = queued
                    await _settle_contact_region(self, role)
                    effective = role.region_of(self.kernel_state).effective_open
                    if effective != announced:
                        await _announce_contact_change(self, role)
                        announced = effective
            except asyncio.CancelledError:
                _LOGGER.debug(
                    "better_thermostat %s: %s queue processing cancelled",
                    self.device_name,
                    role.kind.capitalize(),
                )
                raise
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        _LOGGER.debug(
            "better_thermostat %s: %s queue task cancelled",
            self.device_name,
            role.kind.capitalize(),
        )
        raise
