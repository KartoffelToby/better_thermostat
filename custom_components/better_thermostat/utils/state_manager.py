"""Unified runtime state persistence for Better Thermostat.

Replaces four separate HA Store files with a single versioned store per
config entry. The StateManager owns all runtime state that must survive
a Home Assistant restart (calibration models, thermal stats, learned
presets).

Usage in climate.py
-------------------
::

    async def async_added_to_hass(self) -> None:
        self.state_mgr = StateManager(self.hass, self.config_entry.entry_id)
        await self.state_mgr.load()

    # After calibration updates:
    self.state_mgr.mark_dirty()

    async def async_will_remove_from_hass(self) -> None:
        self.state_mgr.close()
        await self.state_mgr.flush()

Schema migration
----------------
When ``load()`` reads a store file without a ``"version"`` key it applies
``_migrate_v0_to_v1`` which fills in schema defaults.  Future schema
changes bump ``CURRENT_VERSION`` and add a new migration function.

One-time data migration from the four legacy Store files is handled by
``migrate_v0_stores`` (see ``utils/migrate_v0_stores.py``).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
import logging
import math
from time import monotonic
from typing import Any

from homeassistant.core import (
    CALLBACK_TYPE,
    CoreState,
    HassJob,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .calibration.mpc import MpcState
from .calibration.mpc_v2 import (
    MpcV2Params,
    MpcV2State,
    export_mpc_v2_state,
    import_mpc_v2_state,
)
from .calibration.pid import PIDState
from .calibration.tpi import TpiState
from .const import (
    DOMAIN,
    MAX_HEAT_LOSS,
    MAX_HEATING_POWER,
    MIN_HEAT_LOSS,
    MIN_HEATING_POWER,
)
from .thermal_learning import clamp


@dataclass
class MpcV2StateData:
    """Persistable per-key state for the MPC v2 controller.

    ``snapshot`` is the opaque payload returned by
    :meth:`MpcV2Controller.export_snapshot` — restored verbatim by
    :meth:`MpcV2Controller.restore_snapshot`. Top-level fields mirror the
    metadata the runtime state holds independently of the controller.
    """

    last_percent: float | None = None
    last_compute_ts: float = 0.0
    created_ts: float = 0.0
    outdoor_fallback_logged: bool = False
    snapshot: dict[str, Any] = field(default_factory=dict)


_LOGGER = logging.getLogger(__name__)

CURRENT_VERSION = 1

# Container version of the file an unreadable store is set aside in. The
# payload is kept verbatim and only read back to confirm the copy, so this
# version stays put when ``CURRENT_VERSION`` moves and no migration ever
# rewrites a set-aside copy.
QUARANTINE_VERSION = 1

# How many distinct unreadable payloads one config entry keeps copies of.
# The first copy holds the state from before anything went wrong and the
# newest the latest learning; one more keeps the step between them. A
# store that keeps turning unreadable does not fill the disk with copies.
QUARANTINE_COPIES = 3

# Seconds before a failed copy is tried again, by a timer or by a runtime
# save that falls due first, doubling after each failure up to the cap: a
# disk that recovers is used within the hour, and one that stays full is not
# written to on every save.
COPY_RETRY_FIRST_S = 60.0
COPY_RETRY_MAX_S = 3600.0

# State dataclasses (only those NOT owned by a controller module)


@dataclass
class ThermalStats:
    """Learned thermal characteristics of the room."""

    heating_power: float | None = None
    heat_loss_rate: float | None = None


@dataclass
class RuntimeState:
    """Complete runtime state for one BetterThermostat config entry.

    This is the top-level structure that gets serialized to a single
    HA Store file.
    """

    version: int = CURRENT_VERSION
    mpc: dict[str, MpcState] = field(default_factory=dict)
    mpc_v2: dict[str, MpcV2StateData] = field(default_factory=dict)
    pid: dict[str, PIDState] = field(default_factory=dict)
    tpi: dict[str, TpiState] = field(default_factory=dict)
    thermal: ThermalStats = field(default_factory=ThermalStats)
    presets: dict[str, float] = field(default_factory=dict)


# Serialization helpers

# Fields that should be coerced to int during deserialization.
_INT_FIELDS = frozenset(
    {
        "dead_zone_hits",
        "loss_learn_count",
        "gain_learn_count",
        "profile_samples",
        "consecutive_insufficient_heat",
        "last_delta_sign",
        "last_error_sign",
    }
)

# Fields that should be coerced to bool during deserialization.
_BOOL_FIELDS = frozenset(
    {
        "is_calibration_active",
        "regime_boost_active",
        "tolerance_hold_active",
        "auto_tune",
    }
)

# Fields that should be coerced to str during deserialization.
_STR_FIELDS = frozenset({"trv_profile"})


def _make_json_safe(obj: Any) -> Any:
    """Recursively convert non-JSON-serializable types.

    ``dataclasses.asdict`` does **not** convert ``deque`` to ``list``,
    so we walk the resulting dict and fix up anything that ``json.dumps``
    would choke on.
    """
    if isinstance(obj, deque):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(v) for v in obj]
    return obj


def _serialize(state: RuntimeState) -> dict[str, Any]:
    """Convert RuntimeState to a JSON-serializable dict.

    The ``deque`` used by MPC's ``recent_errors`` is converted to a plain
    list so that ``json.dumps`` can handle it.
    """
    data = asdict(state)
    return _make_json_safe(data)


def _finite_float(value: Any) -> float:
    """Parse one stored float; a non-finite number cannot be used."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{number} is not a finite number")
    return number


