"""Shell-side builder for the core WorldSnapshot.

``build_snapshot`` is the single seam where entity attributes and Home
Assistant states are read and condensed into the immutable
:class:`~..core.snapshot.WorldSnapshot` consumed by the control path.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..calibration import _get_current_outdoor_temperature, _get_solar_context
from ..core.snapshot import TrvReported, WorldSnapshot, parse_hvac_mode, parse_preset
from ..model_fixes.model_quirks import (
    local_calibration_shifts_setpoint,
    trv_state_unknown_as_available,
)
from .const import CalibrationOutput
from .helpers import (
    configured_calibration_output,
    convert_to_float,
    defers_to_bound_sensor,
    round_by_step,
    state_is_live,
)

if TYPE_CHECKING:
    from ..climate import BetterThermostat
    from ..trv import Trv


def _as_float(self: BetterThermostat, value: str | float | None) -> float | None:
    """Normalize one observation via the shared converter.

    The 0.01-step rounding rule lives in ``convert_to_float``; the
    snapshot must carry the same numbers the rest of BT computes with.
    """
    return convert_to_float(value, self.device_name, "build_snapshot()")


def _raw_trv_temperature(
    self: BetterThermostat, entity_id: str, trv: Trv, current_temperature: float | None
) -> float | None:
    """Recover the TRV's unshifted internal sensor reading (None when unknown)."""
    if current_temperature is None:
        return None
    if self.hass is not None and defers_to_bound_sensor(self.hass, entity_id):
        return None
    calibration_output = trv.calibration or configured_calibration_output(trv.advanced)
    if calibration_output is not CalibrationOutput.LOCAL_BASED:
        return current_temperature
    if local_calibration_shifts_setpoint(self, entity_id):
        return current_temperature
    calibration_offset = _as_float(self, trv.last_calibration)
    if calibration_offset is None:
        return None
    return round_by_step(current_temperature - calibration_offset, 0.01)


def _trv_valve_percent(self: BetterThermostat, trv: Trv) -> float | None:
    """Return the TRV's confirmed valve opening in ``[0, 100]``, or ``None``."""
    for candidate in (trv.valve_position, trv.last_valve_percent):
        percent = _as_float(self, candidate)
        if percent is not None and 0.0 <= percent <= 100.0:
            return percent
    return None


def _build_trv_reported(
    self: BetterThermostat, entity_id: str, trv: Trv
) -> TrvReported:
    """Condense one ``real_trvs`` entry (a Trv) into a TrvReported."""
    available = False
    if self.hass is not None:
        state = self.hass.states.get(entity_id)
        if state is not None:
            # Addressing drops an unavailable TRV, so a model that reports
            # ``unknown`` while it is being driven has to be read as
            # present here or it would never be written to again.
            available = state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN) or (
                state.state == STATE_UNKNOWN
                and trv_state_unknown_as_available(self, entity_id)
            )
    current_temperature = _as_float(self, trv.current_temperature)
    return TrvReported(
        entity_id=entity_id,
        available=available,
        released=entity_id in self.released_trvs,
        hvac_mode=parse_hvac_mode(trv.hvac_mode),
        current_temperature=current_temperature,
        setpoint=_as_float(self, trv.commanded_setpoint),
        min_temp=_as_float(self, trv.min_temp),
        max_temp=_as_float(self, trv.max_temp),
        valve_max_opening=_as_float(self, trv.valve_max_opening),
        min_local_calibration=_as_float(self, trv.min_local_calibration),
        max_local_calibration=_as_float(self, trv.max_local_calibration),
        raw_temperature=_raw_trv_temperature(self, entity_id, trv, current_temperature),
        valve_percent=_trv_valve_percent(self, trv),
    )


def _raw_window_open(self: BetterThermostat) -> bool | None:
    """Read the raw window-sensor state (None: no sensor configured)."""
    window_sensor_entity_id = self.window_sensor_entity_id
    if not window_sensor_entity_id or self.hass is None:
        return None
    state = self.hass.states.get(window_sensor_entity_id)
    if not state_is_live(state):
        return None
    return state.state not in ("off", "false", "closed")


def build_snapshot(self: BetterThermostat) -> WorldSnapshot:
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
    now_monotonic = self.clock.monotonic()
    room_temperature = _as_float(self, self.current_temperature)
    room_temperature_filtered = _as_float(self, self.room_temperature_filtered)
    temperature_slope = _as_float(self, self.temperature_slope)
    outdoor_temperature = _get_current_outdoor_temperature(self)
    window_open = _raw_window_open(self)

    # A TRV that startup has not initialised yet carries none of the values
    # a decision reads, and boost would address it even while unreachable,
    # so it is not part of the room until its initialisation has completed.
    # A released TRV is never addressed, so it stays part of the room: the
    # kernel reads from it that the room has a TRV released.
    raw_trvs = {
        entity_id: _build_trv_reported(self, entity_id, trv)
        for entity_id, trv in self.real_trvs.items()
        if not trv.awaiting_initialization or entity_id in self.released_trvs
    }
    thermal_evidence, trv_slopes = self.anomaly_evidence.observe_snapshot(
        now_monotonic=now_monotonic,
        room_temperature=room_temperature,
        room_temperature_filtered=room_temperature_filtered,
        outdoor_temperature=outdoor_temperature,
        temperature_slope_kelvin_per_min=temperature_slope,
        heat_loss_rate_kelvin_per_hour_per_kelvin=_as_float(self, self.heat_loss_rate),
        trv_raw_temperatures={
            entity_id: rep.raw_temperature for entity_id, rep in raw_trvs.items()
        },
        contact_open=bool(window_open),
    )
    trvs = {
        entity_id: replace(
            rep, raw_temperature_slope_kelvin_per_min=trv_slopes.get(entity_id)
        )
        for entity_id, rep in raw_trvs.items()
    }

    is_day, solar_intensity = _get_solar_context(self)

    return WorldSnapshot(
        now=self.clock.now(),
        now_monotonic=now_monotonic,
        # The 0.01 grid convert_to_float puts the room temperature on, so a
        # room resting on its target compares equal to it.
        heat_target_temperature=round_by_step(self.heat_target_temperature, 0.01),
        cool_target_temperature=_as_float(self, self.cool_target_temperature),
        hvac_mode=parse_hvac_mode(self.bt_hvac_mode),
        # A snapshot can be asked for before startup has measured the room
        # (the devices can be released while Home Assistant starts), so it
        # reads the value that is still None then.
        room_temperature=room_temperature,
        room_temperature_filtered=room_temperature_filtered,
        temperature_slope=temperature_slope,
        call_for_heat=self.call_for_heat,
        window_open=window_open,
        preset_mode=parse_preset(self.preset_mode),
        devices_released=self.devices_released,
        tolerance=_as_float(self, self.tolerance) or 0.0,
        outdoor_temperature=outdoor_temperature,
        is_day=is_day,
        solar_intensity=solar_intensity,
        min_temp=_as_float(self, self.bt_min_temp),
        max_temp=_as_float(self, self.bt_max_temp),
        trvs=trvs,
        thermal_evidence=thermal_evidence,
    )
