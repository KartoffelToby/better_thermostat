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

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
import logging
import math
from time import monotonic
from typing import Any, get_args, get_type_hints

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
from .calibration.mpc_v2.reid import ReidBuffer
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


@dataclass
class MpcV2ReidData:
    """Persisted result of an accepted offline re-identification.

    Carries the fitted plant-prior components plus the validation metrics
    of the accepting fit; the sample buffer that produced it is in-memory
    only and never persisted.
    """

    tau_room_min: float = 0.0
    gain_heater: float = 0.0
    fitted_ts: float = 0.0
    # `persist_state` writes this dataclass through `dataclasses.asdict` into
    # Home Assistant's `Store`, so these two field names are the names on disk.
    # Lowercasing them needs a store version bump and a migration step, and the
    # two move with it.
    rmse_prior_K: float = 0.0  # noqa: N815
    rmse_fit_K: float = 0.0  # noqa: N815
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
    external_temp_ema : float | None
        Exponential moving average of the external temperature.
    temp_slope : float | None
        Estimated room-temperature slope.
    """

    external_temp_ema: float | None = None
    temp_slope: float | None = None


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

# Integer fields that tally occurrences, so a stored value is only usable
# when it is a non-negative integer the store can write back.
_COUNT_FIELDS = frozenset(
    {
        "dead_zone_hits",
        "loss_learn_count",
        "gain_learn_count",
        "profile_samples",
        "consecutive_insufficient_heat",
    }
)

# Integer fields that record which way a quantity last moved, so a negative
# value is meaningful and only the storable range applies.
_SIGN_FIELDS = frozenset({"last_delta_sign", "last_error_sign"})

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


def _nullable_fields(cls: Any) -> frozenset[str]:
    """Return the names of *cls*'s fields whose declared type admits ``None``.

    Read off the declarations rather than listed by hand, so the set
    still matches once a field's type changes.
    """
    hints = get_type_hints(cls)
    return frozenset(
        name for name in cls.__dataclass_fields__ if type(None) in get_args(hints[name])
    )


_MPC_NULLABLE_FIELDS = _nullable_fields(MpcState)
_MPC_V2_NULLABLE_FIELDS = _nullable_fields(MpcV2StateData)
_MPC_V2_REID_NULLABLE_FIELDS = _nullable_fields(MpcV2ReidData)
_PID_NULLABLE_FIELDS = _nullable_fields(PIDState)
_TPI_NULLABLE_FIELDS = _nullable_fields(TpiState)


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


class _PoisonedStateError(ValueError):
    """A stored entry carries a mathematical anomaly (NaN/inf)."""


# Home Assistant's JSON encoder writes an integer only inside the 64-bit
# range orjson supports and raises TypeError on anything wider. The Store's
# write path turns that TypeError into a SerializationError, which the Store
# catches and only logs, so a single unstorable integer anywhere in the
# state leaves the config entry's file unwritten without failing the save.
_MIN_STORED_INT = -(2**63)
_MAX_STORED_INT = 2**64 - 1


def _stored_int(value: Any) -> int:
    """Return *value* as an integer the store can write back.

    A JSON number wider than 64 bits is parsed as a float, and ``int()``
    turns it into an arbitrary-precision integer that the encoder refuses.
    Those raise ``ValueError`` here, so a caller handles them like any
    other field ``int()`` cannot make sense of.
    """
    number = int(value)
    if not _MIN_STORED_INT <= number <= _MAX_STORED_INT:
        raise ValueError("integer outside the storable range")
    return number


def _stored_count(value: Any) -> int:
    """Return *value* as a storable, non-negative tally.

    A tally cannot be negative, so a negative value is as unusable as one
    the store could not write back or one that is not an integer at all;
    all three restore as 0.
    """
    try:
        count = _stored_int(value)
    except TypeError, ValueError, OverflowError:
        return 0
    return max(count, 0)


def _within(value: float, bounds: tuple[float, float]) -> bool:
    """Return whether *value* lies inside *bounds*, inclusive at both ends.

    A NaN answers ``False`` on both comparisons, so it reads as outside.
    """
    low, high = bounds
    return low <= value <= high


def finite_or_none(value: Any) -> float | None:
    """Return *value* as a finite float, or ``None`` when it is not one.

    A missing value, one ``float()`` refuses, and NaN or infinity all
    collapse to ``None``. Non-finite numbers carry no usable learning, and
    keeping one would only feed the same unusable value back into the next
    calculation that reads it.

    Parameters
    ----------
    value : Any
        the value to read, from a store or a caller

    Returns
    -------
    float | None
        the value as a finite float, or None when it is not one
    """
    if value is None:
        return None
    try:
        number = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    return number if math.isfinite(number) else None


def _null_or_poison(attr: str, nullable: frozenset[str]) -> None:
    """Let a stored null through, unless the declared type forbids one.

    A null where a number is declared is how a non-finite value gets back
    out of a file this module wrote: the store's encoder writes NaN and
    infinity as ``null``. Where anything else is declared it is simply a
    value that cannot be held. Neither leaves anything usable, so the
    entry gets the disposal :func:`_finite_or_poison` gives corrupt math.

    *nullable* names the fields whose declared type admits a ``None``. An
    empty set means the caller is parsing a place no ``None`` may reach at
    all, such as an element of a collection declared to hold numbers.
    """
    if attr in nullable:
        return
    raise _PoisonedStateError(f"{attr} is null, which its declared type cannot hold")


def _finite_or_poison(value: Any, attr: str) -> float:
    """Parse one stored float; a non-finite number poisons the entry.

    Wrong types merely skip the field (schema evolution), but NaN or
    infinity means the entry's math is corrupt — the rest of it cannot be
    trusted either, so the caller keeps none of the entry's stored values
    and the learning they carried starts over.
    """
    number = float(value)
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


def _finite_element(value: Any, attr: str) -> float:
    """Parse one number stored inside a collection field.

    ``recent_errors`` and the bins of ``perf_curve`` are declared to hold
    plain numbers, and the field guards rule only on the collection
    itself. A null among its numbers is the same saved NaN a null in a
    numeric field is — the store's encoder writes both that way — and
    costs the entry its stored values just as one does.
    """
    if value is None:
        _null_or_poison(attr, frozenset())
    return _finite_or_poison(value, attr)


def _finite_perf_curve(value: Mapping[Any, Any]) -> dict[str, dict[str, float]]:
    """Copy a stored performance curve, parsing every statistic in it.

    What the offending value is decides what it costs. A bin that is not a
    mapping of statistics, or a statistic ``float()`` refuses outright such
    as ``"later"``, raises one of the errors the caller skips a field on:
    the curve is lost and the rest of the entry survives. A statistic that
    is null or parses as a non-finite number raises :class:`_PoisonedStateError`
    instead — the bins are declared to hold plain numbers, so either one is
    corrupt math, and the entry keeps none of its stored values.
    """
    curve: dict[str, dict[str, float]] = {}
    for label, stats in value.items():
        if not isinstance(stats, Mapping):
            raise TypeError("perf_curve bin is not a mapping of statistics")
        curve[label] = {
            name: _finite_element(stat, "perf_curve statistic")
            for name, stat in stats.items()
        }
    return curve


def deserialize_mpc(
    raw: dict[str, Any], *, key: str | None = None, poisoned: list[str] | None = None
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

    Parameters
    ----------
    raw : dict[str, Any]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    poisoned : list[str] | None
        collects the entry's section and key when a non-finite value
        discards its stored values
    """
    state = MpcState()
    for attr in MpcState.__dataclass_fields__:
        if attr not in raw:
            continue
        value = raw[attr]
        try:
            if value is None:
                _null_or_poison(attr, _MPC_NULLABLE_FIELDS)
                setattr(state, attr, None)
            elif attr == "perf_curve" and isinstance(value, Mapping):
                setattr(state, attr, _finite_perf_curve(value))
            elif attr == "recent_errors" and isinstance(value, (list, tuple)):
                # MpcState.recent_errors is a deque(maxlen=20).
                setattr(
                    state,
                    attr,
                    deque(
                        (
                            _finite_element(item, "recent_errors element")
                            for item in value
                        ),
                        maxlen=20,
                    ),
                )
            elif attr in _COUNT_FIELDS:
                setattr(state, attr, _stored_count(value))
            elif attr in _SIGN_FIELDS:
                setattr(state, attr, _stored_int(value))
            elif attr in _BOOL_FIELDS:
                setattr(state, attr, bool(value))
            elif attr in _STR_FIELDS:
                setattr(state, attr, str(value))
            else:
                setattr(state, attr, _finite_or_poison(value, attr))
        except _PoisonedStateError as error:
            _discard_poisoned_entry(error, "mpc", key, poisoned)
            return MpcState()
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(attr, "mpc", key)
            continue
    return state


