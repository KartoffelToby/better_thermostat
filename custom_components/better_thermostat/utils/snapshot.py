"""Shell-side builder for the core WorldSnapshot.

``build_snapshot`` is the single seam where entity attributes and Home
Assistant states are read and condensed into the immutable
:class:`~..core.snapshot.WorldSnapshot` consumed by the control path.
"""

from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..calibration import _get_current_outdoor_temp, _get_solar_context
from ..core.snapshot import TrvReported, WorldSnapshot, parse_hvac_mode
from .helpers import convert_to_float


def _as_float(self, value) -> float | None:
    """Normalize one observation via the shared converter.

    The 0.01-step rounding rule lives in ``convert_to_float``; the
    snapshot must carry the same numbers the rest of BT computes with.
    """
    return convert_to_float(value, self.device_name, "build_snapshot()")


def _build_trv_reported(self, entity_id: str, trv) -> TrvReported:
    """Condense one ``real_trvs`` entry (a Trv) into a TrvReported."""
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
        hvac_mode=parse_hvac_mode(trv.hvac_mode),
        current_temp=_as_float(self, trv.current_temperature),
        setpoint=_as_float(self, trv.last_temperature),
        min_temp=_as_float(self, trv.min_temp),
        max_temp=_as_float(self, trv.max_temp),
        valve_max_opening=_as_float(self, trv.valve_max_opening),
        local_calibration_min=_as_float(self, trv.local_calibration_min),
        local_calibration_max=_as_float(self, trv.local_calibration_max),
    )


def _raw_window_open(self) -> bool | None:
    """Read the raw window-sensor state (None: no sensor configured)."""
    window_id = getattr(self, "window_id", None)
    if not window_id or self.hass is None:
        return None
    state = self.hass.states.get(window_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return state.state not in ("off", "false", "closed")


def build_snapshot(self) -> WorldSnapshot:
    """Build the immutable world snapshot for one control cycle.

    ``self`` is the BetterThermostat entity; this function is the only
    place that flattens its attributes into the core snapshot type.

    Parameters
    ----------
    self :
        BetterThermostat entity instance.

    Returns
    -------
    WorldSnapshot
        Immutable observation used by the core control cycle.
    """
    trvs = {
        entity_id: _build_trv_reported(self, entity_id, trv)
        for entity_id, trv in self.real_trvs.items()
    }

    is_day, solar_intensity = _get_solar_context(self)

    return WorldSnapshot(
        now=self.clock.now(),
        now_monotonic=self.clock.monotonic(),
        target_temp=_as_float(self, self.bt_target_temp),
        target_cooltemp=_as_float(self, self.bt_target_cooltemp),
        hvac_mode=parse_hvac_mode(self.bt_hvac_mode),
        room_temp=_as_float(self, self.cur_temp),
        room_temp_filtered=_as_float(self, self.cur_temp_filtered),
        temp_slope=_as_float(self, self.temp_slope),
        call_for_heat=bool(self.call_for_heat),
        window_open=_raw_window_open(self),
        preset_mode=self.preset_mode,
        tolerance=_as_float(self, self.tolerance) or 0.0,
        outdoor_temp=_get_current_outdoor_temp(self),
        is_day=is_day,
        solar_intensity=solar_intensity,
        min_temp=_as_float(self, self.bt_min_temp),
        max_temp=_as_float(self, self.bt_max_temp),
        trvs=trvs,
    )
