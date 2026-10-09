"""Cooler event handlers for the Better Thermostat integration.

Contains the event handler that reacts to changes in the configured cooler
entity and updates the integration state accordingly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State

from custom_components.better_thermostat.utils.helpers import (
    COOLER_SETPOINT_KEYS,
    cooler_mode_diverges,
    cooler_send_cache,
    device_setpoint_step,
    dual_role_entity_id,
    last_sent_cooler_temperature,
    on_cooler_grid,
    read_setpoint_celsius,
    resolve_inbound_setpoint,
    resolve_state_change_event,
    setpoint_echo_window,
    settle_cooler_reading,
    state_says_nothing,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle

if TYPE_CHECKING:
    from homeassistant.core import Event, EventStateChangedData

    from custom_components.better_thermostat.climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

# The cooler modes whose reported setpoint is a cooling setpoint.
COOLING_MODES = (HVACMode.COOL, HVACMode.HEAT_COOL)


def cooling_writes_as_held(
    self: BetterThermostat, state: State
) -> tuple[float | None, float | None]:
    """Return the cooling channel's writes as the device holds them, in °C.

    The cooling channel sends the cool target rounded onto the cooler's own
    grid, which on a Fahrenheit system is whole degrees Fahrenheit unless the
    cooler publishes a step of its own. A report is compared with those grid
    points: rounded onto any other grid, a write of 75 °F lands half a
    Fahrenheit degree off the value sent, and a press to 76 °F reads as that
    write coming back. The cool target stands for a write whose service call
    has not returned yet, which the send cache records only afterwards.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    state : State
        the cooler's reported state, which carries its step

    Returns
    -------
    tuple[float | None, float | None]
        the cool target and the last sent cooling setpoint on the cooler's
        grid, each None while unknown
    """
    return (
        on_cooler_grid(self, state, self.cool_target_temperature),
        on_cooler_grid(self, state, last_sent_cooler_temperature(self)),
    )


def settle_on_own_write_report(
    self: BetterThermostat, event: Event[EventStateChangedData]
) -> None:
    """Take a report caused by BT's own write as the cooler's answer to it.

    Such a report carries BT's context and is otherwise passed over as BT's
    own doing, but the setpoint in it is what the device made of the write:
    the value sent, or the value on the coarser grid the device holds. Kept as
    the settled reading, it is what a later report is compared with, so a
    press within the device's quantization of the write is told apart from
    the answer. A device that carries both roles is left out, because its
    reports also answer the heating channel's writes.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    event : Event[EventStateChangedData]
        the cooler's state change, caused by BT's own service call
    """
    new_state = event.data.get("new_state")
    if (
        not isinstance(new_state, State)
        or new_state.state not in COOLING_MODES
        or event.data.get("entity_id") == dual_role_entity_id(self)
    ):
        return
    reading = read_setpoint_celsius(
        self, new_state, COOLER_SETPOINT_KEYS, "settle_on_own_write_report()"
    )
    if reading is not None:
        settle_cooler_reading(self, reading)


async def trigger_cooler_change(
    self: BetterThermostat, event: Event[EventStateChangedData]
) -> None:
    """Trigger a change in the cooler state."""
    if self.startup_running:
        return
    if self.control_queue_task is None:
        return

    if event.context == self.context:
        settle_on_own_write_report(self, event)
    resolved_event = resolve_state_change_event(self, event, "Cooler")
    if resolved_event is None:
        return
    old_state, new_state, entity_id = resolved_event

    _LOGGER.debug(
        "better_thermostat %s: Cooler %s update received", self.device_name, entity_id
    )

    if entity_id == dual_role_entity_id(self):
        # A device that carries both roles reports into the TRV handler, which
        # takes every reading this one takes and files a reported setpoint
        # under the channel that drives the device. Adopting here as well would
        # read the heating channel's own write as a press on the cooler's
        # controls.
        _LOGGER.debug(
            "better_thermostat %s: Cooler %s carries the heating channel as "
            "well, its reports are handled there",
            self.device_name,
            entity_id,
        )
        self.async_write_ha_state()
        return

    if new_state.state != old_state.state:
        # A mode change the cooler reports of its own, an outage included,
        # tells the resend throttle that the device has moved since the last
        # mode command, so a command it no longer holds is not a resend into
        # a reply still on its way.
        cooler_send_cache(self)["hvac_mode_reported"] = self.clock.monotonic()

    _main_change = False
    _step = device_setpoint_step(self, new_state, "trigger_cooler_change()")
    # The previous state only answers whether the cooler was publishing a
    # setpoint at all, so it is read without clamping or echo detection.
    _old_cooling_setpoint = read_setpoint_celsius(
        self, old_state, COOLER_SETPOINT_KEYS, "trigger_cooler_change()"
    )
    # Compare only against values BT itself wrote, as the cooler holds them.
    # ``_old_cooling_setpoint`` is the cooler's previously published state and
    # is not necessarily a BT-written value, so it does not belong in the
    # echo-suppression set.
    _last_sent = last_sent_cooler_temperature(self)
    _new_cooling_setpoint = resolve_inbound_setpoint(
        self,
        new_state,
        keys=COOLER_SETPOINT_KEYS,
        known_values=cooling_writes_as_held(self, new_state),
        cooling=True,
        step=_step,
        log_source="trigger_cooler_change()",
    )
    if state_says_nothing(new_state):
        # A cooler that is unavailable or has no mode yet can still carry a
        # setpoint: an entity reports "unknown" while publishing its full
        # attributes, and one that writes the state machine directly keeps the
        # attributes it last set. Such a value is retained rather than reported
        # and says nothing about the device now, so neither the seed nor the
        # adoption gate below may take it: whatever either of them stores is
        # written straight back to that same device.
        # _seed_cool_target_from_cooler() declines the two states at startup.
        # The guard sits ahead of both branches so that declining ends the
        # event: falling through would let the gate read that same retained
        # setpoint and store it as the cool target, raised to clear the heating
        # target, which is exactly what declining refuses. The setpoint being
        # passed over is logged because it is the diagnostic — it is what a
        # later report has to differ from before anything is adopted.
        _LOGGER.debug(
            "better_thermostat %s: Cooler %s is %s, not adopting its retained "
            "setpoint %s",
            self.device_name,
            entity_id,
            new_state.state,
            None if _new_cooling_setpoint is None else _new_cooling_setpoint.raw,
        )
        self.async_write_ha_state()
        return
    # A cooler that is off publishes whatever its integration shows for that
    # state, often a placeholder such as Tado's 5 °C, and one whose mode
    # changes with the same report publishes the setpoint of the mode it
    # leaves or enters rather than one the user set. Only a report that stays
    # in a cooling mode speaks for a change of the cooling target: COOL, the
    # mode BT drives the cooler in, or HEAT_COOL, whose upper bound is the
    # cooling setpoint. A report in one of them, whatever the mode before,
    # carries a cooling setpoint and can seed a target that is unknown; any
    # other mode leaves the seed to the preset.
    _cooling_report = new_state.state in COOLING_MODES
    _stays_cooling = _cooling_report and old_state.state == new_state.state
    # A report on the cooler's answer to BT's last write is that write coming
    # back, even where the device held it on a coarser grid than the step
    # the echo window above allows for.
    _answers_last_write = False
    if _new_cooling_setpoint is not None and _cooling_report:
        _settled = settle_cooler_reading(self, _new_cooling_setpoint.raw)
        _answers_last_write = _settled is not None and abs(
            _new_cooling_setpoint.raw - _settled
        ) < setpoint_echo_window(_step)
    if self.cool_target_temperature is None and not _cooling_report:
        if (
            self._seed_cool_target_from_preset(
                entity_id, f"reports mode {new_state.state}"
            )
            and self.bt_hvac_mode != HVACMode.OFF
        ):
            _main_change = True
    elif _new_cooling_setpoint is not None and self.cool_target_temperature is None:
        # An unknown cool target holds the cooler OFF on every control cycle,
        # and the gate below cannot lift it: that gate needs a setpoint in the
        # previous state, which a cooler that was away usually no longer
        # publishes, and a reported move, which a cooler resting on its own
        # setpoint never reports. The device's own setpoint is the only value
        # there is; taking it loses no user intent because the field carries
        # none, and it cannot be an echo either, because no setpoint is written
        # to the cooler while the target is unknown.
        self._seed_cool_target(_new_cooling_setpoint, entity_id)
        if self.bt_hvac_mode != HVACMode.OFF:
            _main_change = True
    elif (
        _new_cooling_setpoint is not None
        and _old_cooling_setpoint is not None
        and _stays_cooling
        and self.bt_hvac_mode != HVACMode.OFF
    ):
        _LOGGER.debug(
            "better_thermostat %s: trigger_cooler_change / "
            "_old_cooling_setpoint: %s - _new_cooling_setpoint: %s - "
            "cool_target_temperature: %s - last_sent: %s - step: %s - echo: %s - "
            "answers_last_write: %s - contact_open: %s",
            self.device_name,
            _old_cooling_setpoint,
            _new_cooling_setpoint.value,
            self.cool_target_temperature,
            _last_sent,
            _step,
            _new_cooling_setpoint.is_echo,
            _answers_last_write,
            self.contact_open,
        )
        # The cooler handler has no device-side gate of its own, so an event
        # that republishes the same setpoint — an attribute refresh, a mode
        # change, a temperature push — must not be read as user intent: a
        # stale report would otherwise revert a BT-side target that has not
        # been written yet.
        # What the cooler reports also speaks for the cooling channel alone: a
        # value that would cross the heating target is raised onto the floor
        # above it, so a press on the air conditioner's remote does not pull
        # the radiators' target down — potentially below room temperature,
        # stopping the heating. Where the range holds no value above that
        # target the floor stops short of it, and the fallback below is what
        # moves the heating target then.
        _reported_moved = abs(
            _new_cooling_setpoint.raw - _old_cooling_setpoint
        ) >= setpoint_echo_window(_step)
        # While a contact is open the cooler is held OFF and receives no
        # setpoint, so nothing BT wrote explains a setpoint the device reports
        # mid-airing; adopting it would let the airing move the user's cooling
        # target. The TRV handler draws the same line.
        if (
            not _new_cooling_setpoint.is_echo
            and not _answers_last_write
            and _reported_moved
            and self.contact_open is False
        ):
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
                    self.heat_target_temperature,
                    _adopted_cooling_setpoint,
                )
            self.cool_target_temperature = _adopted_cooling_setpoint
            # The press takes the place of BT's last write as what the device
            # holds, and the reading is the device's answer to it: a later
            # press back to the answer of the replaced write is a press again,
            # and the next one near this reading is not taken for its answer.
            # The press was not sent, so it carries no send time for the
            # resend throttle.
            _cooler_sent = cooler_send_cache(self)
            _cooler_sent["temperature"] = (_new_cooling_setpoint.raw, None)
            _cooler_sent["temperature_settled"] = _new_cooling_setpoint.raw
            # The clamp leaves the heating target alone, so this only settles
            # the degenerate case where no cooling value above the heating
            # target exists inside the range: at a heating target resting on
            # the cooling maximum it drops that target by one step, and a range
            # the children narrowed below a target already in place is what
            # moves it further — that move is what brings it back inside the
            # range.
            self._enforce_heat_below_cool()
            _main_change = True
        elif _reported_moved:
            # A setpoint change arrived from the cooler but was not adopted as
            # user intent. Record which guard suppressed it so intermittent
            # "change ignored" reports can be diagnosed from a debug log
            # instead of guesswork.
            _LOGGER.debug(
                "better_thermostat %s: Cooler %s setpoint change %s -> %s NOT "
                "adopted (echo=%s answers_last_write=%s contact_open=%s "
                "cool_target_temperature=%s last_sent=%s step=%s)",
                self.device_name,
                entity_id,
                _old_cooling_setpoint,
                _new_cooling_setpoint.value,
                _new_cooling_setpoint.is_echo,
                _answers_last_write,
                self.contact_open,
                self.cool_target_temperature,
                _last_sent,
                _step,
            )

    # No control cycle reaches a cooler while it is away, so one that comes
    # back may hold a mode or a setpoint the cycles since then would have
    # changed. A cooler that starts running in a mode the cooling channel did
    # not decide on, through its own remote or another integration, is put
    # back by a cycle as well: nothing else in the room has to move for that
    # to happen, and the unit may be cooling into an open window. A cooler
    # that stops while the channel wants it cooling is left to the next cycle
    # the room asks for or to the reconciler, so switching the unit off by
    # hand is not undone the moment the report arrives.
    if state_says_nothing(old_state) or (
        new_state.state not in (old_state.state, HVACMode.OFF)
        and cooler_mode_diverges(self, new_state)
    ):
        _main_change = True

    if _main_change is True:
        self.async_write_ha_state()
        return request_control_cycle(self)
    self.async_write_ha_state()
    return
