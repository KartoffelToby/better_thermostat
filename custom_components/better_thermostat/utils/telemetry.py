"""Telemetry helpers for `extra_state_attributes`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import logging
from typing import Literal, Protocol, TypedDict

from custom_components.better_thermostat.utils.calibration.mpc import MpcDebugInfo
from custom_components.better_thermostat.utils.calibration.pid import PIDDebugInfo
from custom_components.better_thermostat.utils.calibration.tpi import TpiDebugInfo
from custom_components.better_thermostat.utils.const import (
    ATTR_HEATING_POWER_NORMALIZED,
    ATTR_MPC_V2_COUPLING,
    ATTR_MPC_V2_DISTURBANCE,
    ATTR_MPC_V2_GROUP_VALVE,
    ATTR_MPC_V2_RADIATOR_TEMPERATURE,
    ATTR_MPC_V2_REID_TAU_ROOM,
    ATTR_MPC_V2_ROOM_TEMPERATURE,
    ATTR_MPC_V2_TAU_ROOM,
    ATTR_PID_DT,
    ATTR_PID_ERROR,
    ATTR_PID_MEASUREMENT_FILTERED,
    ATTR_PID_MEASUREMENT_SLOPE,
    ATTR_STATE_HEAT_LOSS_STATS,
    ATTR_STATE_TEMPERATURE_SLOPE,
    CalibrationMode,
)
from custom_components.better_thermostat.utils.thermal_learning import (
    HeatingCycle,
    LossCycle,
    LossStats,
)

_LOGGER = logging.getLogger(__name__)


class ValveCommand(TypedDict):
    """Valve intent handed to the valve writer.

    ``valve_percent`` is the device percentage to command; ``apply_valve``
    says whether the TRV takes a direct valve write at all.
    """

    valve_percent: float
    apply_valve: bool


class HeatingPowerDebugInfo(TypedDict):
    """Debug payload of the heating power valve intent."""

    source: Literal["heating_power_calibration"]


# The group valve of a multi-TRV room and this TRV's share of it. The keys
# are read as they are; the functional syntax keeps them as strings rather
# than identifiers.
GroupValveShare = TypedDict(  # noqa: UP013
    "GroupValveShare", {"group_valve_pct": float, "distributed_valve_pct": float}
)


class MpcBalanceDebugInfo(MpcDebugInfo, GroupValveShare):
    """MPC debug payload with the per-TRV share of the group valve."""


# MPC v2 debug payload: the controller diagnostics, the per-TRV share of the
# group valve and the adopted re-identification. The keys are read by the
# MPC v2 sensors and attributes, so they stay as they are; the functional
# syntax keeps them as strings rather than identifiers.
MpcV2BalanceDebugInfo = TypedDict(  # noqa: UP013
    "MpcV2BalanceDebugInfo",
    {
        "T_room_hat": float,
        "T_rad_hat": float,
        "D_hat_K_per_min": float,
        "tau_room_min": float,
        "coupling_rad_room": float,
        "group_valve_pct": float,
        "distributed_valve_pct": float,
        "controller_version": Literal["v2"],
        "reid_tau_room": float | None,
        "reid_gain": float | None,
    },
)


class HeatingPowerBalance(ValveCommand):
    """Valve intent of the heating power calibration."""

    controller: Literal[CalibrationMode.HEATING_POWER_CALIBRATION]
    debug: HeatingPowerDebugInfo


class MpcBalance(ValveCommand):
    """Valve intent of the MPC calibration."""

    controller: Literal[CalibrationMode.MPC_CALIBRATION]
    debug: MpcBalanceDebugInfo


class MpcV2Balance(ValveCommand):
    """Valve intent of the MPC v2 calibration."""

    controller: Literal[CalibrationMode.MPC_V2_CALIBRATION]
    debug: MpcV2BalanceDebugInfo


class TpiBalance(ValveCommand):
    """Valve intent of the TPI calibration."""

    controller: Literal[CalibrationMode.TPI_CALIBRATION]
    debug: TpiDebugInfo


class PidBalance(ValveCommand):
    """Valve intent of the PID calibration."""

    controller: Literal[CalibrationMode.PID_CALIBRATION]
    debug: PIDDebugInfo


# The ``calibration_balance`` a calibration writes for one TRV. ``controller``
# names the producer and selects the shape of ``debug``.
type CalibrationBalance = (
    HeatingPowerBalance | MpcBalance | MpcV2Balance | TpiBalance | PidBalance
)


class TrvInfo(Protocol):
    """Subset of the per-TRV object surface consumed by telemetry.

    ``Trv`` satisfies this structurally; ``calibration_balance`` carries a
    :class:`CalibrationBalance`-shaped mapping when set.
    """

    @property
    def model(self) -> str | None:
        """Detected TRV model, if known."""
        ...

    @property
    def calibration_balance(self) -> CalibrationBalance | None:
        """Last calibration balance result, if any."""
        ...


class TelemetrySource(Protocol):
    """Structural read-only contract for objects telemetry helpers consume.

    Members are declared as read-only properties: the helpers only read these,
    and read-only members match covariantly, so a source whose ``real_trvs`` is
    a plain ``dict`` still satisfies the contract.
    """

    @property
    def real_trvs(self) -> Mapping[str, TrvInfo]:
        """Per-TRV info objects, keyed by entity id."""
        ...

    @property
    def heating_cycles(self) -> Sequence[HeatingCycle] | None:
        """Finalized heating cycles (most recent last)."""
        ...

    @property
    def loss_cycles(self) -> Sequence[LossCycle] | None:
        """Finalized idle-cooling cycles (most recent last)."""
        ...

    @property
    def last_heat_loss_stats(self) -> Sequence[LossStats] | None:
        """Recent heat-loss learning samples."""
        ...

    @property
    def heating_power_normalized(self) -> float | None:
        """Outdoor-normalized heating power, if known."""
        ...

    @property
    def temperature_slope(self) -> float | None:
        """Current temperature slope in °C/min, if known."""
        ...


def _to_float(value: object) -> float | None:
    """Best-effort float cast for telemetry values; no rounding."""
    match value:
        case bool():
            return None
        case int() | float():
            return float(value)
        case str():
            try:
                return float(value)
            except ValueError:
                return None
        case _:
            return None


def _strict_json(payload: object, label: str) -> str | None:
    """Serialize telemetry to strict JSON, or ``None`` when it cannot be.

    ``allow_nan=False`` keeps NaN and infinity out of the result. Python's
    encoder otherwise writes them as the bare literals ``NaN`` and
    ``Infinity``, which no JSON parser accepts, so one non-finite sample
    would make the whole attribute unreadable for every consumer. Omitting
    that attribute leaves the others usable.

    Parameters
    ----------
    payload : object
        the telemetry to serialize
    label : str
        name of the attribute, used in the log line when serializing fails

    Returns
    -------
    str | None
        the JSON text, or None when the payload does not serialize
    """
    try:
        return json.dumps(payload, allow_nan=False)
    except TypeError, ValueError:
        _LOGGER.exception("Error while serializing %s", label)
        return None


def _serialize_cycles(
    cycles: Sequence[Mapping[str, object]] | None,
    count_key: str,
    last_key: str,
    label: str,
) -> dict[str, object]:
    """Serialize a cycle sequence to a count + last-entry JSON dict."""
    if not cycles:
        return {}
    last = _strict_json(cycles[-1], label)
    if last is None:
        return {}
    return {count_key: len(cycles), last_key: last}


def collect_cycle_telemetry(bt: TelemetrySource) -> dict[str, object]:
    """Heating/loss cycle counts, last-cycle JSON, heat-loss stats, normalized power."""
    out: dict[str, object] = {}

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
        stats = _strict_json(list(bt.last_heat_loss_stats), "heat loss stats")
        if stats is not None:
            out[ATTR_STATE_HEAT_LOSS_STATS] = stats

    out[ATTR_HEATING_POWER_NORMALIZED] = bt.heating_power_normalized

    return out


def published_temperature_slope(slope: float) -> float:
    """Return the temperature slope at the precision the state publishes it."""
    return round(slope, 4)


def collect_balance_attrs(bt: TelemetrySource) -> dict[str, object]:
    """Temperature slope plus a compact per-TRV calibration balance summary."""
    out: dict[str, object] = {}

    if bt.temperature_slope is not None:
        out[ATTR_STATE_TEMPERATURE_SLOPE] = published_temperature_slope(
            bt.temperature_slope
        )

    bal_compact: dict[str, dict[str, float | None]] = {}
    for trv, info in bt.real_trvs.items():
        bal = info.calibration_balance
        if bal is None:
            continue
        bal_compact[trv] = {"valve%": bal.get("valve_percent")}
    if bal_compact:
        balance = _strict_json(bal_compact, "calibration balance")
        if balance is not None:
            out["calibration_balance"] = balance

    return out


type PIDScalarKey = Literal[
    "e_K", "p", "i", "d", "u", "kp", "ki", "kd", "meas_smooth_C", "dt_s"
]

# (PIDDebugInfo key, output key, decimals).
_PID_SCALAR_FIELDS: tuple[tuple[PIDScalarKey, str, int], ...] = (
    ("e_K", ATTR_PID_ERROR, 4),
    ("p", "pid_P", 4),
    ("i", "pid_I", 4),
    ("d", "pid_D", 4),
    ("u", "pid_u", 4),
    ("kp", "pid_kp", 6),
    ("ki", "pid_ki", 6),
    ("kd", "pid_kd", 6),
    ("meas_smooth_C", ATTR_PID_MEASUREMENT_FILTERED, 3),
    ("dt_s", ATTR_PID_DT, 3),
)


def _pick_representative_trv(real_trvs: Mapping[str, TrvInfo]) -> str | None:
    """Prefer a sonoff/trvzb TRV; else first key."""
    for entity_id, info in real_trvs.items():
        model = (info.model or "").lower()
        if "sonoff" in model or "trvzb" in model:
            return entity_id
    return next(iter(real_trvs), None)


def _extract_pid_debug(info: TrvInfo | None) -> PIDDebugInfo | None:
    """Return PID debug payload when the TRV's calibration is in PID mode."""
    if info is None:
        return None
    bal = info.calibration_balance
    if bal is None or bal["controller"] != CalibrationMode.PID_CALIBRATION:
        return None
    return bal["debug"]


