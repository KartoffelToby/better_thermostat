"""Unified runtime state persistence for Better Thermostat.

Replaces four separate HA Store files with a single versioned store per
config entry. The StateManager owns all runtime state that must survive
a Home Assistant restart (calibration models, thermal stats, filters).

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
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import logging
import math
from time import monotonic
from typing import NoReturn

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

from .calibration.mpc import MpcState, TrvProfile
from .calibration.mpc_v2 import (
    MpcV2Params,
    MpcV2State,
    export_mpc_v2_state,
    import_mpc_v2_state,
)
from .calibration.mpc_v2.reid import ReidBuffer
from .calibration.mpc_v2.state import MpcV2Payload
from .calibration.mpc_v2_internals.plant import GAIN_HEATER_BOUNDS, TAU_ROOM_BOUNDS_MIN
from .calibration.pid import PIDState
from .calibration.tpi import TpiState
from .const import (
    DOMAIN,
    MAX_HEAT_LOSS,
    MAX_HEATING_POWER,
    MIN_HEAT_LOSS,
    MIN_HEATING_POWER,
)
from .stored_state import (
    StoredFilterState,
    StoredMpcState,
    StoredMpcV2Reid,
    StoredMpcV2State,
    StoredPidState,
    StoredRuntimeState,
    StoredThermalStats,
    StoredTpiState,
)
from .stored_values import (
    MAX_STORED_INT,
    MIN_STORED_INT,
    finite_or_none,
    is_json_object,
    stored_count,
    stored_float,
    stored_int,
)
from .thermal_learning import clamp


@dataclass
class MpcV2StateData:
    """Persistable per-key state for the MPC v2 controller.

    ``snapshot`` is the opaque payload returned by
    :meth:`MpcV2Controller.export_snapshot` — restored verbatim by
    :meth:`MpcV2Controller.restore_snapshot`. It is held as stored and only
    parsed when a controller is rebuilt from it, so a snapshot of a version
    this release cannot read stays in the store as it was. Top-level fields
    mirror the metadata the runtime state holds independently of the
    controller.
    """

    last_percent: float | None = None
    last_compute_ts: float = 0.0
    created_ts: float = 0.0
    outdoor_fallback_logged: bool = False
    snapshot: Mapping[str, object] = field(default_factory=dict)


@dataclass
class MpcV2ReidData:
    """Persisted result of an accepted offline re-identification.

    Carries the fitted plant-prior components plus the validation metrics
    of the accepting fit; the sample buffer that produced it is in-memory
    only and never persisted.
    """

    tau_room_minutes: float = 0.0
    gain_heater: float = 0.0
    fitted_ts: float = 0.0
    rmse_prior_kelvin: float = 0.0
    rmse_fit_kelvin: float = 0.0
    n_segments: int = 0


@dataclass
class MpcV2ReidRuntime:
    """In-memory collection/scheduling state for one MPC key.

    Lost on restart by design: the buffer refills within a day and the
    attempt timer simply starts over.
    """

    buffer: ReidBuffer = field(default_factory=ReidBuffer)
    last_fit_attempt_ts: float = 0.0
    fit_inflight: bool = False


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
class FilterState:
    """Runtime filter state that should survive a restart.

    Attributes
    ----------
    room_temperature_ema : float | None
        Exponential moving average of the external temperature.
    temperature_slope : float | None
        Estimated room-temperature slope.
    room_temperature_ema_recorded_at : float | None
        Wall-clock time the EMA was last updated at, in seconds since the
        epoch. A restart reads the downtime off it.
    """

    room_temperature_ema: float | None = None
    temperature_slope: float | None = None
    room_temperature_ema_recorded_at: float | None = None


@dataclass
class RuntimeState:
    """Complete runtime state for one BetterThermostat config entry.

    This is the top-level structure that gets serialized to a single
    HA Store file.
    """

    version: int = CURRENT_VERSION
    mpc: dict[str, MpcState] = field(default_factory=dict)
    mpc_v2: dict[str, MpcV2StateData] = field(default_factory=dict)
    mpc_v2_reid: dict[str, MpcV2ReidData] = field(default_factory=dict)
    pid: dict[str, PIDState] = field(default_factory=dict)
    tpi: dict[str, TpiState] = field(default_factory=dict)
    thermal: ThermalStats = field(default_factory=ThermalStats)
    filters: FilterState = field(default_factory=FilterState)
    # Learned preset temperatures are user input and live in the preset
    # number entities (RestoreEntity is correct for genuine UI state);
    # they are deliberately not duplicated here.


# Serialization helpers


def _copy_stored_tree(value: object) -> object:
    """Return a copy of *value* with every container rebuilt as JSON shapes.

    Mappings become new dicts and sequences (including ``deque`` and
    ``tuple``) become new lists, so the payload handed to the Store shares no
    container with the live state. Other values are returned as they are.
    """
    if isinstance(value, dict):
        return {key: _copy_stored_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [_copy_stored_tree(item) for item in value]
    return value


# Store keys of the ``FilterState`` fields. The stores on disk carry the
# values under these names, so they are the keys read and written.
_STORED_ROOM_TEMPERATURE_EMA = "external_temp_ema"
_STORED_TEMPERATURE_SLOPE = "temp_slope"

# Store keys of the ``MpcState`` fields whose stored name differs from the
# field name. The other fields are stored under their own names.
_STORED_MPC_KEYS = {
    "last_target_temperature": "last_target_C",
    "last_sensor_temperature": "last_sensor_temp_C",
    "last_room_temperature": "last_room_temp_C",
    "last_trv_temperature": "last_trv_temp",
    "last_trv_temperature_ts": "last_trv_temp_ts",
    "last_learn_temperature": "last_learn_temp",
    "virtual_temperature": "virtual_temp",
    "virtual_temperature_ts": "virtual_temp_ts",
    "last_room_temperature_ts": "last_room_temp_ts",
}

# Store keys of the ``PIDState`` fields whose stored name differs from the
# field name. The other fields are stored under their own names.
_STORED_PID_KEYS = {"last_target_temperature": "last_target_temp"}

# Store keys of the ``MpcV2ReidData`` fields whose stored name differs from
# the field name. The other fields are stored under their own names.
_STORED_MPC_V2_REID_KEYS = {
    "tau_room_minutes": "tau_room_min",
    "rmse_prior_kelvin": "rmse_prior_K",
    "rmse_fit_kelvin": "rmse_fit_K",
}


def write_mpc_state(state: MpcState) -> StoredMpcState:
    """Return the stored form of one MPC state.

    The fields with a legacy store name (:data:`_STORED_MPC_KEYS`) are
    written under that name.
    """
    return {
        "last_percent": state.last_percent,
        "last_update_ts": state.last_update_ts,
        "ema_slope": state.ema_slope,
        "gain_est": state.gain_est,
        "loss_est": state.loss_est,
        "ka_est": state.ka_est,
        "solar_gain_est": state.solar_gain_est,
        "last_time": state.last_time,
        "last_trv_temp": state.last_trv_temperature,
        "last_trv_temp_ts": state.last_trv_temperature_ts,
        "last_window_open_ts": state.last_window_open_ts,
        "dead_zone_hits": state.dead_zone_hits,
        "min_effective_percent": state.min_effective_percent,
        "last_learn_time": state.last_learn_time,
        "last_learn_temp": state.last_learn_temperature,
        "last_residual_time": state.last_residual_time,
        "virtual_temp": state.virtual_temperature,
        "virtual_temp_ts": state.virtual_temperature_ts,
        "last_room_temp_ts": state.last_room_temperature_ts,
        "perf_curve": {
            curve: dict(points) for curve, points in state.perf_curve.items()
        },
        "trv_profile": state.trv_profile,
        "profile_confidence": state.profile_confidence,
        "profile_samples": state.profile_samples,
        "u_integral": state.u_integral,
        "time_integral": state.time_integral,
        "last_integration_ts": state.last_integration_ts,
        "created_ts": state.created_ts,
        "loss_learn_count": state.loss_learn_count,
        "gain_learn_count": state.gain_learn_count,
        "is_calibration_active": state.is_calibration_active,
        "recent_errors": list(state.recent_errors),
        "regime_boost_active": state.regime_boost_active,
        "consecutive_insufficient_heat": state.consecutive_insufficient_heat,
        "kalman_P": state.kalman_P,
        "tolerance_hold_active": state.tolerance_hold_active,
        "last_target_C": state.last_target_temperature,
        "last_sensor_temp_C": state.last_sensor_temperature,
        "last_room_temp_C": state.last_room_temperature,
    }


def write_mpc_v2_state(data: MpcV2StateData) -> StoredMpcV2State:
    """Return the stored form of one persisted MPC v2 entry.

    The snapshot is copied as held, whatever version it carries.
    """
    return {
        "last_percent": data.last_percent,
        "last_compute_ts": data.last_compute_ts,
        "created_ts": data.created_ts,
        "outdoor_fallback_logged": data.outdoor_fallback_logged,
        "snapshot": {
            key: _copy_stored_tree(value) for key, value in data.snapshot.items()
        },
    }


def write_mpc_v2_reid(data: MpcV2ReidData) -> StoredMpcV2Reid:
    """Return the stored form of one accepted re-identification.

    The fields with a legacy store name (:data:`_STORED_MPC_V2_REID_KEYS`)
    are written under that name.
    """
    return {
        "tau_room_min": data.tau_room_minutes,
        "gain_heater": data.gain_heater,
        "fitted_ts": data.fitted_ts,
        "n_segments": data.n_segments,
        "rmse_prior_K": data.rmse_prior_kelvin,
        "rmse_fit_K": data.rmse_fit_kelvin,
    }


def write_pid_state(state: PIDState) -> StoredPidState:
    """Return the stored form of one PID state.

    The fields with a legacy store name (:data:`_STORED_PID_KEYS`) are
    written under that name.
    """
    return {
        "pid_integral": state.pid_integral,
        "pid_last_meas": state.pid_last_meas,
        "pid_last_error": state.pid_last_error,
        "pid_last_time": state.pid_last_time,
        "pid_kp": state.pid_kp,
        "pid_ki": state.pid_ki,
        "pid_kd": state.pid_kd,
        "auto_tune": state.auto_tune,
        "last_tune_ts": state.last_tune_ts,
        "last_delta_sign": state.last_delta_sign,
        "last_error_sign": state.last_error_sign,
        "previous_abs_error": state.previous_abs_error,
        "last_abs_error": state.last_abs_error,
        "ema_slope": state.ema_slope,
        "last_percent": state.last_percent,
        "last_output_change_ts": state.last_output_change_ts,
        "last_target_temp": state.last_target_temperature,
    }


def write_tpi_state(state: TpiState) -> StoredTpiState:
    """Return the stored form of one TPI state."""
    return {"last_percent": state.last_percent, "last_update_ts": state.last_update_ts}


def write_thermal(stats: ThermalStats) -> StoredThermalStats:
    """Return the stored form of the learned thermal characteristics."""
    return {
        "heating_power": stats.heating_power,
        "heat_loss_rate": stats.heat_loss_rate,
    }


def write_filters(filters: FilterState) -> StoredFilterState:
    """Return the stored form of the filter state, under its legacy store keys."""
    return {
        "external_temp_ema": filters.room_temperature_ema,
        "temp_slope": filters.temperature_slope,
        "room_temperature_ema_recorded_at": filters.room_temperature_ema_recorded_at,
    }


def _serialize(state: RuntimeState) -> StoredRuntimeState:
    """Return the payload the Store writes for *state*.

    Every container is a fresh copy, so the live state can change while
    the Store still holds the payload.
    """
    return {
        "version": state.version,
        "mpc": {key: write_mpc_state(mpc) for key, mpc in state.mpc.items()},
        "mpc_v2": {key: write_mpc_v2_state(data) for key, data in state.mpc_v2.items()},
        "mpc_v2_reid": {
            key: write_mpc_v2_reid(data) for key, data in state.mpc_v2_reid.items()
        },
        "pid": {key: write_pid_state(pid) for key, pid in state.pid.items()},
        "tpi": {key: write_tpi_state(tpi) for key, tpi in state.tpi.items()},
        "thermal": write_thermal(state.thermal),
        "filters": write_filters(state.filters),
    }


class _PoisonedStateError(ValueError):
    """A stored entry carries a mathematical anomaly (NaN/inf)."""


def _within(value: float, bounds: tuple[float, float]) -> bool:
    """Return whether *value* lies inside *bounds*, inclusive at both ends.

    A NaN answers ``False`` on both comparisons, so it reads as outside.
    """
    low, high = bounds
    return low <= value <= high


def _refuse_null(stored: str) -> NoReturn:
    """Reject a stored null where the declared type cannot hold one.

    A null where a number is declared is how a non-finite value gets back
    out of a file this module wrote: the store's encoder writes NaN and
    infinity as ``null``. Where anything else is declared it is simply a
    value that cannot be held. Neither leaves anything usable, so the
    entry gets the disposal :func:`_finite_or_poison` gives corrupt math.

    *stored* is the key the file holds the value under, or the name of the
    collection element it was found in.
    """
    raise _PoisonedStateError(f"{stored} is null, which its declared type cannot hold")


def _finite_or_poison(value: object, attr: str) -> float:
    """Parse one stored float; a non-finite number poisons the entry.

    Wrong types merely skip the field (schema evolution), but NaN or
    infinity means the entry's math is corrupt — the rest of it cannot be
    trusted either, so the caller keeps none of the entry's stored values
    and the learning they carried starts over.
    """
    number = stored_float(value)
    if not math.isfinite(number):
        raise _PoisonedStateError(f"{attr} is non-finite")
    return number


def _report_unreadable_field(attr: str, kind: str, key: str | None) -> None:
    """Name a stored field that keeps its default because it cannot be read.

    Past the load path the field carries the default a first start leaves
    there, and a value the store lost looks exactly like one it never
    held, so this is the only place that can still say so.
    """
    _LOGGER.warning(
        "better_thermostat: stored %s state for %s has an unusable %s, "
        "continuing without it",
        kind,
        key or "an unnamed state entry",
        attr,
        exc_info=True,
    )


def _discard_poisoned_entry(
    error: _PoisonedStateError, kind: str, key: str | None, poisoned: list[str] | None
) -> None:
    """Name an entry whose stored values are discarded, and note it when asked.

    The entry's learning starts over from defaults, so the report names the
    room it belonged to as well as the value that cost it.
    """
    _LOGGER.warning(
        "better_thermostat: stored %s state for %s: %s; discarding that "
        "entry's stored values",
        kind,
        key or "an unnamed state entry",
        error,
    )
    if poisoned is not None:
        poisoned.append(f"{kind}:{key}")


def _finite_element(value: object, attr: str) -> float:
    """Parse one number stored inside a collection field.

    ``recent_errors`` and the bins of ``perf_curve`` are declared to hold
    plain numbers, and the field guards rule only on the collection
    itself. A null among its numbers is the same saved NaN a null in a
    numeric field is — the store's encoder writes both that way — and
    costs the entry its stored values just as one does.
    """
    if value is None:
        _refuse_null(attr)
    return _finite_or_poison(value, attr)


def _finite_perf_curve(
    value: Mapping[str, object],
) -> dict[str, dict[str, float | int]]:
    """Copy a stored performance curve, parsing every statistic in it.

    What the offending value is decides what it costs. A bin that is not a
    mapping of statistics, or a statistic ``float()`` refuses outright such
    as ``"later"``, raises one of the errors the caller skips a field on:
    the curve is lost and the rest of the entry survives. A statistic that
    is null or parses as a non-finite number raises :class:`_PoisonedStateError`
    instead — the bins are declared to hold plain numbers, so either one is
    corrupt math, and the entry keeps none of its stored values.
    """
    curve: dict[str, dict[str, float | int]] = {}
    for label, stats in value.items():
        if not is_json_object(stats):
            raise TypeError("perf_curve bin is not a mapping of statistics")
        curve[label] = {
            name: _finite_element(stat, "perf_curve statistic")
            for name, stat in stats.items()
        }
    return curve


def _not_a_collection(value: object, stored: str) -> TypeError:
    """Return the error a collection field raises for a value of another shape.

    A string that parses as a non-finite number is still the saved NaN it
    spells and poisons the entry first; anything else costs only the field.
    """
    _finite_or_poison(value, stored)
    return TypeError(f"{stored} is not a collection")


def _stored_perf_curve(value: object, stored: str) -> dict[str, dict[str, float | int]]:
    """Parse ``perf_curve``: a mapping of bins, each a mapping of statistics."""
    if is_json_object(value):
        return _finite_perf_curve(value)
    raise _not_a_collection(value, stored)


def _stored_recent_errors(value: object, stored: str) -> deque[float]:
    """Parse ``recent_errors`` into the twenty-sample window the state keeps."""
    if isinstance(value, (list, tuple)):
        return deque(
            (_finite_element(item, "recent_errors element") for item in value),
            maxlen=20,
        )
    raise _not_a_collection(value, stored)


def _stored_tally(value: object, stored: str) -> int:
    """Parse an integer that tallies occurrences; an unusable one restores as 0."""
    return stored_count(value)


def _stored_direction(value: object, stored: str) -> int:
    """Parse an integer that records which way a quantity last moved.

    A negative value is meaningful here, so only the storable range applies.
    """
    return stored_int(value)


def _stored_flag(value: object, stored: str) -> bool:
    """Parse a flag with ``bool()``'s truthiness."""
    return bool(value)


