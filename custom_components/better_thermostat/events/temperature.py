"""External temperature event handlers for Better Thermostat.

This module includes logic to handle external temperature updates and apply
debounce, anti-flicker, accumulation, and plateau acceptance heuristics used
to make robust decisions about whether the external temperature should be
propagated to the target devices.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import math
from time import monotonic

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP, DOMAIN
from custom_components.better_thermostat.utils.helpers import (
    convert_to_float_celsius,
    is_reasonable_temperature,
)

_LOGGER = logging.getLogger(__name__)


# Accept sub-threshold changes if the new value stays stable for this window (seconds)
PLATEAU_ACCEPT_WINDOW = 120


def _update_external_temp_ema(self, temp_q: float) -> float:
    """Update and return EMA-filtered external temperature.

    Uses a time-based EMA so varying sensor update intervals behave sensibly.

    Tunables (optional attributes on `self`):
    - `external_temp_ema_tau_s` (float): time constant in seconds (e.g. 900=15min, 1800=30min)
    """

    tau_s = float(self.external_temp_ema_tau_s or 300.0)
    if tau_s <= 0:
        tau_s = 300.0

    now_m = monotonic()
    prev_ts = self._external_temp_ema_ts
    prev_ema = self.external_temp_ema

    if prev_ts is None or prev_ema is None:
        ema = float(temp_q)
    else:
        dt_s = max(0.0, float(now_m) - float(prev_ts))
        # alpha = 1 - exp(-dt/tau)
        alpha = 1.0 - math.exp(-dt_s / tau_s) if dt_s > 0 else 0.0
        ema = float(prev_ema) + alpha * (float(temp_q) - float(prev_ema))

        _LOGGER.debug(
            "better_thermostat %s: EMA calc: prev=%.3f input=%.3f dt=%.1fs alpha=%.4f -> new=%.3f",
            self.device_name,
            float(prev_ema),
            float(temp_q),
            dt_s,
            alpha,
            ema,
        )

    self._external_temp_ema_ts = now_m
    self.external_temp_ema = ema
    # Expose a generic name so consumers don't need to know EMA vs SMA
    self.cur_temp_filtered = round(float(ema), 2)
    return float(ema)


async def _apply_temperature_update(self, new_temp):
    """Apply the new external temperature and trigger updates."""
    _LOGGER.debug(
        "better_thermostat %s: _apply_temperature_update called with %.2f",
        self.device_name,
        new_temp,
    )
    _cur_q = None if self.cur_temp is None else round(self.cur_temp, 2)
    new_temp_q = round(new_temp, 2)

    # Remember previous value as stable pre-measure before updating
    if _cur_q is not None and _cur_q != new_temp_q:
        self.prev_stable_temp = _cur_q
    # Remember the direction (only on a real change)
    if _cur_q is not None:
        if new_temp_q > _cur_q:
            self.last_change_direction = 1
        elif new_temp_q < _cur_q:
            self.last_change_direction = -1
    self.cur_temp = new_temp_q
    self.last_known_external_temp = new_temp_q
    # Update EMA (useful if called from timer after delay)
    try:
        _update_external_temp_ema(self, float(new_temp_q))
    except Exception as exc:
        _LOGGER.debug(
            "better_thermostat %s: EMA update failed (non-critical): %s",
            self.device_name,
            exc,
        )
    _ema = self.external_temp_ema
    self.last_external_sensor_change = dt_util.now()
    # Reset accumulation & pending after accept
    self.accum_delta = 0.0
    self.accum_dir = 0
    self.pending_temp = None
    self.pending_since = None
    # Cancel any pending plateau timer
    if getattr(self, "plateau_timer_cancel", None) is not None:
        self.plateau_timer_cancel()
        self.plateau_timer_cancel = None
    self.async_write_ha_state()
    if _ema is not None:
        _LOGGER.debug(
            "better_thermostat %s: external_temperature filtered (ema_tau_s=%s) raw=%.2f ema=%.2f",
            self.device_name,
            self.external_temp_ema_tau_s,
            float(new_temp_q),
            float(_ema),
        )
    # Write the value used by BT (self.cur_temp) to the TRV
    try:
        trv_ids = list(self.real_trvs.keys())
        if not trv_ids and hasattr(self, "entity_ids"):
            trv_ids = list(self.entity_ids or [])
        for trv_id in trv_ids:
            _trv = self.real_trvs.get(trv_id) if hasattr(self, "real_trvs") else None
            quirks = _trv.model_quirks if _trv is not None else None
            if quirks and hasattr(quirks, "maybe_set_external_temperature"):
                await quirks.maybe_set_external_temperature(self, trv_id, self.cur_temp)
            else:
                _LOGGER.debug(
                    "better_thermostat %s: no quirks with maybe_set_external_temperature for %s",
                    self.device_name,
                    trv_id,
                )
    except AttributeError, KeyError, TypeError, ValueError, RuntimeError:
        _LOGGER.debug(
            "better_thermostat %s: external_temperature write to TRV failed (non critical)",
            self.device_name,
        )
    # Enqueue control action (skip during valve maintenance to avoid overwriting exercise).
    # Still mark that a control cycle is needed after maintenance so we immediately
    # resume with the latest temperature.
    if self.control_queue_task is not None:
        if getattr(self, "in_maintenance", False):
            self._control_needed_after_maintenance = True
        else:
            await self.control_queue_task.put(self)
    _LOGGER.debug(
        "better_thermostat %s: _apply_temperature_update finished", self.device_name
    )


@callback
async def trigger_temperature_change(self, event):
    """Handle temperature changes.

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
        str(new_state.state),
        self.device_name,
        "external_temperature",
        unit_of_measurement=new_state.attributes.get("unit_of_measurement"),
    )
    # Quantize to 2 decimals to avoid floating-point artifacts
    _incoming_temperature_q = (
        None if _incoming_temperature is None else round(_incoming_temperature, 2)
    )

    # Base debounce (seconds) for normal devices; anti-flicker lets us go down to 5s
    # here. HomematicIP still gets a higher interval (600s) below.
    _time_diff = 5
    # Significance threshold: 0.11°C (to filter out 0.1°C noise).
    # We ignore the tolerance setting here so we keep getting precise sensor
    # updates even with a larger control tolerance.
    _sig_threshold = 0.11

    try:
        for trv in self.all_trvs:
            if trv["advanced"][CONF_HOMEMATICIP]:
                _time_diff = 600
    except KeyError, TypeError:
        pass

    # First-run guard: seed the timestamp far enough in the past that the
    # first real update clears the debounce interval finalized above (setting
    # it to "now" would make the age zero and fail the interval check).
    if self.last_external_sensor_change is None:
        self.last_external_sensor_change = dt_util.now() - timedelta(
            seconds=_time_diff + 1
        )

    if not is_reasonable_temperature(_incoming_temperature_q):
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
            issue_id=f"invalid_external_temperature_{self.device_name}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="invalid_external_temperature",
            translation_placeholders={
                "name": self.device_name,
                "value": str(new_state.state),
            },
        )
        return

    _now = dt_util.now()
    try:
        _age = (_now - self.last_external_sensor_change).total_seconds()
    except TypeError, AttributeError:  # defensive, should not happen
        _age = 999999
    # Rounded comparison values
    _cur_q = None if self.cur_temp is None else round(self.cur_temp, 2)
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
            if self.pending_temp != _incoming_temperature_q:
                self.pending_temp = _incoming_temperature_q
                self.pending_since = dt_util.now()
                # Cancel existing timer if pending value changes
                if getattr(self, "plateau_timer_cancel", None) is not None:
                    self.plateau_timer_cancel()
                    self.plateau_timer_cancel = None
        # no change (value back to current): reset pending/timer
        elif self.pending_temp is not None:
            self.pending_temp = None
            self.pending_since = None
            if getattr(self, "plateau_timer_cancel", None) is not None:
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
        and self.pending_temp is not None
        and self.pending_temp != _cur_q
        and self.pending_since is not None
    ):
        _plateau_age = (dt_util.now() - self.pending_since).total_seconds()
        _plateau_ok = _plateau_age >= PLATEAU_ACCEPT_WINDOW and _interval_ok

        # Schedule timer if not already scheduled
        if not _plateau_ok and getattr(self, "plateau_timer_cancel", None) is None:
            remaining = max(0.1, PLATEAU_ACCEPT_WINDOW - _plateau_age)

            async def _plateau_cb(_now):
                self.plateau_timer_cancel = None
                # Re-check debounce interval so HomematicIP 600s is respected
                _cb_age = (
                    (dt_util.now() - self.last_external_sensor_change).total_seconds()
                    if self.last_external_sensor_change is not None
                    else 999999
                )
                _cb_interval_ok = _cb_age > _time_diff
                if self.pending_temp is not None and _cb_interval_ok:
                    _LOGGER.debug(
                        "better_thermostat %s: external_temperature plateau auto-accepted (value=%.2f)",
                        self.device_name,
                        self.pending_temp,
                    )
                    await _apply_temperature_update(self, self.pending_temp)

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
        await _apply_temperature_update(self, _incoming_temperature_q)
    else:
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
                f"{self.pending_temp:.2f}"
                if isinstance(self.pending_temp, (int, float))
                else None
            ),
            (
                f"{(dt_util.now() - self.pending_since).total_seconds():.1f}"
                if self.pending_since is not None
                else None
            ),
        )
