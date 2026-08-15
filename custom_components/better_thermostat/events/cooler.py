"""Cooler event handlers for the Better Thermostat integration.

Contains the event handler that reacts to changes in the configured cooler
entity and updates the integration state accordingly.
"""

from __future__ import annotations

import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback

from custom_components.better_thermostat.utils.helpers import (
    COOLER_SETPOINT_KEYS,
    device_setpoint_step,
    read_setpoint_celsius,
    resolve_inbound_setpoint,
    resolve_state_change_event,
    setpoint_echo_window,
)

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_cooler_change(self, event):
    """Trigger a change in the cooler state."""
    if self.startup_running:
        return
    if self.control_queue_task is None:
        return

    resolved_event = resolve_state_change_event(self, event, "Cooler")
    if resolved_event is None:
        return
    old_state, new_state, entity_id = resolved_event

    _LOGGER.debug(
        "better_thermostat %s: Cooler %s update received", self.device_name, entity_id
    )

    _main_change = False
    _step = device_setpoint_step(self, new_state, "trigger_cooler_change()")
    # The previous state only answers whether the cooler was publishing a
    # setpoint at all, so it is read without clamping or echo detection.
    _old_cooling_setpoint = read_setpoint_celsius(
        self, old_state, COOLER_SETPOINT_KEYS, "trigger_cooler_change()"
    )
    _new_cooling_setpoint = resolve_inbound_setpoint(
        self,
        new_state,
        keys=COOLER_SETPOINT_KEYS,
        known_values=(self.bt_target_cooltemp, self.last_sent_cooler_temp),
        step=_step,
        log_source="trigger_cooler_change()",
    )
    if _new_cooling_setpoint is not None and self.bt_target_cooltemp is None:
        # An unknown cool target holds the cooler OFF on every control cycle,
        # and the gate below cannot lift it: that gate needs a setpoint in the
        # previous state, which a cooler that was away usually no longer
        # publishes, and a reported move, which a cooler resting on its own
        # setpoint never reports. The device's own setpoint is the only value
        # there is; taking it loses no user intent because the field carries
        # none, and it cannot be an echo either, because no setpoint is written
        # to the cooler while the target is unknown.
        if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # A cooler that is unavailable or has no mode yet can still carry a
            # setpoint: an entity reports "unknown" while publishing its full
            # attributes, and one that writes the state machine directly keeps
            # the attributes it last set. Such a value is retained rather than
            # reported and says nothing about the device now, so it must not
            # become the cool target, which is written straight back to it.
            # _seed_cool_target_from_cooler() declines the same two states at
            # startup. This check sits inside the branch rather than in its
            # condition, so declining ends the event here: the gate below would
            # otherwise read that same retained setpoint and store it as the
            # cool target, raised to clear the heating target, which is exactly
            # what declining refuses.
            _LOGGER.debug(
                "better_thermostat %s: Cooler %s is %s, not seeding the cool "
                "target from its retained setpoint %s",
                self.device_name,
                entity_id,
                new_state.state,
                _new_cooling_setpoint.raw,
            )
        else:
            self._seed_cool_target(_new_cooling_setpoint, entity_id)
            if self.bt_hvac_mode != HVACMode.OFF:
                _main_change = True
    elif (
        _new_cooling_setpoint is not None
        and _old_cooling_setpoint is not None
        and self.bt_hvac_mode != HVACMode.OFF
    ):
        _LOGGER.debug(
            "better_thermostat %s: trigger_cooler_change / "
            "_old_cooling_setpoint: %s - _new_cooling_setpoint: %s - "
            "bt_target_cooltemp: %s - last_sent: %s - step: %s - echo: %s",
            self.device_name,
            _old_cooling_setpoint,
            _new_cooling_setpoint.value,
            self.bt_target_cooltemp,
            self.last_sent_cooler_temp,
            _step,
            _new_cooling_setpoint.is_echo,
        )
        # The cooler handler has no device-side gate of its own, so an event
        # that republishes the same setpoint — an attribute refresh, a mode
        # change, a temperature push — must not be read as user intent: a
        # stale report would otherwise revert a BT-side target that has not
        # been written yet.
        # What the cooler reports also speaks for the cooling channel alone: a
        # value that would cross the heating target is raised above it, so a
        # press on the air conditioner's remote cannot move the radiators'
        # target — potentially below room temperature, stopping the heating.
        _reported_moved = abs(
            _new_cooling_setpoint.raw - _old_cooling_setpoint
        ) >= setpoint_echo_window(_step)
        if not _new_cooling_setpoint.is_echo and _reported_moved:
            if _new_cooling_setpoint.clamped:
                _LOGGER.warning(
                    "better_thermostat %s: New Cooler %s setpoint outside of range, "
                    "overwriting it",
                    self.device_name,
                    entity_id,
                )
            _adopted_cooling_setpoint = self._clamp_inbound_cool_target(
                _new_cooling_setpoint.value
            )
            if _adopted_cooling_setpoint != _new_cooling_setpoint.value:
                # A user turning the remote down step by step would collect one
                # warning per press, so yielding to the heating target is an
                # INFO: the range clamp above and the ordering fallback below
                # own the WARNING level.
                _LOGGER.info(
                    "better_thermostat %s: Cooler %s reported setpoint %.2f does not "
                    "clear the heating target %.2f, keeping %.2f",
                    self.device_name,
                    entity_id,
                    _new_cooling_setpoint.value,
                    self.bt_target_temp,
                    _adopted_cooling_setpoint,
                )
            self.bt_target_cooltemp = _adopted_cooling_setpoint
            # The clamp leaves the heating target alone, so this only settles
            # the degenerate case where no cooling value above the heating
            # target exists inside the range: at a heating target within one
            # step of bt_max_temp it drops that target by one step, and a range
            # the children narrowed below a target already in place is what
            # moves it further — that move is what brings it back inside the
            # range.
            self._enforce_heat_below_cool()
            _main_change = True

    if _main_change is True:
        self.async_write_ha_state()
        return await self.control_queue_task.put(self)
    self.async_write_ha_state()
    return