def _stored_trv_profile(value: object, stored: str) -> TrvProfile:
    """Parse a learned TRV profile; a name this version does not know restores as unknown."""
    try:
        return TrvProfile(value)
    except ValueError:
        return TrvProfile.UNKNOWN


# Parses one stored value into a field's type. The second argument is the
# key the value is stored under, for the error a corrupt value raises.
type _FieldParser[T] = Callable[[object, str], T]


class _StoredEntryReader:
    """Read the fields of one stored entry, each into its declared type.

    A field the entry does not hold keeps the default it is read with, and
    so does one whose stored value its parser refuses; that one is named.
    A stored null is a value only for a field read with :meth:`optional`.
    A null anywhere else, and a parser that finds corrupt math, raise
    :class:`_PoisonedStateError`, which costs the caller the whole entry.

    Parameters
    ----------
    raw : Mapping[str, object]
        the stored entry to read
    kind : str
        the store section the entry belongs to, for the reports
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    renamed : Mapping[str, str]
        the store keys of the fields stored under a name other than their own
    field_name_fallback : bool
        whether a renamed field is also read under its field name, the
        spelling a snapshot taken with ``asdict`` carries; the store key wins
    """

    def __init__(
        self,
        raw: Mapping[str, object],
        kind: str,
        key: str | None,
        *,
        renamed: Mapping[str, str] | None = None,
        field_name_fallback: bool = False,
    ) -> None:
        self._raw = raw
        self._kind = kind
        self._key = key
        self._renamed: Mapping[str, str] = renamed or {}
        self._field_name_fallback = field_name_fallback

    def required[T](self, name: str, parse: _FieldParser[T], default: T) -> T:
        """Return field *name*, whose declared type holds no ``None``.

        Parameters
        ----------
        name : str
            the field's name
        parse : _FieldParser[T]
            turns the stored value into the field's type
        default : T
            the value a missing or unusable stored value leaves in place

        Returns
        -------
        T
            the parsed value, or *default*

        Raises
        ------
        _PoisonedStateError
            when the stored value is null or corrupt math
        """
        found = self._lookup(name)
        if found is None:
            return default
        stored, value = found
        if value is None:
            _refuse_null(stored)
        return self._parse(stored, value, parse, default)

    def optional[T](
        self, name: str, parse: _FieldParser[T], default: T | None
    ) -> T | None:
        """Return field *name*, whose declared type admits ``None``.

        Parameters
        ----------
        name : str
            the field's name
        parse : _FieldParser[T]
            turns a stored value other than null into the field's type
        default : T | None
            the value a missing or unusable stored value leaves in place

        Returns
        -------
        T | None
            the parsed value, None for a stored null, or *default*

        Raises
        ------
        _PoisonedStateError
            when the stored value is corrupt math
        """
        found = self._lookup(name)
        if found is None:
            return default
        stored, value = found
        if value is None:
            return None
        return self._parse(stored, value, parse, default)

    def _lookup(self, name: str) -> tuple[str, object] | None:
        """Return the store key and stored value of field *name*, if held."""
        stored = self._renamed.get(name, name)
        if stored in self._raw:
            return stored, self._raw[stored]
        if self._field_name_fallback and name in self._raw:
            return stored, self._raw[name]
        return None

    def _parse[T](
        self, stored: str, value: object, parse: _FieldParser[T], default: T
    ) -> T:
        """Parse *value*, or name it and return *default* when it is unusable."""
        try:
            return parse(value, stored)
        except _PoisonedStateError:
            raise
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(stored, self._kind, self._key)
            return default


