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

from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .calibration.mpc import MpcState
from .calibration.pid import PIDState
from .calibration.tpi import TpiState

_LOGGER = logging.getLogger(__name__)

DOMAIN = "better_thermostat"
CURRENT_VERSION = 1

# ---------------------------------------------------------------------------
# State dataclasses (only those NOT owned by a controller module)
# ---------------------------------------------------------------------------


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
    pid: dict[str, PIDState] = field(default_factory=dict)
    tpi: dict[str, TpiState] = field(default_factory=dict)
    thermal: ThermalStats = field(default_factory=ThermalStats)
    presets: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

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


def deserialize_mpc(raw: dict[str, Any]) -> MpcState:
    """Deserialize a single MPC state dict into an MpcState dataclass."""
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
                setattr(state, attr, float(value))
        except (TypeError, ValueError):
            continue
    return state


def deserialize_pid(raw: dict[str, Any]) -> PIDState:
    """Deserialize a single PID state dict into a PIDState dataclass."""
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
                setattr(state, attr, float(value))
        except (TypeError, ValueError):
            continue
    return state


def deserialize_tpi(raw: dict[str, Any]) -> TpiState:
    """Deserialize a single TPI state dict into a TpiState dataclass."""
    state = TpiState()
    for attr in TpiState.__dataclass_fields__:
        if attr not in raw:
            continue
        value = raw[attr]
        if value is None:
            setattr(state, attr, None)
            continue
        try:
            setattr(state, attr, float(value))
        except (TypeError, ValueError):
            continue
    return state


def _deserialize(raw: dict[str, Any]) -> RuntimeState:
    """Reconstruct a RuntimeState from a raw dict (loaded from Store)."""
    state = RuntimeState(version=raw.get("version", CURRENT_VERSION))

    for key, state_dict in raw.get("mpc", {}).items():
        if isinstance(state_dict, dict):
            state.mpc[key] = deserialize_mpc(state_dict)

    for key, state_dict in raw.get("pid", {}).items():
        if isinstance(state_dict, dict):
            state.pid[key] = deserialize_pid(state_dict)

    for key, state_dict in raw.get("tpi", {}).items():
        if isinstance(state_dict, dict):
            state.tpi[key] = deserialize_tpi(state_dict)

    thermal_raw = raw.get("thermal", {})
    if isinstance(thermal_raw, dict):
        heating_power = thermal_raw.get("heating_power")
        heat_loss_rate = thermal_raw.get("heat_loss_rate")
        state.thermal = ThermalStats(
            heating_power=float(heating_power) if heating_power is not None else None,
            heat_loss_rate=(
                float(heat_loss_rate) if heat_loss_rate is not None else None
            ),
        )

    presets_raw = raw.get("presets", {})
    if isinstance(presets_raw, dict):
        for name, temp in presets_raw.items():
            try:
                state.presets[str(name)] = float(temp)
            except (TypeError, ValueError):
                continue

    return state


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------


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
            hass, CURRENT_VERSION, f"{DOMAIN}_{entry_id}_state"
        )
        self._entry_id = entry_id
        self._state = RuntimeState()
        self._dirty = False

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

    # -- Load / Save ---------------------------------------------------------

    async def load(self) -> None:
        """Load state from HA Store.  Applies migrations if needed."""
        raw = await self._store.async_load()
        if not raw or not isinstance(raw, dict):
            _LOGGER.debug(
                "better_thermostat [%s]: No persisted state found, starting fresh",
                self._entry_id,
            )
            return

        version = raw.get("version", 0)
        if version < 1:
            raw = _migrate_v0_to_v1(raw)

        self._state = _deserialize(raw)
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
        """Persist current state to HA Store unconditionally."""
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

    async def save_if_dirty(self) -> None:
        """Persist current state only if it has been modified since last save."""
        if self._dirty:
            await self.save()

    async def flush(self) -> None:
        """Flush unsaved changes -- call from async_will_remove_from_hass."""
        await self.save_if_dirty()
