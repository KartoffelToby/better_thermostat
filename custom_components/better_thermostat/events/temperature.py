import logging

from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP
from custom_components.better_thermostat.utils.helpers import convert_to_float
from datetime import datetime
from time import monotonic
from homeassistant.helpers import issue_registry as ir

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)


# is ignored for this time window (seconds).
FLICKER_REVERT_WINDOW = 45  # can optionally be made configurable later


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

    # Initialize anti-flicker attributes on first run
    if not hasattr(self, "prev_stable_temp"):
        self.prev_stable_temp = None  # letzter stabiler (vor-dem-Sprung) Wert
    if not hasattr(self, "last_change_direction"):
        # +1 = steigend, -1 = fallend, 0 = unbekannt/gleich
        self.last_change_direction = 0

    # Ensure timestamp exists (first run guard)
    if getattr(self, "last_external_sensor_change", None) is None:
        # Setze einen alten Zeitpunkt, damit erste Änderung akzeptiert wird
        self.last_external_sensor_change = datetime.now()

    # Basis-Debounce (Sekunden) für normale Geräte; durch Anti-Flicker können wir hier auf 5s runter
    # gesetzt werden. HomematicIP erhält unten weiterhin ein höheres Intervall (600s).
    _time_diff = 5
    # Signifikanz-Schwelle: halbe Toleranz oder mindestens 0.1°C
    try:
        _sig_threshold = max(0.1, (getattr(self, "tolerance", 0.0) or 0.0) / 2.0)
    except (TypeError, ValueError):
        _sig_threshold = 0.1

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
    _sig_threshold_q = round(_sig_threshold, 2)
    _is_significant = _cur_q is None or (
        _diff is not None and _diff >= _sig_threshold_q
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
    _last_dir = getattr(self, "last_change_direction", 0)
    _block_flip_small = (
        _dir_now != 0
        and _last_dir != 0
        and _dir_now != _last_dir
        and _diff is not None
        and _diff <= _sig_threshold_q
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
    try:
        now_m = monotonic()
        _last_ts = getattr(self, "_slope_last_ts", None)
        if _last_ts is not None and _cur_q is not None:
            dt_min = max(1e-6, (now_m - _last_ts) / 60.0)
            dT = _incoming_temperature_q - _cur_q
            inst_slope = dT / dt_min
            # light smoothing
            if getattr(self, "temp_slope", None) is None:
                self.temp_slope = inst_slope
            else:
                self.temp_slope = 0.7 * self.temp_slope + 0.3 * inst_slope
        setattr(self, "_slope_last_ts", now_m)
    except (AttributeError, TypeError, ZeroDivisionError):
        pass

    if _is_significant and (
        _interval_ok or (_diff is not None and _diff >= _sig_threshold_q)
    ):
        # Verarbeite sofort, wenn Intervall abgelaufen ODER Änderung sehr groß
        _LOGGER.debug(
            "better_thermostat %s: external_temperature update accepted (old=%.2f new=%.2f diff=%.2f age=%.1fs threshold=%.2f interval=%ss)",
            self.device_name,
            (_cur_q if _cur_q is not None else float("nan")),
            _incoming_temperature_q,
            (_diff if _diff is not None else float("nan")),
            _age,
            _sig_threshold_q,
            _time_diff,
        )

        # Remember previous value as stable pre-measure before updating
        if _cur_q is not None and _cur_q != _incoming_temperature_q:
            self.prev_stable_temp = _cur_q
        # Richtung merken (nur bei echter Änderung)
        if _cur_q is not None:
            if _incoming_temperature_q > _cur_q:
                self.last_change_direction = 1
            elif _incoming_temperature_q < _cur_q:
                self.last_change_direction = -1
        self.cur_temp = _incoming_temperature_q
        self.last_external_sensor_change = _now
        self.async_write_ha_state()
        # Schreibe den von BT verwendeten Wert (self.cur_temp) ins TRV
        try:
            # Verwende die bekannten TRV-IDs aus real_trvs (Keys)
            trv_ids = list(getattr(self, "real_trvs", {}).keys())
            if not trv_ids and hasattr(self, "entity_ids"):
                trv_ids = list(getattr(self, "entity_ids", []) or [])
            if not trv_ids and hasattr(self, "heater_entity_id"):
                trv_ids = [self.heater_entity_id]
            for trv_id in trv_ids:
                quirks = (
                    self.real_trvs.get(trv_id, {}).get("model_quirks")
                    if hasattr(self, "real_trvs")
                    else None
                )
                if quirks and hasattr(quirks, "maybe_set_external_temperature"):
                    await quirks.maybe_set_external_temperature(
                        self, trv_id, self.cur_temp
                    )
                else:
                    _LOGGER.debug(
                        "better_thermostat %s: no quirks with maybe_set_external_temperature for %s",
                        getattr(self, "device_name", "unknown"),
                        trv_id,
                    )
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
            _LOGGER.debug(
                "better_thermostat %s: external_temperature write to TRV failed (non critical)",
                getattr(self, "device_name", "unknown"),
            )
        # Enqueue control action
        if self.control_queue_task is not None:
            await self.control_queue_task.put(self)
    else:
        _LOGGER.debug(
            "better_thermostat %s: external_temperature ignored (old=%.2f new=%.2f diff=%s age=%.1fs sig=%s interval_ok=%s threshold=%.2f)",
            self.device_name,
            (_cur_q if _cur_q is not None else float("nan")),
            _incoming_temperature_q,
            (f"{_diff:.2f}" if _diff is not None else "None"),
            _age,
            _is_significant,
            _interval_ok,
            _sig_threshold_q,
        )