def deserialize_mpc(
    raw: Mapping[str, object],
    *,
    key: str | None = None,
    poisoned: list[str] | None = None,
) -> MpcState:
    """Deserialize a single MPC state dict into an MpcState dataclass.

    A non-finite number in a float field rejects the whole entry: learning
    restarts from defaults rather than continuing on corrupt math. That
    covers the numbers inside ``perf_curve`` and ``recent_errors`` as well
    as the float fields themselves. This state's integer fields are all
    tallies and are read as counts instead, so a value ``int()`` cannot
    make sense of — ``"NaN"`` and ``"Infinity"`` among them, the spellings
    a stored file delivers a non-finite number in — restores as 0 and
    leaves the rest of the entry standing.

    A stored ``null`` rejects the entry wherever the declared type has no
    ``None``, the tallies included, because that is the shape a saved NaN
    comes back in.

    The store spells a few fields under their own key
    (:data:`_STORED_MPC_KEYS`); a snapshot taken with ``asdict`` spells them
    by field name. The store key wins.

    Parameters
    ----------
    raw : Mapping[str, object]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    poisoned : list[str] | None
        collects the entry's section and key when a non-finite value
        discards its stored values
    """
    read = _StoredEntryReader(
        raw, "mpc", key, renamed=_STORED_MPC_KEYS, field_name_fallback=True
    )
    number = _finite_or_poison
    held = MpcState()
    try:
        return MpcState(
            last_percent=read.optional("last_percent", number, held.last_percent),
            last_update_ts=read.required("last_update_ts", number, held.last_update_ts),
            last_target_temperature=read.optional(
                "last_target_temperature", number, held.last_target_temperature
            ),
            ema_slope=read.optional("ema_slope", number, held.ema_slope),
            gain_est=read.optional("gain_est", number, held.gain_est),
            loss_est=read.optional("loss_est", number, held.loss_est),
            ka_est=read.optional("ka_est", number, held.ka_est),
            solar_gain_est=read.optional("solar_gain_est", number, held.solar_gain_est),
            last_time=read.required("last_time", number, held.last_time),
            last_trv_temperature=read.optional(
                "last_trv_temperature", number, held.last_trv_temperature
            ),
            last_trv_temperature_ts=read.required(
                "last_trv_temperature_ts", number, held.last_trv_temperature_ts
            ),
            last_window_open_ts=read.required(
                "last_window_open_ts", number, held.last_window_open_ts
            ),
            dead_zone_hits=read.required(
                "dead_zone_hits", _stored_tally, held.dead_zone_hits
            ),
            min_effective_percent=read.optional(
                "min_effective_percent", number, held.min_effective_percent
            ),
            last_learn_time=read.optional(
                "last_learn_time", number, held.last_learn_time
            ),
            last_learn_temperature=read.optional(
                "last_learn_temperature", number, held.last_learn_temperature
            ),
            last_residual_time=read.optional(
                "last_residual_time", number, held.last_residual_time
            ),
            virtual_temperature=read.optional(
                "virtual_temperature", number, held.virtual_temperature
            ),
            virtual_temperature_ts=read.required(
                "virtual_temperature_ts", number, held.virtual_temperature_ts
            ),
            last_sensor_temperature=read.optional(
                "last_sensor_temperature", number, held.last_sensor_temperature
            ),
            last_room_temperature=read.optional(
                "last_room_temperature", number, held.last_room_temperature
            ),
            last_room_temperature_ts=read.required(
                "last_room_temperature_ts", number, held.last_room_temperature_ts
            ),
            perf_curve=read.required("perf_curve", _stored_perf_curve, held.perf_curve),
            trv_profile=read.required(
                "trv_profile", _stored_trv_profile, held.trv_profile
            ),
            profile_confidence=read.required(
                "profile_confidence", number, held.profile_confidence
            ),
            profile_samples=read.required(
                "profile_samples", _stored_tally, held.profile_samples
            ),
            u_integral=read.required("u_integral", number, held.u_integral),
            time_integral=read.required("time_integral", number, held.time_integral),
            last_integration_ts=read.required(
                "last_integration_ts", number, held.last_integration_ts
            ),
            created_ts=read.required("created_ts", number, held.created_ts),
            loss_learn_count=read.required(
                "loss_learn_count", _stored_tally, held.loss_learn_count
            ),
            gain_learn_count=read.required(
                "gain_learn_count", _stored_tally, held.gain_learn_count
            ),
            is_calibration_active=read.required(
                "is_calibration_active", _stored_flag, held.is_calibration_active
            ),
            recent_errors=read.required(
                "recent_errors", _stored_recent_errors, held.recent_errors
            ),
            regime_boost_active=read.required(
                "regime_boost_active", _stored_flag, held.regime_boost_active
            ),
            consecutive_insufficient_heat=read.required(
                "consecutive_insufficient_heat",
                _stored_tally,
                held.consecutive_insufficient_heat,
            ),
            kalman_P=read.required("kalman_P", number, held.kalman_P),
            tolerance_hold_active=read.required(
                "tolerance_hold_active", _stored_flag, held.tolerance_hold_active
            ),
        )
    except _PoisonedStateError as error:
        _discard_poisoned_entry(error, "mpc", key, poisoned)
        return MpcState()


