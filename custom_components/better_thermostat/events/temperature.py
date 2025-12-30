"""External temperature event handlers for Better Thermostat.

This module includes logic to handle external temperature updates and apply
debounce, anti-flicker, accumulation, and plateau acceptance heuristics used
to make robust decisions about whether the external temperature should be
propagated to the target devices.
"""

import logging
import math

from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP
from custom_components.better_thermostat.utils.helpers import convert_to_float
from datetime import datetime
from time import monotonic
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)


# is ignored for this time window (seconds).
FLICKER_REVERT_WINDOW = 45  # can optionally be made configurable later
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
    # Richtung merken (nur bei echter Änderung)
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
    except Exception:
        pass
    _ema = self.external_temp_ema
    self.last_external_sensor_change = datetime.now()
    # Reset accumulation & pending after accept
    self.accum_delta = 0.0
    self.accum_dir = 0
    self.accum_since = datetime.now()
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
    # Schreibe den von BT verwendeten Wert (self.cur_temp) ins TRV
    try:
        trv_ids = list(self.real_trvs.keys())
        if not trv_ids and hasattr(self, "entity_ids"):
            trv_ids = list(self.entity_ids or [])
        if not trv_ids and hasattr(self, "heater_entity_id"):
            trv_ids = [self.heater_entity_id]
        for trv_id in trv_ids:
            quirks = (
                self.real_trvs.get(trv_id, {}).get("model_quirks")
                if hasattr(self, "real_trvs")
                else None
            )
            if quirks and hasattr(quirks, "maybe_set_external_temperature"):
                await quirks.maybe_set_external_temperature(self, trv_id, self.cur_temp)
            else:
                _LOGGER.debug(
                    "better_thermostat %s: no quirks with maybe_set_external_temperature for %s",
                    self.device_name,
                    trv_id,
                )
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        _LOGGER.debug(
            "better_thermostat %s: external_temperature write to TRV failed (non critical)",
            self.device_name,
        )
    # Enqueue control action
    if self.control_queue_task is not None:
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

    _incoming_temperature = convert_to_float(
        str(new_state.state), self.device_name, "external_temperature"
    )
    # Quantisiere auf 2 Dezimalstellen, um FP-Artefakte zu vermeiden
    _incoming_temperature_q = (
        None if _incoming_temperature is None else round(_incoming_temperature, 2)
    )

    # Ensure timestamp exists (first run guard)
    if self.last_external_sensor_change is None:
        # Setze einen alten Zeitpunkt, damit erste Änderung akzeptiert wird
        self.last_external_sensor_change = datetime.now()

    # Basis-Debounce (Sekunden) für normale Geräte; durch Anti-Flicker können wir hier auf 5s runter
    # gesetzt werden. HomematicIP erhält unten weiterhin ein höheres Intervall (600s).
    _time_diff = 5
    # Signifikanz-Schwelle: 0.11°C (um 0.1°C Rauschen zu filtern).
    # Wir ignorieren die Toleranz-Einstellung hier, um auch bei größerer Regel-Toleranz
    # präzise Sensor-Updates zu erhalten.
    _sig_threshold = 0.0

    try:
        for trv in self.all_trvs:
            if trv["advanced"][CONF_HOMEMATICIP]:
                _time_diff = 600
    except KeyError:
        pass

    if _incoming_temperature_q is None or _incoming_temperature_q < -50:
        # raise a ha repair notication
        _LOGGER.error(
            "better_thermostat %s: external_temperature is not a valid number: %s",
            self.device_name,
            new_state.state,
        )
        # Minimal kompatibler Aufruf (Parameter-Namen angepasst an aktuelle HA API)
        ir.async_create_issue(
            hass=self.hass,
            domain="better_thermostat",
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

    _now = datetime.now()
    try:
        _age = (_now - self.last_external_sensor_change).total_seconds()
    except (TypeError, AttributeError):  # defensiv, sollte nicht auftreten
        _age = 999999
    # Gerundete Vergleichswerte
    _cur_q = None if self.cur_temp is None else round(self.cur_temp, 2)
    _diff = None if _cur_q is None else abs(_incoming_temperature_q - _cur_q)
    # Quantisierte Differenz zur robusten Schwellenprüfung (vermeidet 0.099999-Fehler)
    _diff_q = None if _diff is None else round(_diff, 2)
    _sig_threshold_q = round(_sig_threshold, 2)
    _is_significant = _cur_q is None or (
        _diff_q is not None and _diff_q >= _sig_threshold_q
    )
    _interval_ok = _age > _time_diff

    # Anti-Flicker: Wenn der neue Wert exakt dem vorherigen stabilen Wert entspricht
    # (also ein schneller Rücksprung) UND wir kürzlich erst umgestellt haben,
    # dann ignorieren wir diesen Rücksprung bis das Fenster abläuft.
    if (
        _cur_q is not None
        and self.prev_stable_temp is not None
        and _incoming_temperature_q == round(self.prev_stable_temp, 2)
        and _incoming_temperature_q != _cur_q
        and _age < FLICKER_REVERT_WINDOW
    ):
        # Plane eine Übernahme nach Ablauf des Revert-Fensters, falls der Sensorwert stabil bleibt
        try:
            remaining = max(0.0, float(FLICKER_REVERT_WINDOW) - float(_age))
        except (ValueError, TypeError):
            remaining = float(FLICKER_REVERT_WINDOW)
        # Merke Kandidatenwert und cancel ggf. vorherige Planung
        cancel_cb = self.flicker_unignore_cancel
        if callable(cancel_cb):
            cancel_cb()
        self.flicker_unignore_cancel = None
        self.flicker_candidate = _incoming_temperature_q

        def _deadline_cb(_now):  # executed by HA loop, schedule async body
            async def _apply_if_stable():
                try:
                    # Prüfe aktuellen Sensor-Status
                    sensor_id = self.sensor_entity_id
                    state = self.hass.states.get(sensor_id) if sensor_id else None
                    if state is None or state.state in (
                        STATE_UNAVAILABLE,
                        STATE_UNKNOWN,
                        None,
                    ):
                        return
                    _val = convert_to_float(
                        str(state.state), self.device_name, "external_temperature"
                    )
                    _val_q = None if _val is None else round(_val, 2)
                    cand = self.flicker_candidate
                    # Übernehme nur, wenn Kandidatwert unverändert und ungleich cur_temp ist
                    if _val_q is not None and cand is not None and _val_q == cand:
                        if _val_q != self.cur_temp:
                            _LOGGER.debug(
                                "better_thermostat %s: external_temperature flicker revert auto-accepted after %ss (value=%.2f)",
                                self.device_name,
                                FLICKER_REVERT_WINDOW,
                                _val_q,
                            )
                            # Akzeptiere Wert wie im normalen Pfad
                            _prev = self.cur_temp
                            if _prev is not None and _prev != _val_q:
                                self.prev_stable_temp = _prev
                                if _val_q > _prev:
                                    self.last_change_direction = 1
                                elif _val_q < _prev:
                                    self.last_change_direction = -1
                            self.cur_temp = _val_q
                            try:
                                _update_external_temp_ema(self, float(_val_q))
                            except Exception:
                                _LOGGER.debug(
                                    "better_thermostat %s: external_temperature EMA update failed (non critical)",
                                    self.device_name,
                                )
                            self.last_external_sensor_change = datetime.now()
                            # Reset Anti-Flicker-Akkumulatoren
                            self.accum_delta = 0.0
                            self.accum_dir = 0
                            self.accum_since = datetime.now()
                            self.pending_temp = None
                            self.pending_since = None
                            self.async_write_ha_state()
                            # Schreibe TRV-External-Temp über Quirks, falls vorhanden
                            try:
                                trv_ids = list(self.real_trvs.keys())
                                if not trv_ids and hasattr(self, "entity_ids"):
                                    trv_ids = list(self.entity_ids or [])
                                if not trv_ids and hasattr(self, "heater_entity_id"):
                                    trv_ids = [self.heater_entity_id]
                                for trv_id in trv_ids:
                                    quirks = (
                                        self.real_trvs.get(trv_id, {}).get(
                                            "model_quirks"
                                        )
                                        if hasattr(self, "real_trvs")
                                        else None
                                    )
                                    if quirks and hasattr(
                                        quirks, "maybe_set_external_temperature"
                                    ):
                                        await quirks.maybe_set_external_temperature(
                                            self, trv_id, self.cur_temp
                                        )
                            except (
                                AttributeError,
                                KeyError,
                                TypeError,
                                ValueError,
                                RuntimeError,
                            ):
                                _LOGGER.debug(
                                    "better_thermostat %s: external_temperature write to TRV failed (non critical)",
                                    self.device_name,
                                )
                            if self.control_queue_task is not None:
                                await self.control_queue_task.put(self)
                finally:
                    # Aufräumen
                    self.flicker_unignore_cancel = None
                    self.flicker_candidate = None

            # schedule the async part
            self.hass.loop.create_task(_apply_if_stable())

        self.flicker_unignore_cancel = async_call_later(
            self.hass, remaining + 0.1, _deadline_cb
        )
        _LOGGER.debug(
            "better_thermostat %s: external_temperature flicker revert ignored (current=%.2f revert=%.2f age=%.1fs < %ss)",
            self.device_name,
            _cur_q,
            _incoming_temperature_q,
            _age,
            FLICKER_REVERT_WINDOW,
        )
        return

    # Richtungswechsel-Schutz: kleine Gegenbewegungen (<= Schwellwert) innerhalb des Flicker-Fensters ignorieren,
    # um Ping-Pong um genau 0.10°C zu vermeiden.
    _dir_now = 0
    if _cur_q is not None:
        if _incoming_temperature_q > _cur_q:
            _dir_now = 1
        elif _incoming_temperature_q < _cur_q:
            _dir_now = -1
    _last_dir = self.last_change_direction
    _block_flip_small = (
        _dir_now != 0
        and _last_dir != 0
        and _dir_now != _last_dir
        and _diff_q is not None
        and _diff_q <= _sig_threshold_q
        and _age < FLICKER_REVERT_WINDOW
    )

    if _block_flip_small:
        _LOGGER.debug(
            "better_thermostat %s: external_temperature opposite-direction change ignored (current=%.2f new=%.2f diff=%.2f age=%.1fs <= %ss threshold=%.2f)",
            self.device_name,
            (_cur_q if _cur_q is not None else float("nan")),
            _incoming_temperature_q,
            (_diff if _diff is not None else float("nan")),
            _age,
            FLICKER_REVERT_WINDOW,
            _sig_threshold_q,
        )
        return

    # Slope calculation (simple delta per minute)
    # Disabled in favor of periodic EMA-based slope calculation in climate.py
    # try:
    #     now_m = monotonic()
    #     _last_ts = getattr(self, "_slope_last_ts", None)
    #     if _last_ts is not None and _cur_q is not None:
    #         dt_min = max(1e-6, (now_m - _last_ts) / 60.0)
    #         dT = _incoming_temperature_q - _cur_q  # K
    #         inst_slope = dT / dt_min  # K/min
    #         # light smoothing
    #         if getattr(self, "temp_slope", None) is None:
    #             self.temp_slope = inst_slope
    #         else:
    #             self.temp_slope = 0.7 * self.temp_slope + 0.3 * inst_slope
    #     setattr(self, "_slope_last_ts", now_m)
    # except (AttributeError, TypeError, ZeroDivisionError):
    #     pass

    # Accumulation of small changes in the same direction
    _accept_reason = None
    if _cur_q is not None:
        _signed_delta = round(_incoming_temperature_q - _cur_q, 2)
        if _signed_delta != 0:
            # set direction from sign
            _acc_dir_now = 1 if _signed_delta > 0 else -1
            if self.accum_dir == 0 or self.accum_dir == _acc_dir_now:
                self.accum_delta = round(self.accum_delta + _signed_delta, 2)
                self.accum_dir = _acc_dir_now if self.accum_dir == 0 else self.accum_dir
            else:
                # direction flipped: reset accumulation to current delta
                self.accum_delta = _signed_delta
                self.accum_dir = _acc_dir_now
                self.accum_since = datetime.now()
            # Plateau tracking
            if self.pending_temp != _incoming_temperature_q:
                self.pending_temp = _incoming_temperature_q
                self.pending_since = datetime.now()
                # Cancel existing timer if pending value changes
                if getattr(self, "plateau_timer_cancel", None) is not None:
                    self.plateau_timer_cancel()
                    self.plateau_timer_cancel = None
        else:
            # no change (value back to current): reset pending/timer
            if self.pending_temp is not None:
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
        _plateau_age = (datetime.now() - self.pending_since).total_seconds()
        _plateau_ok = _plateau_age >= PLATEAU_ACCEPT_WINDOW and _interval_ok

        # Schedule timer if not already scheduled
        if not _plateau_ok and getattr(self, "plateau_timer_cancel", None) is None:
            remaining = max(0.1, PLATEAU_ACCEPT_WINDOW - _plateau_age)

            async def _plateau_cb(_now):
                self.plateau_timer_cancel = None
                if self.pending_temp is not None:
                    _LOGGER.debug(
                        "better_thermostat %s: external_temperature plateau auto-accepted (value=%.2f)",
                        self.device_name,
                        self.pending_temp,
                    )
                    await _apply_temperature_update(self, self.pending_temp)

            self.plateau_timer_cancel = async_call_later(
                self.hass, remaining, _plateau_cb
            )

    if _is_significant and (
        _interval_ok or (_diff_q is not None and _diff_q >= _sig_threshold_q)
    ):
        _accept_reason = "significant"
    elif _accum_ok:
        _accept_reason = "accumulated"
    elif _plateau_ok:
        _accept_reason = "plateau"

    if _accept_reason is not None:
        # Verarbeite sofort, wenn Intervall abgelaufen ODER Änderung sehr groß
        _LOGGER.debug(
            "better_thermostat %s: external_temperature update accepted (old=%.2f new=%.2f diff=%.2f age=%.1fs threshold=%.2f interval=%ss reason=%s accum=%.2f dir=%s)",
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
            "better_thermostat %s: external_temperature ignored (old=%.2f new=%.2f diff=%s age=%.1fs sig=%s interval_ok=%s threshold=%.2f accum=%.2f dir=%s pending=%s pending_age=%ss)",
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
                f"{(datetime.now() - self.pending_since).total_seconds():.1f}"
                if self.pending_since is not None
                else None
            ),
        )