def _report_unreadable_field(
    attr: str, kind: str, key: str | None, dropped: list[str] | None
) -> None:
    """Name a stored field that keeps its default because it cannot be read.

    Past the load path the field carries the default a first start leaves
    there, and a value the store lost looks exactly like one it never
    held, so this is the only place that can still say so. The field is
    also added to *dropped* when the caller collects them.
    """
    if dropped is not None:
        dropped.append(f"{kind}.{key}.{attr}")
    _LOGGER.warning(
        "better_thermostat: stored %s state for %s has an unusable %s, "
        "continuing without it",
        kind,
        key or "an unnamed state entry",
        attr,
        exc_info=True,
    )


def deserialize_mpc(
    raw: dict[str, Any], *, key: str | None = None, dropped: list[str] | None = None
) -> MpcState:
    """Deserialize a single MPC state dict into an MpcState dataclass.

    Parameters
    ----------
    raw : dict[str, Any]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    dropped : list[str] | None
        collects the fields left at their default because they cannot be read
    """
    state = MpcState()
    for attr in MpcState.__dataclass_fields__:
        if attr not in raw:
            continue
        value = raw[attr]
        if value is None:
            setattr(state, attr, None)
            continue
        if attr == "perf_curve" and isinstance(value, Mapping):
            setattr(state, attr, dict(value))
            continue
        if attr == "recent_errors" and isinstance(value, (list, tuple)):
            # MpcState.recent_errors is a deque(maxlen=20).
            setattr(state, attr, deque(value, maxlen=20))
            continue
        try:
            if attr in _INT_FIELDS:
                setattr(state, attr, int(value))
            elif attr in _BOOL_FIELDS:
                setattr(state, attr, bool(value))
            elif attr in _STR_FIELDS:
                setattr(state, attr, str(value))
            else:
                setattr(state, attr, _finite_float(value))
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(attr, "mpc", key, dropped)
            continue
    return state


def deserialize_mpc_v2(
    raw: dict[str, Any], *, key: str | None = None, dropped: list[str] | None = None
) -> MpcV2StateData:
    """Deserialize a single MPC v2 state dict into MpcV2StateData.

    Parameters
    ----------
    raw : dict[str, Any]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    dropped : list[str] | None
        collects the fields left at their default because they cannot be read
    """
    state = MpcV2StateData()
    for attr in ("last_percent", "last_compute_ts", "created_ts"):
        value = raw.get(attr)
        if value is None:
            continue
        try:
            setattr(state, attr, _finite_float(value))
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(attr, "mpc_v2", key, dropped)
            continue
    state.outdoor_fallback_logged = bool(raw.get("outdoor_fallback_logged", False))
    snapshot = raw.get("snapshot")
    if isinstance(snapshot, Mapping):
        state.snapshot = dict(snapshot)
    elif snapshot is not None:
        # The snapshot holds the learned controller state, so one of any
        # other shape is named and collected like every other dropped field.
        if dropped is not None:
            dropped.append(f"mpc_v2.{key}.snapshot")
        _LOGGER.warning(
            "better_thermostat: stored mpc_v2 state for %s has an unusable "
            "snapshot, continuing without it",
            key or "an unnamed state entry",
        )
    return state