def deserialize_mpc_v2(
    raw: Mapping[str, object],
    *,
    key: str | None = None,
    poisoned: list[str] | None = None,
) -> MpcV2StateData | None:
    """Deserialize a single MPC v2 state dict; ``None`` if the entry is corrupt.

    A non-finite float in ``last_percent``, ``last_compute_ts`` or
    ``created_ts`` rejects the whole entry, ``snapshot`` included: the
    observer state in it was exported by the same controller whose command
    or timestamp went corrupt. Returning ``None`` keeps the key out of the
    restored state, which is what makes the restart a cold one: an entry
    left in place with an empty ``snapshot`` would still rehydrate into a
    controller and count as initialised, so its Kalman estimate would stay
    at the construction default rather than being seeded from the first
    measurement.

    Those three are the only fields parsed; ``outdoor_fallback_logged`` and
    ``snapshot`` are read after them and reach no guard. A ``snapshot``
    that is null or not a mapping therefore keeps the entry and leaves the
    empty default in its place — the very shape described above. Only a
    store this integration did not write can hold one: what it saves is
    always a mapping.

    Parameters
    ----------
    raw : Mapping[str, object]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    poisoned : list[str] | None
        collects the entry's section and key when a non-finite value
        discards its stored values

    Returns
    -------
    MpcV2StateData | None
        the parsed entry, or None when it is corrupt
    """
    read = _StoredEntryReader(raw, "mpc_v2", key)
    number = _finite_or_poison
    held = MpcV2StateData()
    try:
        state = MpcV2StateData(
            last_percent=read.optional("last_percent", number, held.last_percent),
            last_compute_ts=read.required(
                "last_compute_ts", number, held.last_compute_ts
            ),
            created_ts=read.required("created_ts", number, held.created_ts),
        )
    except _PoisonedStateError as error:
        _discard_poisoned_entry(error, "mpc_v2", key, poisoned)
        return None
    state.outdoor_fallback_logged = bool(raw.get("outdoor_fallback_logged", False))
    snapshot = raw.get("snapshot")
    if is_json_object(snapshot):
        state.snapshot = dict(snapshot)
    elif snapshot is not None:
        # The snapshot holds the learned controller state, so one of any
        # other shape is named like every other field the load drops.
        _LOGGER.warning(
            "better_thermostat: stored mpc_v2 state for %s has an unusable "
            "snapshot, continuing without it",
            key or "an unnamed state entry",
        )
    return state


def deserialize_mpc_v2_reid(
    raw: Mapping[str, object],
    *,
    key: str | None = None,
    poisoned: list[str] | None = None,
) -> MpcV2ReidData | None:
    """Deserialize a persisted re-identification result; None if malformed.

    Returning ``None`` leaves the entry out of the restored state, so the
    plant-prior lookup moves on: to another stored result sharing this
    entity's key prefix, and failing that to the heat-loss-derived prior.

    Three checks reject an entry. A NaN or infinity in one of the five
    float fields; a stored ``null`` in any of the six, since none of them
    is declared to hold one; and a ``tau_room_minutes`` or ``gain_heater``
    outside :data:`TAU_ROOM_BOUNDS_MIN` / :data:`GAIN_HEATER_BOUNDS`,
    inclusive at both ends, since those two are the pair that seeds the
    prior and the plant's room dynamics divide by ``tau_room_min``.

    The magnitude check rejects rather than clamps: this deserialiser's
    contract is that a corrupt entry is left out so the lookup moves on to
    the heat-loss-derived prior, whereas clamping would present a nonsense
    stored value as a learned result sitting at the edge of the band.

    A float field that does not parse keeps its default, which leaves the
    entry usable as the schema grows. ``n_segments`` is metadata: a value
    that is not a storable count falls back to 0 and the entry survives —
    except a null, which is refused there as in every other field.

    Every rejection and every skipped field is logged, since past this
    point a dropped result looks like one that was never learned.

    Parameters
    ----------
    raw : Mapping[str, object]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    poisoned : list[str] | None
        collects the entry's section and key when a non-finite value or an
        out-of-band fit discards its stored values
    """
    read = _StoredEntryReader(raw, "mpc_v2_reid", key, renamed=_STORED_MPC_V2_REID_KEYS)
    number = _finite_or_poison
    held = MpcV2ReidData()
    try:
        state = MpcV2ReidData(
            tau_room_minutes=read.required(
                "tau_room_minutes", number, held.tau_room_minutes
            ),
            gain_heater=read.required("gain_heater", number, held.gain_heater),
            fitted_ts=read.required("fitted_ts", number, held.fitted_ts),
            rmse_prior_kelvin=read.required(
                "rmse_prior_kelvin", number, held.rmse_prior_kelvin
            ),
            rmse_fit_kelvin=read.required(
                "rmse_fit_kelvin", number, held.rmse_fit_kelvin
            ),
            n_segments=read.required("n_segments", _stored_tally, held.n_segments),
        )
    except _PoisonedStateError as error:
        _discard_poisoned_entry(error, "mpc_v2_reid", key, poisoned)
        return None
    # A result whose fitted components lie outside the plausible band cannot
    # seed a plant prior. The band is two-sided on both: too small a
    # ``tau_room_minutes`` and the room dynamics blow up, too large and they
    # freeze, and either rail pins the commanded valve.
    for attr, value, bounds in (
        ("tau_room_min", state.tau_room_minutes, TAU_ROOM_BOUNDS_MIN),
        ("gain_heater", state.gain_heater, GAIN_HEATER_BOUNDS),
    ):
        if not _within(value, bounds):
            _LOGGER.warning(
                "better_thermostat: stored mpc_v2_reid result for %s has %s=%s "
                "outside the plausible band %s; falling back to the derived "
                "plant prior",
                key or "an unnamed state entry",
                attr,
                value,
                bounds,
            )
            if poisoned is not None:
                poisoned.append(f"mpc_v2_reid:{key}")
            return None
    return state


