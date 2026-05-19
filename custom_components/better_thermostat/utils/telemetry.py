"""Telemetry helpers for `extra_state_attributes`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import logging
from typing import Any, Literal, Protocol, TypedDict, cast

from custom_components.better_thermostat.utils.calibration.pid import PIDDebugInfo
from custom_components.better_thermostat.utils.const import ATTR_STATE_HEAT_LOSS_STATS

_LOGGER = logging.getLogger(__name__)


class CalibrationBalance(TypedDict, total=False):
    """Shape of ``trv_state['calibration_balance']`` written by calibration.py.

    ``debug`` is ``PIDDebugInfo`` for PID mode and other shapes for MPC/TPI;
    consumers must check ``debug['mode']`` before narrowing.
    """

    valve_percent: float
    apply_valve: bool
    debug: Mapping[str, object]


class TrvInfo(TypedDict, total=False):
    """Subset of ``real_trvs[entity_id]`` fields consumed by telemetry."""

    model: str
    calibration_balance: CalibrationBalance | None


class TelemetrySource(Protocol):
    """Structural contract for objects telemetry helpers consume."""

    real_trvs: Mapping[str, TrvInfo]
    heating_cycles: Sequence[Mapping[str, object]] | None
    loss_cycles: Sequence[Mapping[str, object]] | None
    last_heat_loss_stats: Sequence[object] | None
    heating_power_normalized: float | None
    temp_slope: float | None


def _to_float(val: object) -> float | None:
    """Best-effort float cast for telemetry values; no rounding."""
    match val:
        case bool():
            return None
        case int() | float():
            return float(val)
        case str():
            try:
                return float(val)
            except ValueError:
                return None
        case _:
            return None


def _serialize_cycles(
    cycles: Sequence[Mapping[str, object]] | None,
    count_key: str,
    last_key: str,
    label: str,
) -> dict[str, Any]:
    """Serialize a cycle sequence to a count + last-entry JSON dict."""
    if not cycles:
        return {}
    try:
        return {count_key: len(cycles), last_key: json.dumps(cycles[-1])}
    except (TypeError, ValueError):
        _LOGGER.exception("Error while serializing %s", label)
        return {}


def collect_cycle_telemetry(bt: TelemetrySource) -> dict[str, Any]:
    """Heating/loss cycle counts, last-cycle JSON, heat-loss stats, normalized power."""
    out: dict[str, Any] = {}

    out.update(
        _serialize_cycles(
            bt.heating_cycles,
            "heating_cycle_count",
            "heating_cycle_last",
            "heating cycle telemetry",
        )
    )
    out.update(
        _serialize_cycles(
            bt.loss_cycles,
            "heat_loss_cycle_count",
            "heat_loss_cycle_last",
            "heat loss telemetry",
        )
    )

    if bt.last_heat_loss_stats:
        try:
            out[ATTR_STATE_HEAT_LOSS_STATS] = json.dumps(list(bt.last_heat_loss_stats))
        except (TypeError, ValueError):
            _LOGGER.exception("Error while serializing heat loss stats")

    out["heating_power_norm"] = bt.heating_power_normalized

    return out


def collect_balance_attrs(bt: TelemetrySource) -> dict[str, Any]:
    """Temperature slope plus a compact per-TRV calibration balance summary."""
    out: dict[str, Any] = {}

    if bt.temp_slope is not None:
        out["temp_slope_K_min"] = round(bt.temp_slope, 4)

    bal_compact: dict[str, dict[str, float | None]] = {}
    for trv, info in bt.real_trvs.items():
        bal = info.get("calibration_balance")
        if bal is None:
            continue
        bal_compact[trv] = {"valve%": bal.get("valve_percent")}
    if bal_compact:
        out["calibration_balance"] = json.dumps(bal_compact)

    return out


type PIDScalarKey = Literal[
    "e_K", "p", "i", "d", "u", "kp", "ki", "kd", "meas_smooth_C", "dt_s"
]

# (PIDDebugInfo key, output key, decimals).
_PID_SCALAR_FIELDS: tuple[tuple[PIDScalarKey, str, int], ...] = (
    ("e_K", "pid_e_K", 4),
    ("p", "pid_P", 4),
    ("i", "pid_I", 4),
    ("d", "pid_D", 4),
    ("u", "pid_u", 4),
    ("kp", "pid_kp", 6),
    ("ki", "pid_ki", 6),
    ("kd", "pid_kd", 6),
    ("meas_smooth_C", "pid_meas_smooth_C", 3),
    ("dt_s", "pid_dt_s", 3),
)


def _pick_representative_trv(real_trvs: Mapping[str, TrvInfo]) -> str | None:
    """Prefer a sonoff/trvzb TRV; else first key."""
    for trv_id, info in real_trvs.items():
        model = (info.get("model") or "").lower()
        if "sonoff" in model or "trvzb" in model:
            return trv_id
    return next(iter(real_trvs), None)


def _extract_pid_debug(info: TrvInfo | None) -> PIDDebugInfo | None:
    """Return PID debug payload when the TRV's calibration is in PID mode."""
    if info is None:
        return None
    bal = info.get("calibration_balance")
    if bal is None:
        return None
    debug = bal.get("debug")
    if not isinstance(debug, Mapping):
        return None
    if str(debug.get("mode")).lower() != "pid":
        return None
    return cast(PIDDebugInfo, debug)


def collect_pid_debug_attrs(bt: TelemetrySource) -> dict[str, Any]:
    """Flatten PID controller debug from a representative TRV's calibration_balance."""
    out: dict[str, Any] = {}

    rep = _pick_representative_trv(bt.real_trvs)
    if rep is None:
        return out

    pid = _extract_pid_debug(bt.real_trvs.get(rep))
    if pid is None:
        return out

    for src_key, dst_key, decimals in _PID_SCALAR_FIELDS:
        if (value := _to_float(pid.get(src_key))) is not None:
            out[dst_key] = round(value, decimals)

    # d_meas_per_s is K/s; expose as K/min for readability
    if (d_per_s := _to_float(pid.get("d_meas_per_s"))) is not None:
        out["pid_d_meas_K_per_min"] = round(d_per_s * 60.0, 4)

    return out