def deserialize_pid(
    raw: dict[str, Any], *, key: str | None = None, dropped: list[str] | None = None
) -> PIDState:
    """Deserialize a single PID state dict into a PIDState dataclass.

    Parameters
    ----------
    raw : dict[str, Any]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    dropped : list[str] | None
        collects the fields left at their default because they cannot be read
    """
    state = PIDState()
    for attr in PIDState.__dataclass_fields__:
        if attr not in raw:
            continue
        value = raw[attr]
        if value is None:
            setattr(state, attr, None)
            continue
        try:
            if attr in _INT_FIELDS:
                setattr(state, attr, int(value))
            elif attr in _BOOL_FIELDS:
                setattr(state, attr, bool(value))
            else:
                setattr(state, attr, _finite_float(value))
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(attr, "pid", key, dropped)
            continue
    return state


def deserialize_tpi(
    raw: dict[str, Any], *, key: str | None = None, dropped: list[str] | None = None
) -> TpiState:
    """Deserialize a single TPI state dict into a TpiState dataclass.

    Parameters
    ----------
    raw : dict[str, Any]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    dropped : list[str] | None
        collects the fields left at their default because they cannot be read
    """
    state = TpiState()
    for attr in TpiState.__dataclass_fields__:
        if attr not in raw:
            continue
        value = raw[attr]
        if value is None:
            setattr(state, attr, None)
            continue
        try:
            setattr(state, attr, _finite_float(value))
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(attr, "tpi", key, dropped)
            continue
    return state


def _stored_section(
    raw: dict[str, Any], section: str, dropped: list[str]
) -> Mapping[str, Any] | None:
    """Return one section of the store, or ``None`` when it has none.

    A section of any other shape than a mapping is dropped, named and added
    to *dropped*: past the load path its entries start from defaults, like
    on a first start.
    """
    value = raw.get(section, {})
    if isinstance(value, Mapping):
        return value
    dropped.append(section)
    _LOGGER.warning(
        "better_thermostat: stored %s section is not a mapping; its entries "
        "start from defaults",
        section,
    )
    return None


def _stored_entries(
    raw: dict[str, Any], section: str, dropped: list[str]
) -> list[tuple[str, dict[str, Any]]]:
    """Return the entries of one keyed section that are mappings.

    An entry of any other shape is dropped, named with its key and added
    to *dropped*.
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    for key, entry in (_stored_section(raw, section, dropped) or {}).items():
        if isinstance(entry, dict):
            entries.append((key, entry))
            continue
        dropped.append(f"{section}.{key}")
        _LOGGER.warning(
            "better_thermostat: stored %s entry for %s is not a mapping; "
            "it starts from defaults",
            section,
            key,
        )
    return entries


def _stored_optional_number(
    values: Mapping[str, Any], section: str, attr: str, dropped: list[str]
) -> float | None:
    """Return one optional number of an unkeyed section, naming an unusable one.

    A missing value and a stored null are a value never learned and pass
    as ``None`` silently. Anything else that is not a finite number is
    dropped as well, named and added to *dropped*, since past the load path
    it looks like one never learned.
    """
    value = values.get(attr)
    if value is None:
        return None
    try:
        return _finite_float(value)
    except TypeError, ValueError, OverflowError:
        dropped.append(f"{section}.{attr}")
        _LOGGER.warning(
            "better_thermostat: stored %s section has an unusable %s, "
            "continuing without it",
            section,
            attr,
        )
        return None


def _deserialize(
    raw: dict[str, Any], *, dropped: list[str] | None = None
) -> RuntimeState:
    """Reconstruct a RuntimeState from a raw dict (loaded from Store).

    Parameters
    ----------
    raw : dict[str, Any]
        the stored payload
    dropped : list[str] | None
        collects every value, entry and section the load leaves out
    """
    if dropped is None:
        dropped = []
    state = RuntimeState(version=raw.get("version", CURRENT_VERSION))

    for key, entry in _stored_entries(raw, "mpc", dropped):
        state.mpc[key] = deserialize_mpc(entry, key=key, dropped=dropped)

    for key, entry in _stored_entries(raw, "mpc_v2", dropped):
        state.mpc_v2[key] = deserialize_mpc_v2(entry, key=key, dropped=dropped)

    for key, entry in _stored_entries(raw, "pid", dropped):
        state.pid[key] = deserialize_pid(entry, key=key, dropped=dropped)

    for key, entry in _stored_entries(raw, "tpi", dropped):
        state.tpi[key] = deserialize_tpi(entry, key=key, dropped=dropped)

    thermal_raw = _stored_section(raw, "thermal", dropped)
    if thermal_raw is not None:
        state.thermal = ThermalStats(
            heating_power=_stored_optional_number(
                thermal_raw, "thermal", "heating_power", dropped
            ),
            heat_loss_rate=_stored_optional_number(
                thermal_raw, "thermal", "heat_loss_rate", dropped
            ),
        )

    presets_raw = _stored_section(raw, "presets", dropped)
    if presets_raw is not None:
        for name in presets_raw:
            number = _stored_optional_number(presets_raw, "presets", name, dropped)
            if number is not None:
                state.presets[str(name)] = number

    return state


def _store_key(entry_id: str) -> str:
    """Return the Store key holding one config entry's runtime state."""
    return f"{DOMAIN}_{entry_id}_state"