def deserialize_pid(
    raw: Mapping[str, object],
    *,
    key: str | None = None,
    poisoned: list[str] | None = None,
) -> PIDState:
    """Deserialize a single PID state dict into a PIDState dataclass.

    A non-finite number in a float field rejects the whole entry: learning
    restarts from defaults rather than continuing on corrupt math. This
    state's two integer fields record a direction and are read as integers
    instead, so a value ``int()`` cannot make sense of — ``"NaN"`` and
    ``"Infinity"`` among them, the spellings a stored file delivers a
    non-finite number in — keeps its default and leaves the rest of the
    entry standing.

    A stored ``null`` rejects the entry wherever the field's type has no
    ``None``, because that is the shape a saved NaN comes back in; both
    direction fields are declared ``int | None`` and keep theirs.

    Parameters
    ----------
    raw : Mapping[str, object]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    poisoned : list[str] | None
        collects the entry's section and key when a non-finite value
        discards its stored values
    """
    read = _StoredEntryReader(raw, "pid", key, renamed=_STORED_PID_KEYS)
    number = _finite_or_poison
    held = PIDState()
    try:
        return PIDState(
            pid_integral=read.required("pid_integral", number, held.pid_integral),
            pid_last_meas=read.optional("pid_last_meas", number, held.pid_last_meas),
            pid_last_error=read.optional("pid_last_error", number, held.pid_last_error),
            pid_last_time=read.required("pid_last_time", number, held.pid_last_time),
            pid_kp=read.optional("pid_kp", number, held.pid_kp),
            pid_ki=read.optional("pid_ki", number, held.pid_ki),
            pid_kd=read.optional("pid_kd", number, held.pid_kd),
            auto_tune=read.optional("auto_tune", _stored_flag, held.auto_tune),
            last_tune_ts=read.required("last_tune_ts", number, held.last_tune_ts),
            last_delta_sign=read.optional(
                "last_delta_sign", _stored_direction, held.last_delta_sign
            ),
            last_error_sign=read.optional(
                "last_error_sign", _stored_direction, held.last_error_sign
            ),
            previous_abs_error=read.optional(
                "previous_abs_error", number, held.previous_abs_error
            ),
            last_abs_error=read.optional("last_abs_error", number, held.last_abs_error),
            ema_slope=read.optional("ema_slope", number, held.ema_slope),
            last_percent=read.required("last_percent", number, held.last_percent),
            last_output_change_ts=read.required(
                "last_output_change_ts", number, held.last_output_change_ts
            ),
            last_target_temperature=read.optional(
                "last_target_temperature", number, held.last_target_temperature
            ),
        )
    except _PoisonedStateError as error:
        _discard_poisoned_entry(error, "pid", key, poisoned)
        return PIDState()


def deserialize_tpi(
    raw: Mapping[str, object],
    *,
    key: str | None = None,
    poisoned: list[str] | None = None,
) -> TpiState:
    """Deserialize a single TPI state dict into a TpiState dataclass.

    A non-finite numeric field rejects the whole entry: learning
    restarts from defaults rather than continuing on corrupt math. A
    stored ``null`` counts as one wherever the field's type has no
    ``None``, because that is the shape a saved NaN comes back in.

    Parameters
    ----------
    raw : Mapping[str, object]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    poisoned : list[str] | None
        collects the entry's section and key when a non-finite value
        discards its stored values
    """
    read = _StoredEntryReader(raw, "tpi", key)
    held = TpiState()
    try:
        return TpiState(
            last_percent=read.optional(
                "last_percent", _finite_or_poison, held.last_percent
            ),
            last_update_ts=read.required(
                "last_update_ts", _finite_or_poison, held.last_update_ts
            ),
        )
    except _PoisonedStateError as error:
        _discard_poisoned_entry(error, "tpi", key, poisoned)
        return TpiState()


def _stored_section(
    raw: Mapping[str, object], section: str, poisoned: list[str] | None
) -> Mapping[str, object]:
    """Return one section of the store, or an empty one when it has none.

    A section of any other shape than a mapping is dropped and named: past
    the load path its entities start from defaults, like on a first start.
    It is noted in *poisoned* as well, since those defaults replace it on
    the next save.
    """
    value = raw.get(section, {})
    if is_json_object(value):
        return value
    _LOGGER.warning(
        "better_thermostat: stored %s section is not a mapping; its entries "
        "start from defaults",
        section,
    )
    if poisoned is not None:
        poisoned.append(section)
    return {}


def _stored_entries(
    raw: Mapping[str, object], section: str, poisoned: list[str] | None
) -> list[tuple[str, Mapping[str, object]]]:
    """Return the entries of one keyed section that are mappings.

    An entry of any other shape is dropped, named with its key and noted
    in *poisoned*.
    """
    entries: list[tuple[str, Mapping[str, object]]] = []
    for key, entry in _stored_section(raw, section, poisoned).items():
        if is_json_object(entry):
            entries.append((key, entry))
            continue
        _LOGGER.warning(
            "better_thermostat: stored %s entry for %s is not a mapping; "
            "it starts from defaults",
            section,
            key,
        )
        if poisoned is not None:
            poisoned.append(f"{section}:{key}")
    return entries


def _stored_optional_number(
    values: Mapping[str, object], section: str, attr: str
) -> float | None:
    """Return one optional number of an unkeyed section, naming an unusable one.

    A missing value and a stored null are a value never learned and pass
    as ``None`` silently. Anything else that is not a finite number is
    dropped as well, and named, since past the load path it looks like one
    never learned.
    """
    value = values.get(attr)
    number = finite_or_none(value)
    if number is None and value is not None:
        _LOGGER.warning(
            "better_thermostat: stored %s section has an unusable %s, "
            "continuing without it",
            section,
            attr,
        )
    return number


def _deserialize(
    raw: Mapping[str, object], *, poisoned: list[str] | None = None
) -> RuntimeState:
    """Reconstruct a RuntimeState from a raw dict (loaded from Store).

    *poisoned* collects the section, or the section and key, of every part
    of the store whose stored values are discarded as a whole: an entry a
    non-finite number reset, a re-identification result outside its
    plausible band, and a section or entry of the wrong shape.
    """
    state = RuntimeState(version=_stored_version(raw, CURRENT_VERSION))

    for key, entry in _stored_entries(raw, "mpc", poisoned):
        state.mpc[key] = deserialize_mpc(entry, key=key, poisoned=poisoned)

    for key, entry in _stored_entries(raw, "mpc_v2", poisoned):
        mpc_v2 = deserialize_mpc_v2(entry, key=key, poisoned=poisoned)
        if mpc_v2 is not None:
            state.mpc_v2[key] = mpc_v2

    for key, entry in _stored_entries(raw, "mpc_v2_reid", poisoned):
        reid = deserialize_mpc_v2_reid(entry, key=key, poisoned=poisoned)
        if reid is not None:
            state.mpc_v2_reid[key] = reid

    for key, entry in _stored_entries(raw, "pid", poisoned):
        state.pid[key] = deserialize_pid(entry, key=key, poisoned=poisoned)

    for key, entry in _stored_entries(raw, "tpi", poisoned):
        state.tpi[key] = deserialize_tpi(entry, key=key, poisoned=poisoned)

    thermal_raw = _stored_section(raw, "thermal", poisoned)
    state.thermal = ThermalStats(
        heating_power=_stored_optional_number(thermal_raw, "thermal", "heating_power"),
        heat_loss_rate=_stored_optional_number(
            thermal_raw, "thermal", "heat_loss_rate"
        ),
    )

    filters_raw = _stored_section(raw, "filters", poisoned)
    state.filters = FilterState(
        room_temperature_ema=_stored_optional_number(
            filters_raw, "filters", _STORED_ROOM_TEMPERATURE_EMA
        ),
        temperature_slope=_stored_optional_number(
            filters_raw, "filters", _STORED_TEMPERATURE_SLOPE
        ),
        room_temperature_ema_recorded_at=_stored_optional_number(
            filters_raw, "filters", "room_temperature_ema_recorded_at"
        ),
    )

    # A legacy "presets" section is ignored: preset temperatures are UI
    # state owned by the preset number entities.

    return state


