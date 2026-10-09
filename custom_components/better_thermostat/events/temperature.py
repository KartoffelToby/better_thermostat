"""External temperature event handlers for Better Thermostat.

This module includes logic to handle external temperature updates and apply
debounce, anti-flicker, accumulation, and plateau acceptance heuristics used
to make robust decisions about whether the external temperature should be
propagated to the target devices.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import math
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
)
from custom_components.better_thermostat.utils.const import DOMAIN
from custom_components.better_thermostat.utils.helpers import (
    convert_to_float_celsius,
    entry_issue_id,
    is_reasonable_temperature,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle
from custom_components.better_thermostat.utils.watcher import room_sensor_reading

if TYPE_CHECKING:
    from homeassistant.core import Event, EventStateChangedData

    from custom_components.better_thermostat.climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)


# Accept sub-threshold changes if the new value stays stable for this window (seconds)
PLATEAU_ACCEPT_WINDOW = 120


def _update_room_temperature_ema(
    self: BetterThermostat, rounded_temperature: float
) -> float:
    """Update and return EMA-filtered external temperature.

    Uses a time-based EMA so varying sensor update intervals behave sensibly.

    Tunables (optional attributes on `self`):
    - `room_temperature_ema_tau_seconds` (float): time constant in seconds (e.g. 900=15min, 1800=30min)
    """

    tau_seconds = float(self.room_temperature_ema_tau_seconds or 300.0)
    if tau_seconds <= 0:
        tau_seconds = 300.0

    now_m = monotonic()
    prev_ts = self._room_temperature_ema_monotonic
    prev_ema = self.room_temperature_ema

    if prev_ts is None or prev_ema is None:
        ema = float(rounded_temperature)
    else:
        dt_seconds = max(0.0, float(now_m) - float(prev_ts))
        # alpha = 1 - exp(-dt/tau)
        alpha = 1.0 - math.exp(-dt_seconds / tau_seconds) if dt_seconds > 0 else 0.0
        ema = float(prev_ema) + alpha * (float(rounded_temperature) - float(prev_ema))

        _LOGGER.debug(
            "better_thermostat %s: EMA calc: prev=%.3f input=%.3f dt=%.1fs alpha=%.4f -> new=%.3f",
            self.device_name,
            float(prev_ema),
            float(rounded_temperature),
            dt_seconds,
            alpha,
            ema,
        )

    self._room_temperature_ema_monotonic = now_m
    self.room_temperature_ema = ema
    # Expose a generic name so consumers don't need to know EMA vs SMA
    self.room_temperature_filtered = round(float(ema), 2)
    return float(ema)


# Every external-temperature write happens under the filter lock, so one
# device that never answers would otherwise hold back every later reading and
# keepalive tick. A write that outlasts this bound counts as refused; the
# ``TimeoutError`` it raises is an ``OSError`` and meets the same handlers.
EXTERNAL_TEMPERATURE_WRITE_TIMEOUT_S = 30.0


def temperature_filter_lock(self: BetterThermostat) -> asyncio.Lock:
    """Return the lock that serialises this entity's temperature filter.

    The filter carries state from one reading to the next: the accumulated
    delta, the pending plateau value and its timer. Home Assistant handles
    every sensor update in its own task, and applying a reading suspends
    while the value is written to the TRVs. Without the lock a reading that
    arrives during such a write is decided against, and committed on top of,
    a half-applied predecessor. The lock is created on first use and lives
    on the entity, so each Better Thermostat only queues behind itself.

    Everything that writes the room temperature to the TRVs takes this
    lock: the sensor readings, the plateau timer and the keepalive tick.

    Parameters
    ----------
    self :
            self instance of better_thermostat

    Returns
    -------
    asyncio.Lock
            the entity's own lock, created on first use
    """
    lock = self._temperature_filter_lock
    if lock is None:
        lock = asyncio.Lock()
        self._temperature_filter_lock = lock
    return lock


def _room_sensor_returns(self: BetterThermostat, previous_state: State | None) -> bool:
    """Tell whether a reading brings the room back from a sensor outage.

    The room is off its sensor while the ladder stands on a lower rung, and
    the reading returns from the outage when the sensor's previous state
    carried no room temperature.
    """
    if self.kernel_state.control_mode.mode == ControlMode.OPTIMAL:
        return False
    return room_sensor_reading(self, previous_state) is None


async def _commit_temperature_update(
    self: BetterThermostat, new_temperature: float
) -> None:
    """Apply the new external temperature and trigger updates.

    Callers hold the filter lock.
    """
    _LOGGER.debug(
        "better_thermostat %s: _commit_temperature_update called with %.2f",
        self.device_name,
        new_temperature,
    )
    _cur_q = None if self.room_temperature is None else round(self.room_temperature, 2)
    new_temperature_rounded = round(new_temperature, 2)

    # Remember previous value as stable pre-measure before updating
    if _cur_q is not None and _cur_q != new_temperature_rounded:
        self.prev_stable_temperature = _cur_q
    # Remember the direction (only on a real change)
    if _cur_q is not None:
        if new_temperature_rounded > _cur_q:
            self.last_change_direction = 1
        elif new_temperature_rounded < _cur_q:
            self.last_change_direction = -1
    self.room_temperature = new_temperature_rounded
    self.last_known_external_temperature = new_temperature_rounded
    # Update EMA (useful if called from timer after delay)
    try:
        _update_room_temperature_ema(self, float(new_temperature_rounded))
    except (TypeError, ValueError) as exc:
        _LOGGER.debug(
            "better_thermostat %s: EMA update failed (non-critical): %s",
            self.device_name,
            exc,
        )
    _ema = self.room_temperature_ema
    self.last_external_sensor_change = dt_util.now()
    # Reset accumulation & pending after accept
    self.accum_delta = 0.0
    self.accum_dir = 0
    self.pending_temperature = None
    self.pending_since = None
    # Cancel any pending plateau timer
    if self.plateau_timer_cancel is not None:
        self.plateau_timer_cancel()
        self.plateau_timer_cancel = None
    self.async_write_ha_state()
    if _ema is not None:
        _LOGGER.debug(
            "better_thermostat %s: external_temperature filtered (ema_tau_s=%s) raw=%.2f ema=%.2f",
            self.device_name,
            self.room_temperature_ema_tau_seconds,
            float(new_temperature_rounded),
            float(_ema),
        )
    # Write the value used by BT (self.room_temperature) to the TRV. The heads are
    # read from `real_trvs`, which is what carries the quirks the write goes
    # through: an id from anywhere else resolves to no TRV and no write.
    entity_ids: list[str] = []
    try:
        entity_ids = list(self.real_trvs.keys())
    except (AttributeError, TypeError) as exc:
        _LOGGER.warning(
            "better_thermostat %s: no TRV list to write external_temperature to: %s",
            self.device_name,
            exc,
        )
    for entity_id in entity_ids:
        try:
            _trv = self.real_trvs.get(entity_id)
            if _trv is not None and _trv.awaiting_initialization:
                # Its first write goes out with its initialization.
                continue
            quirks: object = _trv.model_quirks if _trv is not None else None
            room_temperature = self.room_temperature
            if not isinstance(quirks, ExternalTemperatureQuirk):
                _LOGGER.debug(
                    "better_thermostat %s: no quirks with maybe_set_external_temperature for %s",
                    self.device_name,
                    entity_id,
                )
            elif room_temperature is None:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature write to %s skipped (room_temperature is None)",
                    self.device_name,
                    entity_id,
                )
            else:
                async with asyncio.timeout(EXTERNAL_TEMPERATURE_WRITE_TIMEOUT_S):
                    await quirks.maybe_set_external_temperature(
                        self, entity_id, room_temperature
                    )
        except (
            HomeAssistantError,
            OSError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as exc:
            # A device that refuses the value keeps its old one; the other TRVs
            # still get the reading, and the control cycle below runs on it.
            _LOGGER.warning(
                "better_thermostat %s: external_temperature write to %s failed: %s",
                self.device_name,
                entity_id,
                exc,
            )
    # Enqueue control action (skip during valve maintenance to avoid overwriting exercise).
    # Still mark that a control cycle is needed after maintenance so we immediately
    # resume with the latest temperature.
    if self.control_queue_task is not None:
        if self.in_maintenance:
            self._control_needed_after_maintenance = True
        else:
            request_control_cycle(self)
    _LOGGER.debug(
        "better_thermostat %s: _commit_temperature_update finished", self.device_name
    )


def _sensor_still_reads(self: BetterThermostat, value: float) -> bool:
    """Tell whether the room sensor still reports a pending reading.

    A timer that commits a pending reading queues on the filter lock with
    the sensor's next state change. A sensor that has moved on carries a
    newer reading, whose own event is still waiting for the lock and is
    judged there, and a sensor that gives no usable reading any more has
    withdrawn the pending one. The reading is compared at the precision
    readings are kept.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    value : float
            The pending reading, rounded to two decimals

    Returns
    -------
    bool
            True if the sensor still reads ``value``.
    """
    sensor_entity_id = self.sensor_entity_id
    if sensor_entity_id is None:
        return False
    reading = room_sensor_reading(self, self.hass.states.get(sensor_entity_id))
    return reading is not None and round(reading, 2) == value


def _commit_pending_after(self: BetterThermostat, delay_seconds: float) -> None:
    """Apply the pending reading once the debounce interval has run out.

    A reading turned away only because it came too soon after the last one
    is the room's temperature as soon as the interval is over: a sensor that
    reports on change says nothing more until the room moves again. The
    timer shares the plateau timer's handle, so a newer reading, a commit or
    the entity's removal cancels it the same way. Its firing runs as work the
    entity owns, so a removal also stops a commit already writing to the TRVs.
    A sensor that has since stopped giving a usable reading, or that now
    reads a different value, has withdrawn the pending one.
    """
    _value = self.pending_temperature
    _since = self.pending_since
    if _value is None:
        return
    if self.plateau_timer_cancel is not None:
        self.plateau_timer_cancel()

    async def _interval_cb() -> None:
        async with temperature_filter_lock(self):
            if self.is_removed:
                return
            if self.pending_temperature != _value or self.pending_since != _since:
                return
            if not _sensor_still_reads(self, _value):
                return
            _LOGGER.debug(
                "better_thermostat %s: external_temperature accepted after the "
                "debounce interval (value=%.2f)",
                self.device_name,
                _value,
            )
            await _commit_temperature_update(self, _value)

    @callback
    def _interval_due(_now: datetime) -> None:
        self.plateau_timer_cancel = None
        self._spawn_owned(_interval_cb(), name=f"bt_debounce_commit_{self.device_name}")

    self.plateau_timer_cancel = async_call_later(
        self.hass, delay_seconds, _interval_due
    )


async def trigger_temperature_change(
    self: BetterThermostat, event: Event[EventStateChangedData]
) -> None:
    """Handle temperature changes.

    Decides whether one external temperature reading is applied. Readings
    are handled one at a time, so a reading that arrives while an earlier
    one is still being applied waits its turn instead of being dropped and
    is then judged against the state the earlier one left behind.

    Callers hold the filter lock (see :func:`temperature_filter_lock`);
    the decision reads and rewrites filter state that must not be shared
    with a second reading.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    event :
            Event object from the eventbus. Contains the current trigger time.

    Returns
    -------
    None
    """
    if self.startup_running:
        return

    new_state = event.data.get("new_state")
    if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
        return

    _incoming_temperature = convert_to_float_celsius(
        new_state.state,
        self.device_name,
        "external_temperature",
        unit_of_measurement=new_state.attributes.get("unit_of_measurement"),
    )
    # Quantize to 2 decimals to avoid floating-point artifacts
    _incoming_temperature_q = (
        None if _incoming_temperature is None else round(_incoming_temperature, 2)
    )

    # Debounce (seconds) of the room sensor; anti-flicker lets us go down to 5s
    # here. The radio limits of the heads are paced where BT writes to them.
    _time_diff = 5
    # Significance threshold: 0.11°C (to filter out 0.1°C noise).
    # We ignore the tolerance setting here so we keep getting precise sensor
    # updates even with a larger control tolerance.
    _sig_threshold = 0.11

    if _incoming_temperature_q is None or not is_reasonable_temperature(
        _incoming_temperature_q
    ):
        # raise a ha repair notification
        _LOGGER.error(
            "better_thermostat %s: external_temperature %s is outside the "
            "plausible range; ignoring (raw state: %s)",
            self.device_name,
            _incoming_temperature_q,
            new_state.state,
        )
        # Minimal compatible call (parameter names match the current HA API)
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=entry_issue_id(
                self._config_entry_id, "invalid_external_temperature"
            ),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="invalid_external_temperature",
            learn_more_url="https://better-thermostat.org/faq/invalid-external-temperature",
            translation_placeholders={
                "name": self.device_name,
                "value": new_state.state,
            },
        )
        return

    # A plausible reading clears the repair issue an implausible one raised,
    # so a sensor that recovers does not leave the warning standing.
    ir.async_delete_issue(
        self.hass,
        DOMAIN,
        entry_issue_id(self._config_entry_id, "invalid_external_temperature"),
    )

    _now = dt_util.now()
    try:
        _age = (_now - self.last_external_sensor_change).total_seconds()
    except TypeError, AttributeError:  # defensive, should not happen
        _age = 999999
    # Rounded comparison values
    _cur_q = None if self.room_temperature is None else round(self.room_temperature, 2)
    _diff = None if _cur_q is None else abs(_incoming_temperature_q - _cur_q)
    # Quantized difference for a robust threshold check (avoids 0.099999 errors)
    _diff_q = None if _diff is None else round(_diff, 2)
    _sig_threshold_q = round(_sig_threshold, 2)
    _is_significant = _cur_q is None or (
        _diff_q is not None and _diff_q >= _sig_threshold_q
    )
    _interval_ok = _age > _time_diff

    # Accumulation of small changes in the same direction
    _accept_reason = None
    if _cur_q is not None:
        _signed_delta = round(_incoming_temperature_q - _cur_q, 2)
        if _signed_delta != 0:
            # set direction from sign
            _acc_dir_now = 1 if _signed_delta > 0 else -1
            if self.accum_dir in (0, _acc_dir_now):
                self.accum_delta = round(self.accum_delta + _signed_delta, 2)
                self.accum_dir = _acc_dir_now if self.accum_dir == 0 else self.accum_dir
            else:
                # direction flipped: reset accumulation to current delta
                self.accum_delta = _signed_delta
                self.accum_dir = _acc_dir_now
            # Plateau tracking
            if self.pending_temperature != _incoming_temperature_q:
                self.pending_temperature = _incoming_temperature_q
                self.pending_since = dt_util.now()
                # Cancel existing timer if pending value changes
                if self.plateau_timer_cancel is not None:
                    self.plateau_timer_cancel()
                    self.plateau_timer_cancel = None
        # no change (value back to current): reset pending/timer
        elif self.pending_temperature is not None:
            self.pending_temperature = None
            self.pending_since = None
            if self.plateau_timer_cancel is not None:
                self.plateau_timer_cancel()
                self.plateau_timer_cancel = None

    _accum_ok = (
        _cur_q is not None
        and abs(self.accum_delta) >= _sig_threshold_q
        and _interval_ok
    )

    # Plateau acceptance: sub-threshold change persisted long enough
    _plateau_ok = False
    if (
        not _is_significant
        and _cur_q is not None
        and self.pending_temperature is not None
        and self.pending_temperature != _cur_q
        and self.pending_since is not None
    ):
        _plateau_age = (dt_util.now() - self.pending_since).total_seconds()
        _plateau_ok = _plateau_age >= PLATEAU_ACCEPT_WINDOW and _interval_ok

        # Schedule timer if not already scheduled
        if not _plateau_ok and self.plateau_timer_cancel is None:
            remaining = max(0.1, PLATEAU_ACCEPT_WINDOW - _plateau_age)
            _plateau_value = self.pending_temperature
            # A value that left and came back starts a new plateau with a
            # timer of its own; this one only applies the episode it was
            # started for.
            _plateau_since = self.pending_since

            async def _plateau_cb(_now: datetime) -> None:
                self.plateau_timer_cancel = None
                async with temperature_filter_lock(self):
                    # The entity does not own this task, so its removal cannot
                    # cancel it; a timer that got the turn only after the
                    # entity was removed writes nothing.
                    if self.is_removed:
                        return
                    # A reading handled while the timer waited for the filter
                    # has applied or replaced the value the timer was armed
                    # for; only that value, still pending, is applied.
                    if (
                        self.pending_temperature is None
                        or self.pending_temperature != _plateau_value
                        or self.pending_since != _plateau_since
                    ):
                        return
                    if not _sensor_still_reads(self, _plateau_value):
                        return
                    # Re-check the debounce interval at the time the timer fires
                    _cb_age = (
                        dt_util.now() - self.last_external_sensor_change
                    ).total_seconds()
                    if _cb_age <= _time_diff:
                        return
                    _LOGGER.debug(
                        "better_thermostat %s: external_temperature plateau auto-accepted (value=%.2f)",
                        self.device_name,
                        self.pending_temperature,
                    )
                    await _commit_temperature_update(self, self.pending_temperature)

            self.plateau_timer_cancel = async_call_later(
                self.hass, remaining, _plateau_cb
            )

    if _cur_q is None:
        # First reading ever — always accept regardless of interval
        _accept_reason = "first_reading"
    elif _is_significant and _interval_ok:
        _accept_reason = "significant"
    elif _accum_ok:
        _accept_reason = "accumulated"
    elif _plateau_ok:
        _accept_reason = "plateau"

    if _accept_reason is not None:
        # One of the accept paths above matched (first reading, or a
        # significant / accumulated / plateau change once the debounce
        # interval elapsed); log the decision and apply the update.
        _LOGGER.debug(
            "better_thermostat %s: external_temperature update accepted (old=%.2f new=%.2f diff=%.2f "
            "age=%.1fs threshold=%.2f interval=%ss reason=%s accum=%.2f dir=%s)",
            self.device_name,
            (_cur_q if _cur_q is not None else float("nan")),
            _incoming_temperature_q,
            (_diff_q if _diff_q is not None else float("nan")),
            _age,
            _sig_threshold_q,
            _time_diff,
            _accept_reason,
            (self.accum_delta if _cur_q is not None else 0.0),
            ("+" if self.accum_dir > 0 else ("-" if self.accum_dir < 0 else "0")),
        )
        if _room_sensor_returns(self, event.data.get("old_state")):
            # During the outage the minute tick kept feeding the filter the
            # last reading from before it, which says nothing about the room
            # since. The filter starts over from the returning reading, and
            # so does the slope the tick derives from it.
            self.room_temperature_ema = None
            self._room_temperature_ema_monotonic = None
        await _commit_temperature_update(self, _incoming_temperature_q)
    else:
        if (
            not _interval_ok
            and self.pending_temperature is not None
            and (_is_significant or abs(self.accum_delta) >= _sig_threshold_q)
        ):
            _commit_pending_after(self, max(0.1, _time_diff - _age))
        _LOGGER.debug(
            "better_thermostat %s: external_temperature ignored (old=%.2f new=%.2f diff=%s "
            "age=%.1fs sig=%s interval_ok=%s threshold=%.2f accum=%.2f dir=%s pending=%s pending_age=%ss)",
            self.device_name,
            (_cur_q if _cur_q is not None else float("nan")),
            _incoming_temperature_q,
            (f"{_diff_q:.2f}" if _diff_q is not None else "None"),
            _age,
            _is_significant,
            _interval_ok,
            _sig_threshold_q,
            (self.accum_delta if _cur_q is not None else 0.0),
            ("+" if self.accum_dir > 0 else ("-" if self.accum_dir < 0 else "0")),
            (
                f"{self.pending_temperature:.2f}"
                if isinstance(self.pending_temperature, (int, float))
                else None
            ),
            (
                f"{(dt_util.now() - self.pending_since).total_seconds():.1f}"
                if self.pending_since is not None
                else None
            ),
        )
