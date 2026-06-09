"""Shell-side builder for the core WorldSnapshot.

``build_snapshot`` is the single seam where entity attributes and Home
Assistant states are read and condensed into the immutable
:class:`~..core.snapshot.WorldSnapshot` consumed by the control path.
"""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..calibration import _get_current_outdoor_temp, _get_current_solar_intensity
from ..core.snapshot import TrvReported, WorldSnapshot, parse_hvac_mode


def _as_float(value: object) -> float | None:
    """Best-effort float conversion; None for missing/unparseable values."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _build_trv_reported(self, entity_id: str, data: dict) -> TrvReported:
    """Condense one ``real_trvs`` entry into a TrvReported."""
    available = False
    if self.hass is not None:
        state = self.hass.states.get(entity_id)
        available = state is not None and state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        )
    return TrvReported(
        entity_id=entity_id,
        available=available,
        hvac_mode=parse_hvac_mode(data.get("hvac_mode")),
        current_temp=_as_float(data.get("current_temperature")),
        setpoint=_as_float(data.get("last_temperature")),
        min_temp=_as_float(data.get("min_temp")),
        max_temp=_as_float(data.get("max_temp")),
        valve_max_opening=_as_float(data.get("valve_max_opening")),
    )


def build_snapshot(self) -> WorldSnapshot:
    """Build the immutable world snapshot for one control cycle.

    ``self`` is the BetterThermostat entity; this function is the only
    place that flattens its attributes into the core snapshot type.
    """
    trvs = {
        entity_id: _build_trv_reported(self, entity_id, data or {})
        for entity_id, data in self.real_trvs.items()
    }

    is_day = True
    if self.hass is not None:
        sun = self.hass.states.get("sun.sun")
        if sun is not None and sun.state == "below_horizon":
            is_day = False
    solar_intensity = _get_current_solar_intensity(self) if is_day else 0.0

    return WorldSnapshot(
        now=self.clock.now(),
        now_monotonic=self.clock.monotonic(),
        target_temp=_as_float(self.bt_target_temp),
        target_cooltemp=_as_float(self.bt_target_cooltemp),
        hvac_mode=parse_hvac_mode(self.bt_hvac_mode),
        room_temp=_as_float(self.cur_temp),
        room_temp_filtered=_as_float(self.cur_temp_filtered),
        temp_slope=_as_float(self.temp_slope),
        window_open=self.window_open,
        call_for_heat=bool(self.call_for_heat),
        preset_mode=self.preset_mode,
        tolerance=_as_float(self.tolerance) or 0.0,
        outdoor_temp=_get_current_outdoor_temp(self),
        is_day=is_day,
        solar_intensity=solar_intensity,
        startup_running=bool(self.startup_running),
        in_maintenance=bool(self.in_maintenance),
        ignore_states=bool(self.ignore_states),
        degraded=bool(self.degraded_mode),
        min_temp=_as_float(self.bt_min_temp),
        max_temp=_as_float(self.bt_max_temp),
        trvs=trvs,
    )