def _stored_version(raw: Mapping[str, object], default: int) -> int:
    """Return the schema version a store payload declares, or *default*.

    Any JSON number is read as its integer part, rounded down, which keeps
    the ``version < 1`` decision of :meth:`StateManager.load` for every
    number, a bool included. Whatever is not a finite number, a stored
    null or a string among them, raises, so ``load()`` treats the payload
    as unreadable.

    Parameters
    ----------
    raw : Mapping[str, object]
        the store payload
    default : int
        the version of a payload without a ``"version"`` key

    Returns
    -------
    int
        the declared version, rounded down

    Raises
    ------
    TypeError
        when the version is not a number
    ValueError
        when the version is NaN
    OverflowError
        when the version is infinite, or outside the integer range the Store
        can write back
    """
    if "version" not in raw:
        return default
    value = raw["version"]
    if not isinstance(value, int | float):
        raise TypeError(f"store version is not a number: {value!r}")
    version = math.floor(value)
    if not MIN_STORED_INT <= version <= MAX_STORED_INT:
        raise OverflowError(f"store version cannot be written back: {value!r}")
    return version


def _mpc_v2_payload(data: MpcV2StateData) -> MpcV2Payload:
    """Return a persisted MPC v2 entry in the form a live state is imported from."""
    return MpcV2Payload(
        last_percent=data.last_percent,
        last_compute_ts=data.last_compute_ts,
        created_ts=data.created_ts,
        outdoor_fallback_logged=data.outdoor_fallback_logged,
        snapshot=data.snapshot,
    )


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


# Keys of the per-thermostat sections


# The middle segment of a learned-state key that belongs to the room as a
# whole rather than to one thermostat.
GROUP_KEY_SEGMENT = "group"


def _is_bucket_tag(part: str) -> bool:
    """Return whether ``part`` is a target bucket tag such as ``t21.0``."""
    if part == "tunknown":
        return True
    if not part.startswith("t"):
        return False
    try:
        float(part[1:])
    except ValueError:
        return False
    return True


def _split_thermostat_key(key: str) -> tuple[str, str, str] | None:
    """Split a per-thermostat key into its head, thermostat segment and tail.

    Learned state is keyed ``<unique_id>:<segment>:t<bucket>``, where the
    segment is a thermostat's entity id or :data:`GROUP_KEY_SEGMENT`, and a
    TRV's PID loop entry is keyed ``<unique_id>:<entity_id>``. The tail is
    ``:t<bucket>`` for the first shape and empty for the second. A key of any
    other shape, such as the shared ``<unique_id>:reid``, returns ``None``.
    Entity ids hold no colon but a dot between domain and object id, so the
    segment is read from the right.
    """
    head, separator, last = key.rpartition(":")
    if not separator:
        return None
    if _is_bucket_tag(last):
        unique_id, separator, segment = head.rpartition(":")
        if not separator:
            return None
        return unique_id, segment, f":{last}"
    if "." in last:
        return head, last, ""
    return None


def thermostat_of_key(key: str) -> str | None:
    """Return the thermostat segment of a learned-state key, or ``None``.

    See :func:`_split_thermostat_key` for the key shapes that name a
    thermostat.
    """
    parts = _split_thermostat_key(key)
    return None if parts is None else parts[1]


def _key_for_thermostat(key: str, entity_id: str) -> str:
    """Return ``key`` with its thermostat segment replaced by ``entity_id``."""
    parts = _split_thermostat_key(key)
    if parts is None:
        return key
    unique_id, _, tail = parts
    return f"{unique_id}:{entity_id}{tail}"


# Migration