def collect_pid_debug_attrs(bt: TelemetrySource) -> dict[str, object]:
    """Flatten PID controller debug from a representative TRV's calibration_balance."""
    out: dict[str, object] = {}

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
        out[ATTR_PID_MEASUREMENT_SLOPE] = round(d_per_s * 60.0, 4)

    return out


type MpcV2DebugKey = Literal[
    "T_room_hat",
    "T_rad_hat",
    "D_hat_K_per_min",
    "tau_room_min",
    "coupling_rad_room",
    "group_valve_pct",
    "reid_tau_room",
    "reid_gain",
]

# (debug key, output key, decimals)
_MPC_V2_FIELDS: tuple[tuple[MpcV2DebugKey, str, int], ...] = (
    ("T_room_hat", ATTR_MPC_V2_ROOM_TEMPERATURE, 3),
    ("T_rad_hat", ATTR_MPC_V2_RADIATOR_TEMPERATURE, 3),
    ("D_hat_K_per_min", ATTR_MPC_V2_DISTURBANCE, 4),
    ("tau_room_min", ATTR_MPC_V2_TAU_ROOM, 1),
    ("coupling_rad_room", ATTR_MPC_V2_COUPLING, 3),
    ("group_valve_pct", ATTR_MPC_V2_GROUP_VALVE, 1),
    ("reid_tau_room", ATTR_MPC_V2_REID_TAU_ROOM, 1),
    ("reid_gain", "mpc_v2_reid_gain", 2),
)


