"""Flight recorder: the last N decision tuples, exportable and replayable.

Because ``decide()`` is pure, recording ``(snapshot, state, desired)``
per cycle is enough to reproduce any decision offline: feed the
exported tuple back through the kernel and compare. The recorder keeps
a bounded ring buffer and serializes on export only.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .decide import KernelState, decide
from .desired import DesiredState, Suppression, TrvDesired
from .fsm.control_mode import ControlMode, ControlModeState
from .fsm.lifecycle import LifecyclePhase, LifecycleState
from .fsm.maintenance import MaintenancePhase, MaintenanceState
from .fsm.mode import ModeState
from .fsm.reachability import ReachabilityState
from .fsm.window import WindowPhase, WindowState
from .snapshot import TrvReported, WorldSnapshot, parse_hvac_mode

DEFAULT_CAPACITY = 50

type Json = str | int | float | bool | None | list[Json] | dict[str, Json]

# The dataclasses fed to ``_json_safe`` via ``asdict`` carry StrEnum
# members (HvacMode, ControlMode, the FSM phases, Suppression — all str
# subtypes, covered by ``str``) and nested mappings/sequences. Covariant
# container types let the wider ``asdict`` value unions flow in.
type _Recordable = (
    str
    | int
    | float
    | bool
    | None
    | datetime
    | Mapping[str, "_Recordable"]
    | Sequence["_Recordable"]
)


def _json_safe(value: _Recordable) -> Json:
    """Convert datetimes to ISO strings, recursing through containers."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_json_safe(item) for item in value]
    return value


def _dict_of(value: Json) -> dict[str, Json]:
    if not isinstance(value, dict):
        raise ValueError(f"expected a mapping, got {type(value).__name__}")
    return value


def _str_of(value: Json) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    return value


def _str_or_none(value: Json) -> str | None:
    if value is None:
        return None
    return _str_of(value)


def _bool_of(value: Json) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"expected a bool, got {type(value).__name__}")
    return value


def _bool_or_none(value: Json) -> bool | None:
    if value is None:
        return None
    return _bool_of(value)


def _float_or_none(value: Json) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected a number, got {type(value).__name__}")
    return float(value)


def _float_of(value: Json) -> float:
    number = _float_or_none(value)
    if number is None:
        raise ValueError("expected a number, got None")
    return number


def _int_of(value: Json) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer, got {type(value).__name__}")
    return value


def _datetime_or_none(value: Json) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(_str_of(value))


@dataclass
class FlightRecorder:
    """Bounded ring buffer of decision tuples."""

    capacity: int = DEFAULT_CAPACITY
    _entries: deque[tuple[WorldSnapshot, KernelState, DesiredState]] = field(
        default_factory=deque, repr=False
    )

    def record(
        self, snapshot: WorldSnapshot, state: KernelState, desired: DesiredState
    ) -> None:
        """Append one decision tuple; the oldest entry falls out at capacity.

        ``state`` must be the kernel state *before* the decision so that
        a replay reproduces the run exactly.
        """
        self._entries.append((snapshot, deepcopy(state), desired))
        while len(self._entries) > self.capacity:
            self._entries.popleft()

    def __len__(self) -> int:
        """Return the number of recorded tuples."""
        return len(self._entries)

    def export(self) -> list[dict[str, Json]]:
        """Serialize the buffer to JSON-safe dicts (oldest first)."""
        return [
            {
                "snapshot": _json_safe(asdict(snapshot)),
                "state": _json_safe(asdict(state)),
                "desired": _json_safe(asdict(desired)),
            }
            for snapshot, state, desired in self._entries
        ]


def snapshot_from_dict(data: dict[str, Json]) -> WorldSnapshot:
    """Reconstruct a WorldSnapshot from its exported form."""
    trvs = {}
    for entity_id, raw_entry in _dict_of(data["trvs"]).items():
        raw = _dict_of(raw_entry)
        trvs[entity_id] = TrvReported(
            entity_id=_str_of(raw["entity_id"]),
            available=_bool_of(raw["available"]),
            hvac_mode=parse_hvac_mode(_str_or_none(raw["hvac_mode"])),
            current_temp=_float_or_none(raw["current_temp"]),
            setpoint=_float_or_none(raw["setpoint"]),
            min_temp=_float_or_none(raw["min_temp"]),
            max_temp=_float_or_none(raw["max_temp"]),
            valve_max_opening=_float_or_none(raw["valve_max_opening"]),
            local_calibration_min=_float_or_none(raw["local_calibration_min"]),
            local_calibration_max=_float_or_none(raw["local_calibration_max"]),
        )
    now = _datetime_or_none(data["now"])
    if now is None:
        raise ValueError("snapshot export lacks a parseable 'now'")
    return WorldSnapshot(
        now=now,
        now_monotonic=_float_of(data["now_monotonic"]),
        target_temp=_float_or_none(data["target_temp"]),
        target_cooltemp=_float_or_none(data["target_cooltemp"]),
        hvac_mode=parse_hvac_mode(_str_or_none(data["hvac_mode"])),
        room_temp=_float_or_none(data["room_temp"]),
        room_temp_filtered=_float_or_none(data["room_temp_filtered"]),
        temp_slope=_float_or_none(data["temp_slope"]),
        call_for_heat=_bool_of(data["call_for_heat"]),
        window_open=_bool_or_none(data.get("window_open")),
        preset_mode=_str_or_none(data["preset_mode"]),
        tolerance=_float_of(data["tolerance"]),
        outdoor_temp=_float_or_none(data["outdoor_temp"]),
        is_day=_bool_of(data["is_day"]),
        solar_intensity=_float_of(data["solar_intensity"]),
        min_temp=_float_or_none(data["min_temp"]),
        max_temp=_float_or_none(data["max_temp"]),
        trvs=trvs,
    )