def _quarantine_key(entry_id: str, copy: int = 0) -> str:
    """Return the Store key one copy of an unreadable runtime state is kept under.

    The suffix matches the one Home Assistant's own Store appends when it
    finds a storage file it cannot parse, so both kinds of damaged file sit
    next to each other under recognizable names. The first copy has no
    number, and each later one of the :data:`QUARANTINE_COPIES` is numbered.
    """
    key = f"{_store_key(entry_id)}.corrupt"
    return key if copy == 0 else f"{key}.{copy}"


async def async_remove_stores(hass: HomeAssistant, entry_id: str) -> None:
    """Delete a config entry's runtime state and every copy set aside from it.

    Parameters
    ----------
    hass : HomeAssistant
        The Home Assistant instance.
    entry_id : str
        Config entry identifier whose store files are removed.
    """
    await Store(hass, CURRENT_VERSION, _store_key(entry_id)).async_remove()
    for copy in range(QUARANTINE_COPIES):
        await Store(
            hass, QUARANTINE_VERSION, _quarantine_key(entry_id, copy)
        ).async_remove()


# Migration


def _migrate_v0_to_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate from unversioned (v0) format to v1.

    v0 is the legacy format where MPC/PID/TPI/thermal data lived in
    separate Store files.  If loading from a unified store that already
    has the v1 schema, this is a no-op.
    """
    raw.setdefault("version", 1)
    raw.setdefault("mpc", {})
    raw.setdefault("pid", {})
    raw.setdefault("tpi", {})
    raw.setdefault("thermal", {})
    raw.setdefault("presets", {})
    return raw


# StateManager


class StateManager:
    """Manages unified runtime state persistence for one BetterThermostat instance.

    Parameters
    ----------
    hass : HomeAssistant
        The Home Assistant instance.
    entry_id : str
        The config entry ID (stable across restarts).
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, CURRENT_VERSION, _store_key(entry_id)
        )
        self._hass = hass
        self._entry_id = entry_id
        self._state = RuntimeState()
        # Live MPC v2 controllers, held in memory across cycles. The persisted
        # ``_state.mpc_v2`` snapshots are folded in only at save time.
        self._mpc_v2_live: dict[str, MpcV2State] = {}
        self._dirty = False
        # A payload load() could not read in full and could not set aside
        # either. The live store holds its only copy, so nothing is written
        # over it until the copy exists.
        self._payload_awaiting_copy: dict[str, Any] | None = None
        # Whether the failing copy has been reported at WARNING already; each
        # further attempt that fails is logged at DEBUG only.
        self._copy_failure_reported = False
        # When the copy is next tried, the wait after that, and whether a try
        # is under way.
        self._copy_retry_at = 0.0
        self._copy_retry_s = COPY_RETRY_FIRST_S
        self._copy_retry_running = False
        # The background task of the latest try, timed or started by a
        # runtime save, or of the latest save_unless_closed(); flush() waits
        # for it.
        self._copy_retry_task: asyncio.Task[None] | None = None
        # The timer that tries the copy at ``_copy_retry_at`` on its own, and
        # whether the manager still starts one; after close() it does not.
        self._copy_retry_timer: CALLBACK_TYPE | None = None
        self._copy_retry_timed = True

    # -- Public properties ---------------------------------------------------

    @property
    def state(self) -> RuntimeState:
        """Return the current runtime state (read-only access)."""
        return self._state

    @property
    def dirty(self) -> bool:
        """Return whether unsaved changes exist."""
        return self._dirty

    # -- State access --------------------------------------------------------

    def get_mpc(self, key: str) -> MpcState:
        """Get or create MPC state for a key."""
        if key not in self._state.mpc:
            self._state.mpc[key] = MpcState()
            self._dirty = True
        return self._state.mpc[key]

    def set_mpc(self, key: str, mpc: MpcState) -> None:
        """Set MPC state for a key and mark dirty."""
        self._state.mpc[key] = mpc
        self._dirty = True

    def get_mpc_v2_live(self, key: str, params: MpcV2Params) -> MpcV2State:
        """Return the live MPC v2 controller state, building it on first use.

        The live controller (Kalman/QP/governor) is kept in memory across
        control cycles. On first access it is rehydrated from the persisted
        snapshot (when one exists); thereafter the same instance is reused, so
        learned state is not rebuilt every cycle. Conversion to the persistable
        form happens only at save time (see :meth:`_sync_mpc_v2_live`).
        """
        live = self._mpc_v2_live.get(key)
        if live is None:
            persisted = self._state.mpc_v2.get(key)
            live = (
                import_mpc_v2_state(asdict(persisted), params)
                if persisted is not None
                else MpcV2State()
            )
            self._mpc_v2_live[key] = live
        return live

    def set_mpc_v2_live(self, key: str, state: MpcV2State) -> None:
        """Store the live MPC v2 controller state for a key and mark dirty."""
        self._mpc_v2_live[key] = state
        self._dirty = True

    def _sync_mpc_v2_live(self) -> None:
        """Fold live MPC v2 controllers into the persistable snapshot.

        Runs at save time only; the per-cycle path keeps the live controller in
        memory and never serialises it.
        """
        for key, live in self._mpc_v2_live.items():
            exported = export_mpc_v2_state(live)
            if exported is not None:
                self._state.mpc_v2[key] = deserialize_mpc_v2(exported)

    def get_pid(self, key: str) -> PIDState:
        """Get or create PID state for a key."""
        if key not in self._state.pid:
            self._state.pid[key] = PIDState()
            self._dirty = True
        return self._state.pid[key]

    def set_pid(self, key: str, pid: PIDState) -> None:
        """Set PID state for a key and mark dirty."""
        self._state.pid[key] = pid
        self._dirty = True

    def reset_pid_states(self, prefix: str) -> int:
        """Drop all PID states whose key starts with *prefix*.

        Returns the number of removed entries; marks the store dirty when
        anything was removed.
        """
        keys = [key for key in self._state.pid if key.startswith(prefix)]
        for key in keys:
            del self._state.pid[key]
        if keys:
            self._dirty = True
        return len(keys)

    def get_tpi(self, key: str) -> TpiState:
        """Get or create TPI state for a key."""
        if key not in self._state.tpi:
            self._state.tpi[key] = TpiState()
            self._dirty = True
        return self._state.tpi[key]

    def set_tpi(self, key: str, tpi: TpiState) -> None:
        """Set TPI state for a key and mark dirty."""
        self._state.tpi[key] = tpi
        self._dirty = True

    @property
    def thermal(self) -> ThermalStats:
        """Return thermal stats."""
        return self._state.thermal

    @thermal.setter
    def thermal(self, value: ThermalStats) -> None:
        """Set thermal stats and mark dirty."""
        self._state.thermal = value
        self._dirty = True

    @property
    def presets(self) -> dict[str, float]:
        """Return learned preset temperatures."""
        return self._state.presets

    @presets.setter
    def presets(self, value: dict[str, float]) -> None:
        """Set learned preset temperatures and mark dirty."""
        self._state.presets = value
        self._dirty = True

    def mark_dirty(self) -> None:
        """Manually mark state as needing persistence."""
        self._dirty = True

    # -- Thermal stats ---------------------------------------------------------

    def clamped_thermal(self) -> tuple[float | None, float | None]:
        """Return persisted thermal stats clamped to their valid bounds.

        Returns ``(heating_power, heat_loss_rate)``; an element is ``None`` when
        the persisted value is absent or is not a finite number.
        """
        thermal = self._state.thermal

        heating_power: float | None = None
        if thermal.heating_power is not None:
            try:
                heating_power = clamp(
                    _finite_float(thermal.heating_power),
                    MIN_HEATING_POWER,
                    MAX_HEATING_POWER,
                )
            except TypeError, ValueError, OverflowError:
                heating_power = None

        heat_loss_rate: float | None = None
        if thermal.heat_loss_rate is not None:
            try:
                heat_loss_rate = clamp(
                    _finite_float(thermal.heat_loss_rate), MIN_HEAT_LOSS, MAX_HEAT_LOSS
                )
            except TypeError, ValueError, OverflowError:
                heat_loss_rate = None

        return heating_power, heat_loss_rate

    def record_thermal(
        self, heating_power: float | None, heat_loss_rate: float | None
    ) -> None:
        """Record the entity-held thermal stats before a save.

        Non-finite samples (NaN/inf) are dropped to ``None`` so a bad reading
        cannot be persisted and reloaded; this mirrors the finite handling in
        ``clamped_thermal()``.
        """

        def _finite_or_none(value: float | None) -> float | None:
            try:
                return value if value is not None and math.isfinite(value) else None
            except TypeError:
                return None

        self.thermal = ThermalStats(
            heating_power=_finite_or_none(heating_power),
            heat_loss_rate=_finite_or_none(heat_loss_rate),
        )

    # -- Load / Save ---------------------------------------------------------

    def close(self) -> None:
        """Stop trying the copy on a timer; call when the entity is removed.

        ``flush()`` and ``save()`` still try the copy, but no timer is left
        behind, and a copy already under way saves nothing, to write into a
        store another entity may own by then.
        """
        self._copy_retry_timed = False
        self._cancel_copy_retry_timer()

    def _schedule_copy_retry(self) -> None:
        """Set when the copy is next tried, double the wait, and start the timer.

        The timer tries the copy at that deadline without waiting for a
        runtime save, and saves unsaved changes once the copy exists. Home
        Assistant cancels it when it starts to stop, so it never runs beside
        the final write.
        """
        delay_s = self._copy_retry_s
        self._copy_retry_at = monotonic() + delay_s
        self._copy_retry_s = min(delay_s * 2, COPY_RETRY_MAX_S)
        self._cancel_copy_retry_timer()
        if self._copy_retry_timed:
            self._copy_retry_timer = async_call_later(
                self._hass,
                delay_s,
                HassJob(
                    self._retry_copy_when_due,
                    f"bt_state_copy_retry_{self._entry_id}",
                    cancel_on_shutdown=True,
                ),
            )

    def _cancel_copy_retry_timer(self) -> None:
        """Cancel the timed copy retry, if one is scheduled."""
        if self._copy_retry_timer is not None:
            self._copy_retry_timer()
            self._copy_retry_timer = None

    @callback
    def _retry_copy_when_due(self, _now: datetime) -> None:
        """Try the copy at its deadline, unless a try is already under way."""
        self._copy_retry_timer = None
        if self._payload_awaiting_copy is None or self._copy_retry_running:
            return
        self._start_copy_retry()

    def _start_copy_retry(self) -> asyncio.Task[None]:
        """Start a try of the copy as the task :meth:`flush` waits for."""
        self._copy_retry_running = True
        self._copy_retry_task = self._hass.async_create_background_task(
            self._retry_copy_then_save(), name=f"bt_state_copy_{self._entry_id}"
        )
        return self._copy_retry_task

    async def _retry_copy_then_save(self) -> None:
        """Try the awaited copy again and save unsaved changes once it is kept.

        While Home Assistant is stopping the copy is left for the final
        write, as in :meth:`save`. A manager closed while the copy was
        under way saves nothing: :meth:`flush` makes its final write.
        """
        try:
            payload = self._payload_awaiting_copy
            if payload is None or self._hass.state is CoreState.stopping:
                return
            await self._quarantine_unreadable_state(payload)
        finally:
            self._copy_retry_running = False
        if (
            self._payload_awaiting_copy is None
            and self._dirty
            and self._copy_retry_timed
        ):
            await self.save()

    def _report_copy_failure(
        self, message: str, key: str, *, with_traceback: bool = False
    ) -> None:
        """Log a copy that could not be set aside.

        The first failure is a WARNING; the error behind it and every
        further failed attempt go to DEBUG, so a disk that stays full does
        not repeat the warning on each attempt.
        """
        if not self._copy_failure_reported:
            self._copy_failure_reported = True
            _LOGGER.warning(message, self._entry_id, key)
        _LOGGER.debug(message, self._entry_id, key, exc_info=with_traceback)

    async def _quarantine_unreadable_state(self, raw: dict[str, Any]) -> None:
        """Set an unreadable store aside before defaults take its place.

        That covers a store that cannot be read at all and one with values,
        entries or sections the load dropped. The defaults this entity falls
        back to are written over the live store on its next save, so without
        a copy the only record of what a user's installation had learned is
        gone.

        Every distinct payload gets a copy of its own: one read after an
        earlier copy was taken holds what was learned since. A payload
        already kept adds none. With all :data:`QUARANTINE_COPIES` taken,
        the current payload replaces the newest copy, the one nearest to it
        in time, and the older ones stay. The copy counts once it loads
        back from disk as the payload; until then, the payload is held so
        that no save overwrites the live store before a later attempt
        succeeds.

        Parameters
        ----------
        raw : dict[str, Any]
            The store payload that could not be deserialized in full.
        """
        key = _quarantine_key(self._entry_id, QUARANTINE_COPIES - 1)
        try:
            free: str | None = None
            for copy in range(QUARANTINE_COPIES):
                copy_key = _quarantine_key(self._entry_id, copy)
                kept: Store[dict[str, Any]] = Store(
                    self._hass, QUARANTINE_VERSION, copy_key
                )
                stored = await kept.async_load()
                if stored == raw:
                    self._payload_awaiting_copy = None
                    self._cancel_copy_retry_timer()
                    return
                if stored is None and free is None:
                    free = copy_key
            key = free or key
            # Written atomically: replacing the newest copy must not leave it
            # half-written when the write fails.
            quarantine: Store[dict[str, Any]] = Store(
                self._hass, QUARANTINE_VERSION, key, atomic_writes=True
            )
            await quarantine.async_save(raw)
            # ``async_save`` returns normally when the write fails, and while
            # Home Assistant stops it only queues the write for the final
            # write. A second Store holds no queued data, so what it loads
            # is what reached the disk.
            on_disk: Store[dict[str, Any]] = Store(self._hass, QUARANTINE_VERSION, key)
            written = await on_disk.async_load()
        except HomeAssistantError, OSError:
            self._report_copy_failure(
                "better_thermostat [%s]: could not set the unreadable state "
                "aside as %s; the stored state is kept unchanged until it is",
                key,
                with_traceback=True,
            )
            self._payload_awaiting_copy = raw
            self._schedule_copy_retry()
            return
        if written != raw:
            self._report_copy_failure(
                "better_thermostat [%s]: the unreadable state did not reach %s; "
                "the stored state is kept unchanged until it does",
                key,
            )
            self._payload_awaiting_copy = raw
            self._schedule_copy_retry()
            return
        self._payload_awaiting_copy = None
        self._cancel_copy_retry_timer()
        _LOGGER.warning(
            "better_thermostat [%s]: unreadable state kept as %s for recovery",
            self._entry_id,
            key,
        )

    async def load(self) -> None:
        """Load state from HA Store.  Applies migrations if needed."""
        raw = await self._store.async_load()
        if not raw or not isinstance(raw, dict):
            _LOGGER.debug(
                "better_thermostat [%s]: No persisted state found, starting fresh",
                self._entry_id,
            )
            return

        # A store that breaks deserialization yields defaults, not a
        # crash: load() runs inside the entity's startup task, and
        # relearning replaces anything a poisoned store could offer.
        dropped: list[str] = []
        try:
            version = raw.get("version", 0)
            if version < 1:
                raw = _migrate_v0_to_v1(raw)
            self._state = _deserialize(raw, dropped=dropped)
        except Exception:
            _LOGGER.warning(
                "better_thermostat [%s]: persisted state is unreadable, starting fresh",
                self._entry_id,
                exc_info=True,
            )
            await self._quarantine_unreadable_state(raw)
            self._state = RuntimeState()
            self._dirty = False
            return
        if dropped:
            # The defaults of what was dropped replace the stored values on
            # the next save, so the payload is kept aside like an unreadable
            # store.
            await self._quarantine_unreadable_state(raw)
        self._dirty = False
        _LOGGER.debug(
            "better_thermostat [%s]: Loaded state v%d (%d mpc, %d pid, %d tpi keys)",
            self._entry_id,
            self._state.version,
            len(self._state.mpc),
            len(self._state.pid),
            len(self._state.tpi),
        )

    async def save(self) -> None:
        """Persist current state to HA Store.

        The one exception is a stored payload that load() could not read in
        full and could not set aside: the copy is attempted again first,
        and while it fails the live store keeps that payload and the state
        stays unsaved. While Home Assistant is stopping, the Store only
        queues writes, so no copy can be confirmed and none is attempted;
        the final write is where the copy is tried again.
        """
        if self._payload_awaiting_copy is not None:
            if self._hass.state is CoreState.stopping:
                _LOGGER.debug(
                    "better_thermostat [%s]: save left for the final write, the "
                    "stored state is not set aside yet",
                    self._entry_id,
                )
                return
            await self._quarantine_unreadable_state(self._payload_awaiting_copy)
            if self._payload_awaiting_copy is not None:
                return
        self._sync_mpc_v2_live()
        data = _serialize(self._state)
        await self._store.async_save(data)
        self._dirty = False
        _LOGGER.debug(
            "better_thermostat [%s]: Saved state (%d mpc, %d pid, %d tpi keys)",
            self._entry_id,
            len(self._state.mpc),
            len(self._state.pid),
            len(self._state.tpi),
        )

    async def save_unless_closed(self) -> None:
        """Save now, as the task :meth:`flush` waits for, unless closed by then.

        For a save that can run beside the entity's removal, such as the
        startup migration. A copy still pending is tried inside that task,
        so a flush waits for its outcome instead of trying the copy beside
        it, and a manager closed in the meantime writes nothing:
        :meth:`flush` makes the final write. A copy that fails again leaves
        the state dirty, and the timed retry saves it once the copy exists.
        """
        running = self._copy_retry_task
        if running is not None and not running.done():
            await asyncio.wait({running})
        if not self._copy_retry_timed:
            return
        task = self._hass.async_create_background_task(
            self._copy_then_save_unless_closed(), name=f"bt_state_save_{self._entry_id}"
        )
        self._copy_retry_task = task
        # Waited for without being cancelled with the caller: flush() waits
        # for the same task.
        await asyncio.wait({task})
        task.result()

    async def _copy_then_save_unless_closed(self) -> None:
        """Try a pending copy, then save unless the copy failed or close() ran.

        While Home Assistant is stopping the copy is left for the final
        write, as in :meth:`save`.
        """
        payload = self._payload_awaiting_copy
        if payload is not None:
            if self._hass.state is CoreState.stopping:
                return
            self._copy_retry_running = True
            try:
                await self._quarantine_unreadable_state(payload)
            finally:
                self._copy_retry_running = False
            if self._payload_awaiting_copy is not None:
                return
        if self._copy_retry_timed:
            await self.save()

    async def save_if_dirty(self) -> None:
        """Persist current state only if it has been modified since last save.

        While a stored payload still waits for its copy, the copy is tried
        again once :data:`COPY_RETRY_FIRST_S`, and after each failure twice
        as long, has passed; until it succeeds the save is skipped and the
        state stays unsaved. A timer tries the copy at the same deadline
        without waiting for this call. Either way the try runs as the task
        :meth:`flush` waits for, and saves nothing once :meth:`close` was
        called. :meth:`flush` tries the copy regardless.
        """
        if not self._dirty:
            return
        if (
            self._payload_awaiting_copy is not None
            and not self._copy_retry_running
            and monotonic() >= self._copy_retry_at
        ):
            await asyncio.wait({self._start_copy_retry()})
            return
        if self._payload_awaiting_copy is not None:
            _LOGGER.debug(
                "better_thermostat [%s]: save skipped, the stored state is "
                "not set aside yet",
                self._entry_id,
            )
            return
        await self.save()

    async def flush(self) -> None:
        """Flush unsaved changes -- call when the entity stops or is removed.

        Unlike :meth:`save_if_dirty`, this tries again to set aside a stored
        payload still waiting for its copy. A copy already under way is
        awaited first, so the final write sees its outcome instead of trying
        the copy beside it: a copy that lands after :meth:`close` saves
        nothing of its own. The wait is not bounded, like the Store write in
        :meth:`save` it consists of.
        """
        copy = self._copy_retry_task
        if copy is not None and not copy.done():
            await asyncio.wait({copy})
        if self._dirty:
            await self.save()