def _extract_mpc_v2_debug(info: TrvInfo | None) -> MpcV2BalanceDebugInfo | None:
    """Return the v2 debug payload when calibration is in MPC v2 mode."""
    if info is None:
        return None
    bal = info.calibration_balance
    if bal is None or bal["controller"] != CalibrationMode.MPC_V2_CALIBRATION:
        return None
    return bal["debug"]


def collect_mpc_v2_debug_attrs(bt: TelemetrySource) -> dict[str, object]:
    """Flatten MPC v2 controller diagnostics from a representative TRV."""
    out: dict[str, object] = {}

    rep = _pick_representative_trv(bt.real_trvs)
    if rep is None:
        return out

    debug = _extract_mpc_v2_debug(bt.real_trvs.get(rep))
    if debug is None:
        return out

    for src_key, dst_key, decimals in _MPC_V2_FIELDS:
        if (value := _to_float(debug.get(src_key))) is not None:
            out[dst_key] = round(value, decimals)

    return out


# Every attribute the collectors above can write. They carry controller
# internals, several of which change on nearly every state write, so the
# climate entity keeps them out of the recorder. The live state still shows
# them.
TELEMETRY_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "heating_cycle_count",
        "heating_cycle_last",
        "heat_loss_cycle_count",
        "heat_loss_cycle_last",
        ATTR_STATE_HEAT_LOSS_STATS,
        ATTR_HEATING_POWER_NORMALIZED,
        ATTR_STATE_TEMPERATURE_SLOPE,
        "calibration_balance",
        ATTR_PID_MEASUREMENT_SLOPE,
        *(dst_key for _, dst_key, _ in _PID_SCALAR_FIELDS),
        *(dst_key for _, dst_key, _ in _MPC_V2_FIELDS),
    }
)
