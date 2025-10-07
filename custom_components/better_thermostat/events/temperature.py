import logging

from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP
from custom_components.better_thermostat.utils.helpers import convert_to_float
from datetime import datetime
from homeassistant.helpers import issue_registry as ir

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)


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

    # Basis-Debounce (Sekunden) für normale Geräte; HomematicIP wird unten erhöht
    _time_diff = 60
    # Signifikanz-Schwelle: halbe Toleranz oder mindestens 0.1°C
    try:
        _sig_threshold = max(0.1, (self.tolerance or 0.0) / 2.0)
    except Exception:  # noqa: BLE001
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
    _age = (_now - self.last_external_sensor_change).total_seconds()
    _diff = None if self.cur_temp is None else abs(
        _incoming_temperature - self.cur_temp)
    _is_significant = self.cur_temp is None or (
        _diff is not None and _diff >= _sig_threshold)
    _interval_ok = _age > _time_diff

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