def deserialize_mpc_v2(
    raw: dict[str, Any], *, key: str | None = None, poisoned: list[str] | None = None
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
    ``snapshot`` are read past that loop and reach no guard. A ``snapshot``
    that is null or not a mapping therefore keeps the entry and leaves the
    empty default in its place — the very shape described above. Only a
    store this integration did not write can hold one: what it saves is
    always a mapping.

    Parameters
    ----------
    raw : dict[str, Any]
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
    state = MpcV2StateData()
    for attr in ("last_percent", "last_compute_ts", "created_ts"):
        if attr not in raw:
            continue
        value = raw[attr]
        try:
            if value is None:
                _null_or_poison(attr, _MPC_V2_NULLABLE_FIELDS)
                setattr(state, attr, None)
            else:
                setattr(state, attr, _finite_or_poison(value, attr))
        except _PoisonedStateError as error:
            _discard_poisoned_entry(error, "mpc_v2", key, poisoned)
            return None
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(attr, "mpc_v2", key)
            continue
    state.outdoor_fallback_logged = bool(raw.get("outdoor_fallback_logged", False))
    snapshot = raw.get("snapshot")
    if isinstance(snapshot, Mapping):
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
    raw: dict[str, Any], *, key: str | None = None, poisoned: list[str] | None = None
) -> MpcV2ReidData | None:
    """Deserialize a persisted re-identification result; None if malformed.

    Returning ``None`` leaves the entry out of the restored state, so the
    plant-prior lookup moves on: to another stored result sharing this
    entity's key prefix, and failing that to the heat-loss-derived prior.

    Three checks reject an entry. A NaN or infinity in one of the five
    float fields; a stored ``null`` in any of the six, since none of them
    is declared to hold one; and a ``tau_room_min`` or ``gain_heater``
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
    raw : dict[str, Any]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    poisoned : list[str] | None
        collects the entry's section and key when a non-finite value or an
        out-of-band fit discards its stored values
    """
    state = MpcV2ReidData()
    for attr in MpcV2ReidData.__dataclass_fields__:
        if attr not in raw:
            continue
        value = raw[attr]
        try:
            if value is None:
                _null_or_poison(attr, _MPC_V2_REID_NULLABLE_FIELDS)
                setattr(state, attr, None)
            elif attr == "n_segments":
                setattr(state, attr, _stored_count(value))
            else:
                setattr(state, attr, _finite_or_poison(value, attr))
        except _PoisonedStateError as error:
            _discard_poisoned_entry(error, "mpc_v2_reid", key, poisoned)
            return None
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(attr, "mpc_v2_reid", key)
            continue
    # A result whose fitted components lie outside the plausible band cannot
    # seed a plant prior. The band is two-sided on both: too small a
    # ``tau_room_min`` and the room dynamics blow up, too large and they
    # freeze, and either rail pins the commanded valve.
    for attr, bounds in (
        ("tau_room_min", TAU_ROOM_BOUNDS_MIN),
        ("gain_heater", GAIN_HEATER_BOUNDS),
    ):
        value = getattr(state, attr)
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
    raw: dict[str, Any], *, key: str | None = None, poisoned: list[str] | None = None
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
    raw : dict[str, Any]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    poisoned : list[str] | None
        collects the entry's section and key when a non-finite value
        discards its stored values
    """
    state = PIDState()
    for attr in PIDState.__dataclass_fields__:
        if attr not in raw:
            continue
        value = raw[attr]
        try:
            if value is None:
                _null_or_poison(attr, _PID_NULLABLE_FIELDS)
                setattr(state, attr, None)
            elif attr in _COUNT_FIELDS:
                setattr(state, attr, _stored_count(value))
            elif attr in _SIGN_FIELDS:
                setattr(state, attr, _stored_int(value))
            elif attr in _BOOL_FIELDS:
                setattr(state, attr, bool(value))
            else:
                setattr(state, attr, _finite_or_poison(value, attr))
        except _PoisonedStateError as error:
            _discard_poisoned_entry(error, "pid", key, poisoned)
            return PIDState()
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(attr, "pid", key)
            continue
    return state


def deserialize_tpi(
    raw: dict[str, Any], *, key: str | None = None, poisoned: list[str] | None = None
) -> TpiState:
    """Deserialize a single TPI state dict into a TpiState dataclass.

    A non-finite numeric field rejects the whole entry: learning
    restarts from defaults rather than continuing on corrupt math. A
    stored ``null`` counts as one wherever the field's type has no
    ``None``, because that is the shape a saved NaN comes back in.

    Parameters
    ----------
    raw : dict[str, Any]
        the stored entry to read
    key : str | None
        names the state entry, so a report about a value that cannot be read
        can point at the room rather than at nothing
    poisoned : list[str] | None
        collects the entry's section and key when a non-finite value
        discards its stored values
    """
    state = TpiState()
    for attr in TpiState.__dataclass_fields__:
        if attr not in raw:
            continue
        value = raw[attr]
        try:
            if value is None:
                _null_or_poison(attr, _TPI_NULLABLE_FIELDS)
                setattr(state, attr, None)
            else:
                setattr(state, attr, _finite_or_poison(value, attr))
        except _PoisonedStateError as error:
            _discard_poisoned_entry(error, "tpi", key, poisoned)
            return TpiState()
        except TypeError, ValueError, OverflowError:
            _report_unreadable_field(attr, "tpi", key)
            continue
    return state


def _stored_section(
    raw: dict[str, Any], section: str, poisoned: list[str] | None
) -> Mapping[str, Any]:
    """Return one section of the store, or an empty one when it has none.

    A section of any other shape than a mapping is dropped and named: past
    the load path its entities start from defaults, like on a first start.
    It is noted in *poisoned* as well, since those defaults replace it on
    the next save.
    """
    value = raw.get(section, {})
    if isinstance(value, Mapping):
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
    raw: dict[str, Any], section: str, poisoned: list[str] | None
) -> list[tuple[str, dict[str, Any]]]:
    """Return the entries of one keyed section that are mappings.

    An entry of any other shape is dropped, named with its key and noted
    in *poisoned*.
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    for key, entry in _stored_section(raw, section, poisoned).items():
        if isinstance(entry, dict):
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
    values: Mapping[str, Any], section: str, attr: str
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
    raw: dict[str, Any], *, poisoned: list[str] | None = None
) -> RuntimeState:
    """Reconstruct a RuntimeState from a raw dict (loaded from Store).

    *poisoned* collects the section, or the section and key, of every part
    of the store whose stored values are discarded as a whole: an entry a
    non-finite number reset, a re-identification result outside its
    plausible band, and a section or entry of the wrong shape.
    """
    state = RuntimeState(version=raw.get("version", CURRENT_VERSION))

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
        external_temp_ema=_stored_optional_number(
            filters_raw, "filters", "external_temp_ema"
        ),
        temp_slope=_stored_optional_number(filters_raw, "filters", "temp_slope"),
    )

    # A legacy "presets" section is ignored: preset temperatures are UI
    # state owned by the preset number entities.

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
    raw.setdefault("filters", {})
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
        # Re-identification sample buffers and scheduling flags, in-memory only.
        self._mpc_v2_reid_live: dict[str, MpcV2ReidRuntime] = {}
        self._dirty = False
        self._delay_save_pending = False
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
        # The timer that tries the copy at ``_copy_retry_at`` on its own, and
        # whether the manager still starts one; after close() it does not.
        self._copy_retry_timer: CALLBACK_TYPE | None = None
        self._copy_retry_timed = True
        # The last runtime save skipped while the copy is pending, as
        # ``(pre_save, delay_s)``; the timer schedules it once the copy exists.
        self._held_save: tuple[Callable[[], None] | None, float] | None = None

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
                import_mpc_v2_state(asdict(persisted), params, key=key)
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
        the persisted value is absent or cannot be parsed as a float.
        """
        thermal = self._state.thermal

        heating_power: float | None = None
        if thermal.heating_power is not None:
            try:
                number = float(thermal.heating_power)
                if math.isfinite(number):
                    heating_power = clamp(number, MIN_HEATING_POWER, MAX_HEATING_POWER)
            except TypeError, ValueError, OverflowError:
                heating_power = None

        heat_loss_rate: float | None = None
        if thermal.heat_loss_rate is not None:
            try:
                number = float(thermal.heat_loss_rate)
                if math.isfinite(number):
                    heat_loss_rate = clamp(number, MIN_HEAT_LOSS, MAX_HEAT_LOSS)
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
        self.thermal = ThermalStats(
            heating_power=finite_or_none(heating_power),
            heat_loss_rate=finite_or_none(heat_loss_rate),
        )

    @property
    def filters(self) -> FilterState:
        """Return the persisted runtime filter state."""
        return self._state.filters

    def record_filters(
        self, external_temp_ema: float | None, temp_slope: float | None
    ) -> None:
        """Record the entity-held filter state before a save.

        Non-finite samples (NaN/inf) are dropped to ``None`` for the same
        reason ``record_thermal`` drops them: the store's encoder writes
        them back as null, so a bad reading would be persisted and reloaded.

        Parameters
        ----------
        external_temp_ema : float | None
            Exponential moving average of the external temperature.
        temp_slope : float | None
            Estimated room-temperature slope.
        """
        self._state.filters = FilterState(
            external_temp_ema=finite_or_none(external_temp_ema),
            temp_slope=finite_or_none(temp_slope),
        )
        self._dirty = True

    # -- Load / Save ---------------------------------------------------------

    def schedule_delay_save(self, pre_save=None, delay_s: float = 15.0) -> None:
        """Schedule a coalesced disk write through the Store.

        The Store flushes a pending delayed save on Home Assistant's
        final-write event, so the data survives a normal shutdown.
        While a save is pending, further calls are no-ops instead of
        resetting the timer: ``pre_save`` and the serialization run at
        write time, so the earliest deadline already covers later
        changes — and a steady trigger stream cannot starve the save.

        Parameters
        ----------
        pre_save : callable or None
            Optional callback invoked at write time to refresh the state
            before serialization.
        delay_s : float
            Coalescing window in seconds before the disk write fires.
        """
        if self._delay_save_pending:
            return
        if self._payload_awaiting_copy is not None:
            # The delayed write cannot take the copy first. Once the retry is
            # due, the copy is tried and the save scheduled behind it.
            self._held_save = (pre_save, delay_s)
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

        def _data_to_save() -> dict[str, Any]:
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

        self._store.async_delay_save(_data_to_save, delay_s)

    @property
    def copy_pending(self) -> bool:
        """Return whether saves wait for a copy of the stored payload."""
        return self._payload_awaiting_copy is not None

    def close(self) -> None:
        """Stop trying the copy on a timer; call when the entity is removed.

        ``flush()`` and ``save()`` still try the copy, but no timer is left
        behind to write into a store another entity may own by then.
        """
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

    def _start_copy_retry(self) -> None:
        """Try the copy in the background, then schedule the held-back save."""
        self._copy_retry_running = True
        self._hass.async_create_background_task(
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
        ``pre_save``, and one that is not saves nothing.
        """
        try:
            await self._retry_awaited_copy()
        finally:
            self._copy_retry_running = False
        if self._payload_awaiting_copy is not None:
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

    async def _quarantine_unreadable_state(self, raw: dict[str, Any]) -> None:
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
        poisoned: list[str] = []
        try:
            version = raw.get("version", 0)
            if version < 1:
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

    async def save_if_dirty(self) -> None:
        """Persist current state only if it has been modified since last save."""
        if self._dirty:
            await self.save()

    async def flush(self) -> None:
        """Flush unsaved changes -- call from async_will_remove_from_hass."""
        await self.save_if_dirty()