def _migrate_v0_to_v1(raw: Mapping[str, object]) -> dict[str, object]:
    """Migrate from unversioned (v0) format to v1.

    v0 is the legacy format where MPC/PID/TPI/thermal data lived in
    separate Store files.  The result is a new mapping holding every key
    of *raw* plus a default for each v1 key it lacks; *raw* itself is left
    as it was loaded.
    """
    migrated = dict(raw)
    migrated.setdefault("version", 1)
    for section in ("mpc", "pid", "tpi", "thermal", "filters"):
        migrated.setdefault(section, {})
    return migrated


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
        self._store: Store[Mapping[str, object]] = Store(
            hass, CURRENT_VERSION, _store_key(entry_id)
        )
        self._hass = hass
        self._entry_id = entry_id
        self._state = RuntimeState()
        # Live MPC v2 controllers, held in memory across cycles. The persisted
        # ``_state.mpc_v2`` snapshots are folded in only at save time.
        self._mpc_v2_live: dict[str, MpcV2State] = {}
        # Re-identification sample buffers and scheduling flags, in-memory only.
        self._mpc_v2_reid_live: dict[str, MpcV2ReidRuntime] = {}
        self._dirty = False
        self._delay_save_pending = False
        # A payload load() could not read in full and could not set aside
        # either. The live store holds its only copy, so nothing is written
        # over it until the copy exists.
        self._payload_awaiting_copy: Mapping[str, object] | None = None
        # Whether the failing copy has been reported at WARNING already; each
        # further attempt that fails is logged at DEBUG only.
        self._copy_failure_reported = False
        # When the copy is next tried, the wait after that, and whether a try
        # is under way.
        self._copy_retry_at = 0.0
        self._copy_retry_seconds = COPY_RETRY_FIRST_S
        self._copy_retry_running = False
        # The background task of the latest try, or of the latest
        # save_unless_closed(); flush() waits for it.
        self._copy_retry_task: asyncio.Task[None] | None = None
        # The timer that tries the copy at ``_copy_retry_at`` on its own, and
        # whether the manager still starts one; after close() it does not.
        self._copy_retry_timer: CALLBACK_TYPE | None = None
        self._copy_retry_timed = True
        # The last runtime save skipped while the copy is pending, as
        # ``(pre_save, delay_seconds)``; the timer schedules it once the copy exists.
        self._held_save: tuple[Callable[[], None] | None, float] | None = None
        # Set by close(); a closed manager schedules no delayed save.
        self._closed = False

    @staticmethod
    async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
        """Delete the per-entry store file when its config entry is removed.

        Parameters
        ----------
        hass : HomeAssistant
            The Home Assistant instance.
        entry_id : str
            Config entry identifier whose store file is removed.
        """
        await Store(hass, CURRENT_VERSION, _store_key(entry_id)).async_remove()
        for copy in range(QUARANTINE_COPIES):
            await Store(
                hass, QUARANTINE_VERSION, _quarantine_key(entry_id, copy)
            ).async_remove()

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
                import_mpc_v2_state(_mpc_v2_payload(persisted), params, key=key)
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
        memory and never serialises it. An export the deserialiser rejects
        leaves the key's stored entry as it was, so a corrupt ``last_percent``,
        ``last_compute_ts`` or ``created_ts`` does not overwrite the entry
        already stored. That is all the rejection buys: ``snapshot`` is
        copied verbatim, so a non-finite number in the observer state does
        reach the file, where the encoder writes it as ``null``.
        """
        for key, live in self._mpc_v2_live.items():
            exported = export_mpc_v2_state(live)
            if exported is None:
                continue
            persistable = deserialize_mpc_v2(exported, key=key)
            if persistable is not None:
                self._state.mpc_v2[key] = persistable

    def get_mpc_v2_reid(self, key: str) -> MpcV2ReidData | None:
        """Return the persisted re-identification result for a key, if any."""
        return self._state.mpc_v2_reid.get(key)

    def get_mpc_v2_reid_runtime(self, key: str) -> MpcV2ReidRuntime:
        """Return the in-memory re-ID collection state, building it on first use."""
        runtime = self._mpc_v2_reid_live.get(key)
        if runtime is None:
            runtime = MpcV2ReidRuntime()
            self._mpc_v2_reid_live[key] = runtime
        return runtime

    def adopt_mpc_v2_reid(self, key: str, data: MpcV2ReidData) -> None:
        """Adopt a validated re-identification result, bumplessly.

        The plant prior describes the room, so every cached live controller
        of this instance is stale after adoption — regardless of which
        target bucket it is keyed under. Each one is exported into the
        persisted snapshot and then dropped, so the next
        :meth:`get_mpc_v2_live` for that key rebuilds the controller with
        the new plant prior while restoring the observer state (Kalman,
        DOB, integral, last command) from the snapshot — no cold start.
        The result is stored under ``key``.

        When ``key`` is the shared, target-independent ``{uid}:reid`` key,
        this instance's obsolete per-bucket result entries are removed:
        the shared key wins every read, so they are dead weight that could
        only resurrect stale data through the legacy fallback lookup.
        """
        for live_key in list(self._mpc_v2_live):
            live = self._mpc_v2_live.pop(live_key)
            exported = export_mpc_v2_state(live)
            persistable = (
                deserialize_mpc_v2(exported, key=live_key)
                if exported is not None
                else None
            )
            if persistable is not None:
                self._state.mpc_v2[live_key] = persistable
            else:
                # No observer state to carry over: the controller had none to
                # export, or what it exported was rejected. The rebuild with
                # the new prior falls back to the last stored snapshot (or a
                # cold start). Rare, but log it so a lost transfer is
                # diagnosable.
                _LOGGER.debug(
                    "MPC v2 re-identification adopt for %s: live controller had "
                    "no usable state to carry over; rebuild will not be bumpless",
                    live_key,
                )
        self._state.mpc_v2_reid[key] = data
        if key.endswith(":reid"):
            uid_prefix = key[: -len("reid")]
            for legacy_key in [
                k
                for k in self._state.mpc_v2_reid
                if k != key and k.startswith(uid_prefix)
            ]:
                del self._state.mpc_v2_reid[legacy_key]
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

    def move_thermostat(self, old_entity_id: str, new_entity_id: str) -> int:
        """Key what was learned for ``old_entity_id`` under ``new_entity_id``.

        Every section moves, the live MPC v2 controllers included, so a
        thermostat whose entity id changed keeps its learned state. An entry
        already stored under the new id is replaced: the state that moves is
        the thermostat's own history.

        Returns the number of moved entries; marks the store dirty when
        anything moved.
        """

        def move[T](section: dict[str, T]) -> int:
            keys = [key for key in section if thermostat_of_key(key) == old_entity_id]
            for key in keys:
                section[_key_for_thermostat(key, new_entity_id)] = section.pop(key)
            return len(keys)

        moved = (
            move(self._state.mpc)
            + move(self._state.mpc_v2)
            + move(self._state.mpc_v2_reid)
            + move(self._state.pid)
            + move(self._state.tpi)
            + move(self._mpc_v2_live)
            + move(self._mpc_v2_reid_live)
        )
        if moved:
            self._dirty = True
        return moved

    def forget_thermostats_except(self, entity_ids: Collection[str]) -> int:
        """Drop the learned state of every thermostat not in ``entity_ids``.

        State learned for a thermostat the entry no longer controls would
        otherwise come back for whichever device is given that entity id
        next. Keys of the room as a whole and keys that name no thermostat
        stay.

        Returns the number of dropped entries; marks the store dirty when
        anything was dropped.
        """
        keep = {*entity_ids, GROUP_KEY_SEGMENT}

        def forget[T](section: dict[str, T]) -> int:
            keys = [
                key
                for key in section
                if (segment := thermostat_of_key(key)) is not None
                and segment not in keep
            ]
            for key in keys:
                del section[key]
            return len(keys)

        dropped = (
            forget(self._state.mpc)
            + forget(self._state.mpc_v2)
            + forget(self._state.mpc_v2_reid)
            + forget(self._state.pid)
            + forget(self._state.tpi)
            + forget(self._mpc_v2_live)
            + forget(self._mpc_v2_reid_live)
        )
        if dropped:
            self._dirty = True
        return dropped

    @property
    def thermal(self) -> ThermalStats:
        """Return thermal stats."""
        return self._state.thermal

    @thermal.setter
    def thermal(self, value: ThermalStats) -> None:
        """Set thermal stats and mark dirty."""
        self._state.thermal = value
        self._dirty = True

    def mark_dirty(self) -> None:
        """Manually mark state as needing persistence."""
        self._dirty = True

    # -- Thermal stats ---------------------------------------------------------

    def clamped_thermal(self) -> tuple[float | None, float | None]:
        """Return persisted thermal stats clamped to their valid bounds.

        Returns ``(heating_power, heat_loss_rate)``; an element is ``None`` when
        the persisted value is absent or not finite.
        """
        thermal = self._state.thermal
        heating_power = thermal.heating_power
        heat_loss_rate = thermal.heat_loss_rate
        return (
            clamp(heating_power, MIN_HEATING_POWER, MAX_HEATING_POWER)
            if heating_power is not None and math.isfinite(heating_power)
            else None,
            clamp(heat_loss_rate, MIN_HEAT_LOSS, MAX_HEAT_LOSS)
            if heat_loss_rate is not None and math.isfinite(heat_loss_rate)
            else None,
        )

    def record_thermal(
        self, heating_power: float | None, heat_loss_rate: float | None
    ) -> None:
        """Record the entity-held thermal stats before a save.

        Non-finite samples (NaN/inf) are dropped to ``None`` so a bad reading
        cannot be persisted and reloaded; this mirrors the finite handling in
        ``clamped_thermal()``.
        """
        self.thermal = ThermalStats(
            heating_power=finite_or_none(heating_power),
            heat_loss_rate=finite_or_none(heat_loss_rate),
        )

    @property
    def filters(self) -> FilterState:
        """Return the persisted runtime filter state."""
        return self._state.filters

    def record_filters(
        self,
        room_temperature_ema: float | None,
        temperature_slope: float | None,
        room_temperature_ema_recorded_at: float | None = None,
    ) -> None:
        """Record the entity-held filter state before a save.

        Non-finite samples (NaN/inf) are dropped to ``None`` for the same
        reason ``record_thermal`` drops them: the store's encoder writes
        them back as null, so a bad reading would be persisted and reloaded.

        Parameters
        ----------
        room_temperature_ema : float | None
            Exponential moving average of the external temperature.
        temperature_slope : float | None
            Estimated room-temperature slope.
        room_temperature_ema_recorded_at : float | None
            Wall-clock time the EMA was last updated at, in seconds since
            the epoch.
        """
        self._state.filters = FilterState(
            room_temperature_ema=finite_or_none(room_temperature_ema),
            temperature_slope=finite_or_none(temperature_slope),
            room_temperature_ema_recorded_at=finite_or_none(
                room_temperature_ema_recorded_at
            ),
        )
        self._dirty = True

    # -- Load / Save ---------------------------------------------------------

    def schedule_delay_save(
        self, pre_save: Callable[[], None] | None = None, delay_seconds: float = 15.0
    ) -> None:
        """Schedule a coalesced disk write through the Store.

        The Store flushes a pending delayed save on Home Assistant's
        final-write event, so the data survives a normal shutdown.
        While a save is pending, further calls are no-ops instead of
        resetting the timer: ``pre_save`` and the serialization run at
        write time, so the earliest deadline already covers later
        changes — and a steady trigger stream cannot starve the save.

        A closed manager schedules nothing: ``flush()`` makes its final
        write, and a delayed one landing after it would recreate a store
        that removing the entry deletes, or overwrite the one a reloaded
        entity has written meanwhile.

        Parameters
        ----------
        pre_save : callable or None
            Optional callback invoked at write time to refresh the state
            before serialization.
        delay_seconds : float
            Coalescing window in seconds before the disk write fires.
        """
        if self._delay_save_pending or self._closed:
            return
        if self._payload_awaiting_copy is not None:
            # The delayed write cannot take the copy first. Once the retry is
            # due, the copy is tried and the save scheduled behind it.
            self._held_save = (pre_save, delay_seconds)
            if not self._copy_retry_running and monotonic() >= self._copy_retry_at:
                self._start_copy_retry()
                return
            _LOGGER.debug(
                "better_thermostat [%s]: delayed save skipped, the stored state "
                "is not set aside yet",
                self._entry_id,
            )
            return
        self._held_save = None
        self._delay_save_pending = True

        def _data_to_save() -> StoredRuntimeState:
            self._delay_save_pending = False
            pre_save_failed = False
            if pre_save is not None:
                try:
                    pre_save()
                except Exception:
                    _LOGGER.exception(
                        "better_thermostat [%s]: pre-save callback failed",
                        self._entry_id,
                    )
                    pre_save_failed = True
                    self._dirty = True
            try:
                self._sync_mpc_v2_live()
            except Exception:
                _LOGGER.exception(
                    "better_thermostat [%s]: MPC v2 live-state sync failed",
                    self._entry_id,
                )
                pre_save_failed = True
                self._dirty = True
            data = _serialize(self._state)
            # Keep ``_dirty`` set when pre-save or the live-state sync
            # failed so ``save_if_dirty`` retries instead of acknowledging
            # an out-of-sync snapshot.
            if not pre_save_failed:
                self._dirty = False
            return data

        self._store.async_delay_save(_data_to_save, delay_seconds)

    @property
    def copy_pending(self) -> bool:
        """Return whether saves wait for a copy of the stored payload."""
        return self._payload_awaiting_copy is not None

    def close(self) -> None:
        """Stop scheduling saves of its own; call when the entity is removed.

        ``flush()`` and ``save()`` still try the copy and write, but no timer
        is left behind, a copy already under way schedules no save
        afterwards, and ``schedule_delay_save()`` schedules nothing, so no
        write lands in a store that removal deletes or another entity owns by
        then.
        """
        self._closed = True
        self._copy_retry_timed = False
        self._held_save = None
        self._cancel_copy_retry_timer()

    def _schedule_copy_retry(self) -> None:
        """Set when the copy is next tried, double the wait, and start the timer.

        The timer tries the copy at that deadline without waiting for a
        runtime save, and schedules a save skipped in the meantime once the
        copy exists. Home Assistant cancels it when it starts to stop, so it
        never runs beside the final write.
        """
        delay_seconds = self._copy_retry_seconds
        self._copy_retry_at = monotonic() + delay_seconds
        self._copy_retry_seconds = min(delay_seconds * 2, COPY_RETRY_MAX_S)
        self._cancel_copy_retry_timer()
        if self._copy_retry_timed:
            self._copy_retry_timer = async_call_later(
                self._hass,
                delay_seconds,
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

    def _start_copy_retry(self) -> None:
        """Try the copy in the background, then schedule the held-back save."""
        self._copy_retry_running = True
        self._copy_retry_task = self._hass.async_create_background_task(
            self._retry_copy_then_delay_save(), name=f"bt_state_copy_{self._entry_id}"
        )

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

    async def _retry_copy_then_delay_save(self) -> None:
        """Try the awaited copy again and schedule the save once it is kept.

        The save scheduled is the last one skipped while the copy was
        pending; with none skipped, a manager marked dirty saves without a
        ``pre_save``, and one that is not saves nothing. A manager closed
        while the copy was under way schedules nothing: ``flush()`` makes
        its final write.
        """
        try:
            await self._retry_awaited_copy()
        finally:
            self._copy_retry_running = False
        if self._payload_awaiting_copy is not None or not self._copy_retry_timed:
            return
        held = self._held_save
        if held is not None:
            self.schedule_delay_save(*held)
        elif self._dirty:
            self.schedule_delay_save()

    async def _retry_awaited_copy(self) -> None:
        """Try the awaited copy again, unless Home Assistant is stopping."""
        payload = self._payload_awaiting_copy
        if payload is None:
            return
        if self._hass.state is CoreState.stopping:
            _LOGGER.debug(
                "better_thermostat [%s]: copy left for the final write, the "
                "Store only queues writes while Home Assistant stops",
                self._entry_id,
            )
            return
        await self._quarantine_unreadable_state(payload)

    async def _quarantine_unreadable_state(self, raw: Mapping[str, object]) -> None:
        """Set an unreadable store aside before defaults take its place.

        That covers a store that cannot be read at all and one with entries
        a non-finite value reset. The defaults this entity falls back to
        are written over the live store on its next save, so without a copy
        the only record of what a user's installation had learned is gone.

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
        raw : Mapping[str, object]
            The store payload that could not be deserialized in full.
        """
        key = _quarantine_key(self._entry_id, QUARANTINE_COPIES - 1)
        try:
            free: str | None = None
            for copy in range(QUARANTINE_COPIES):
                copy_key = _quarantine_key(self._entry_id, copy)
                kept: Store[Mapping[str, object]] = Store(
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
            quarantine: Store[Mapping[str, object]] = Store(
                self._hass, QUARANTINE_VERSION, key, atomic_writes=True
            )
            await quarantine.async_save(raw)
            # ``async_save`` returns normally when the write fails, and while
            # Home Assistant stops it only queues the write for the final
            # write. A second Store holds no queued data, so what it loads
            # is what reached the disk.
            on_disk: Store[Mapping[str, object]] = Store(
                self._hass, QUARANTINE_VERSION, key
            )
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
        poisoned: list[str] = []
        try:
            if _stored_version(raw, 0) < 1:
                raw = _migrate_v0_to_v1(raw)
            self._state = _deserialize(raw, poisoned=poisoned)
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
        if poisoned:
            # The defaults of what was discarded replace the stored values on
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
            await self._retry_awaited_copy()
            if self._payload_awaiting_copy is not None:
                return
        self._sync_mpc_v2_live()
        data = _serialize(self._state)
        # async_save cancels a pending delayed write inside the Store.
        self._delay_save_pending = False
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
        """Save now, as the task ``flush()`` waits for, unless closed by then.

        For a save that can run beside the entity's removal, such as the
        startup migration. A copy still pending is tried inside that task,
        so a flush waits for its outcome instead of trying the copy beside
        it, and a manager closed in the meantime writes nothing: ``flush()``
        makes the final write. A copy that fails again leaves the state
        dirty, and the timed retry saves it once the copy exists.
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
        """Try a pending copy, then save unless the copy failed or close() ran."""
        if self._payload_awaiting_copy is not None:
            self._copy_retry_running = True
            try:
                await self._retry_awaited_copy()
            finally:
                self._copy_retry_running = False
            if self._payload_awaiting_copy is not None:
                return
        if self._copy_retry_timed:
            await self.save()

    async def save_if_dirty(self) -> None:
        """Persist current state only if it has been modified since last save."""
        if self._dirty:
            await self.save()

    async def flush(self) -> None:
        """Flush unsaved changes -- call from async_will_remove_from_hass.

        A copy already under way is awaited first, so the final write sees
        its outcome instead of trying the copy beside it: a copy that lands
        after ``close()`` schedules no save of its own. The wait is not
        bounded, like the Store write in ``save()`` it consists of.
        """
        copy = self._copy_retry_task
        if copy is not None and not copy.done():
            await asyncio.wait({copy})
        await self.save_if_dirty()