def state_from_dict(data: dict[str, Json]) -> KernelState:
    """Reconstruct a KernelState from its exported form."""
    window = _dict_of(data["window"])
    maintenance = _dict_of(data["maintenance"])
    lifecycle = _dict_of(data["lifecycle"])
    mode = _dict_of(data["mode"])
    control_mode = _dict_of(data["control_mode"])
    reachability = {}
    for entity_id, raw_entry in _dict_of(data["reachability"]).items():
        raw = _dict_of(raw_entry)
        reachability[entity_id] = ReachabilityState(
            online=_bool_of(raw["online"]),
            offline_since=_float_or_none(raw["offline_since"]),
            retry_count=_int_of(raw["retry_count"]),
            retry_at=_float_or_none(raw["retry_at"]),
        )
    unavailable = control_mode["unavailable_sensors"]
    if not isinstance(unavailable, list):
        raise ValueError("unavailable_sensors must be a list")
    return KernelState(
        window=WindowState(
            phase=WindowPhase(_str_of(window["phase"])),
            pending_since=_float_or_none(window["pending_since"]),
        ),
        maintenance=MaintenanceState(
            phase=MaintenancePhase(_str_of(maintenance["phase"])),
            next_due=_datetime_or_none(maintenance["next_due"]),
            running_since=_float_or_none(maintenance["running_since"]),
        ),
        lifecycle=LifecycleState(
            phase=LifecyclePhase(_str_of(lifecycle["phase"])),
            grace_until=_datetime_or_none(lifecycle["grace_until"]),
        ),
        mode=ModeState(
            hvac_mode=parse_hvac_mode(_str_or_none(mode["hvac_mode"]))
            or ModeState().hvac_mode,
            preset=_str_or_none(mode["preset"]),
        ),
        control_mode=ControlModeState(
            mode=ControlMode(_str_of(control_mode["mode"])),
            unavailable_sensors=tuple(_str_of(item) for item in unavailable),
            degraded_since=_float_or_none(control_mode["degraded_since"]),
            down_pending_since=_float_or_none(control_mode["down_pending_since"]),
            up_pending_since=_float_or_none(control_mode["up_pending_since"]),
        ),
        reachability=reachability,
        last_control_monotonic=_float_or_none(data["last_control_monotonic"]),
    )


def desired_from_dict(data: dict[str, Json]) -> DesiredState:
    """Reconstruct a DesiredState from its exported form."""
    trvs = {}
    for entity_id, raw_entry in _dict_of(data["trvs"]).items():
        raw = _dict_of(raw_entry)
        trvs[entity_id] = TrvDesired(
            entity_id=_str_of(raw["entity_id"]),
            hvac_mode=parse_hvac_mode(_str_or_none(raw["hvac_mode"])),
            setpoint=_float_or_none(raw["setpoint"]),
            valve_percent=_float_or_none(raw["valve_percent"]),
            offset=_float_or_none(raw["offset"]),
            suppression=(
                Suppression(_str_of(raw["suppression"]))
                if raw["suppression"] is not None
                else None
            ),
        )
    return DesiredState(call_for_heat=_bool_of(data["call_for_heat"]), trvs=trvs)


def replay(entry: dict[str, Json]) -> tuple[bool, DesiredState]:
    """Re-run one exported decision tuple through the kernel.

    Returns ``(matches, recomputed_desired)`` — ``matches`` is True when
    the kernel reproduces the recorded decision exactly.
    """
    snapshot = snapshot_from_dict(_dict_of(entry["snapshot"]))
    state = state_from_dict(_dict_of(entry["state"]))
    recorded = desired_from_dict(_dict_of(entry["desired"]))
    recomputed, _ = decide(snapshot, state)
    return recomputed == recorded, recomputed
