"""Window event handling and debounce queue helper.

These helpers respond to window sensor events and implement debouncing and
delayed handling so that HVAC behavior uses window-open information reliably.
"""

import asyncio
from dataclasses import replace
import logging

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from custom_components.better_thermostat import DOMAIN
from custom_components.better_thermostat.core.fsm.window import (
    WindowParams,
    WindowPhase,
    step as window_step,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_window_change(self, event) -> None:
    """Triggered by window sensor event from HA to check if the window is open.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    event :
            Event object from the eventbus. Contains the new and old state from the window (group).

    Returns
    -------
    None
    """

    new_state = event.data.get("new_state")

    if None in (self.hass.states.get(self.window_id), self.window_id, new_state):
        return

    new_state = new_state.state

    old_window_open = self.window_open

    if new_state in ("on", "true", "open", "unknown", "unavailable"):
        new_window_open = True
        if new_state == "unknown":
            _LOGGER.warning(
                "better_thermostat %s: Window sensor state is unknown, assuming window is open",
                self.device_name,
            )
        elif new_state == "unavailable":
            _LOGGER.info(
                "better_thermostat %s: Window sensor is unavailable, assuming window is open",
                self.device_name,
            )

        # window was opened, disable heating power calculation for this period
        self._heating_tracker.start_temp = None
        self.async_write_ha_state()
    elif new_state in ("off", "false", "closed"):
        new_window_open = False
    else:
        _LOGGER.error(
            "better_thermostat %s: New window sensor state '%s' not recognized",
            self.device_name,
            new_state,
        )
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=f"invalid_window_state_{self.device_name}",
            is_fixable=False,
            is_persistent=False,
            learn_more_url="https://better-thermostat.org/faq/window-sensor",
            severity=ir.IssueSeverity.ERROR,
            translation_key="invalid_window_state",
            translation_placeholders={
                "name": str(self.device_name),
                "state": str(new_state),
            },
        )
        return

    # make sure to skip events which do not change the saved window state:
    if new_window_open == old_window_open:
        _LOGGER.debug(
            "better_thermostat %s: Window state did not change, skipping event",
            self.device_name,
        )
        return

    # Start the pending transition in the window region; the queued task
    # settles it (the region owns the timing). The pre-step committed
    # state travels along so the handler can detect a commit even when a
    # zero delay commits immediately.
    was_open = self.kernel_state.window.effective_open
    self.kernel_state = replace(
        self.kernel_state,
        window=window_step(
            self.kernel_state.window,
            sensor_open=new_window_open,
            now=self.clock.monotonic(),
            params=_window_params(self),
        ),
    )
    await self.window_queue_task.put(was_open)


def _window_params(self) -> WindowParams:
    """Debounce delays from the entity configuration."""
    return WindowParams(
        open_delay_s=float(self.window_delay or 0),
        close_delay_s=float(self.window_delay_after or 0),
    )


async def _settle_window_region(self, was_open: bool) -> None:
    """Drive the window region until no transition is pending.

    The region owns the debounce timing: this helper sleeps exactly the
    remaining delay the region asks for, re-reads the sensor, and
    re-steps. A delay reconfigured mid-flight changes the next sleep,
    and a sensor that reverted cancels the transition (false positive).
    """
    while True:
        region = self.kernel_state.window
        if region.pending_since is None:
            break
        params = _window_params(self)
        delay = (
            params.open_delay_s
            if region.phase == WindowPhase.OPENING
            else params.close_delay_s
        )
        remaining = region.pending_since + delay - self.clock.monotonic()
        if remaining > 0:
            _LOGGER.debug(
                "better_thermostat %s: window %s, waiting %.1f seconds "
                "before continuing",
                self.device_name,
                "opened" if region.phase == WindowPhase.OPENING else "closed",
                remaining,
            )
            await asyncio.sleep(remaining)
        sensor = self.hass.states.get(self.window_id)
        sensor_open = sensor is None or sensor.state not in ("off", "false", "closed")
        self.kernel_state = replace(
            self.kernel_state,
            window=window_step(
                self.kernel_state.window,
                sensor_open=sensor_open,
                now=self.clock.monotonic(),
                params=_window_params(self),
            ),
        )

    if was_open != self.kernel_state.window.effective_open:
        self.async_write_ha_state()
        if getattr(self, "in_maintenance", False):
            # Keep state up to date during maintenance, but defer control
            # until maintenance ends.
            self._control_needed_after_maintenance = True
        else:
            request_control_cycle(self, replace_pending=True)


async def window_queue(self):
    """Process queued window-open events.

    Each queued item carries the committed state from before the event;
    settling the region decides whether the change commits, and a real
    change kicks the control queue.
    """
    try:
        while True:
            queued = await self.window_queue_task.get()
            try:
                if queued is not None:
                    await _settle_window_region(self, was_open=queued)
            except asyncio.CancelledError:
                _LOGGER.debug(
                    "better_thermostat %s: Window queue processing cancelled",
                    self.device_name,
                )
                raise
            finally:
                self.window_queue_task.task_done()
    except asyncio.CancelledError:
        _LOGGER.debug(
            "better_thermostat %s: Window queue task cancelled", self.device_name
        )
        raise
