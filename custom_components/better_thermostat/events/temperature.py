import logging

from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP
from custom_components.better_thermostat.utils.helpers import convert_to_float
from datetime import datetime
from homeassistant.helpers import issue_registry as ir

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

# Anti-Flicker Konfiguration: Rücksprung auf den vorherigen stabilen Wert
# wird für dieses Zeitfenster (Sekunden) ignoriert.
FLICKER_REVERT_WINDOW = 45  # kann später optional konfigurierbar gemacht werden


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

    # Attribute für Anti-Flicker initialisieren, falls noch nicht vorhanden
    if not hasattr(self, "prev_stable_temp"):
        self.prev_stable_temp = None  # letzter stabiler (vor-dem-Sprung) Wert

    # Sicherstellen, dass Zeitstempel existiert (sonst erster Durchlauf)
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

    if _incoming_temperature is None or _incoming_temperature < -50:
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
    _diff = None if self.cur_temp is None else abs(
        _incoming_temperature - self.cur_temp)
    _is_significant = self.cur_temp is None or (
        _diff is not None and _diff >= _sig_threshold)
    _interval_ok = _age > _time_diff

    # Anti-Flicker: Wenn der neue Wert exakt dem vorherigen stabilen Wert entspricht
    # (also ein schneller Rücksprung) UND wir kürzlich erst umgestellt haben,
    # dann ignorieren wir diesen Rücksprung bis das Fenster abläuft.
    if (
        self.cur_temp is not None
        and self.prev_stable_temp is not None
        and _incoming_temperature == self.prev_stable_temp
        and _incoming_temperature != self.cur_temp
        and _age < FLICKER_REVERT_WINDOW
    ):
        _LOGGER.debug(
            "better_thermostat %s: external_temperature flicker revert ignored (current=%s revert=%s age=%.1fs < %ss)",
            self.device_name,
            self.cur_temp,
            _incoming_temperature,
            _age,
            FLICKER_REVERT_WINDOW,
        )
        return

    if _is_significant and (_interval_ok or (_diff is not None and _diff >= (_sig_threshold * 2))):
        # Verarbeite sofort, wenn Intervall abgelaufen ODER Änderung sehr groß
        _LOGGER.debug(
            "better_thermostat %s: external_temperature update accepted (old=%s new=%s diff=%s age=%.1fs threshold=%.2f interval=%ss)",
            self.device_name,
            self.cur_temp,
            _incoming_temperature,
            _diff,
            _age,
            _sig_threshold,
            _time_diff,
        )
        # Vor Übernahme des neuen Werts den bisherigen als stabilen Vormesswert merken
        if self.cur_temp is not None and self.cur_temp != _incoming_temperature:
            self.prev_stable_temp = self.cur_temp
        self.cur_temp = _incoming_temperature
        self.last_external_sensor_change = _now
        self.async_write_ha_state()
        # In die Steuer-Queue stellen
        if self.control_queue_task is not None:
            await self.control_queue_task.put(self)
    else:
        _LOGGER.debug(
            "better_thermostat %s: external_temperature ignored (old=%s new=%s diff=%s age=%.1fs sig=%s interval_ok=%s threshold=%.2f)",
            self.device_name,
            self.cur_temp,
            _incoming_temperature,
            _diff,
            _age,
            _is_significant,
            _interval_ok,
            _sig_threshold,
        )
