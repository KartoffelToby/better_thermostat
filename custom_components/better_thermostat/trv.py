"""Per-TRV domain object.

A :class:`Trv` bundles everything Better Thermostat knows about one
thermostatic radiator valve: static configuration (integration, model,
adapter, quirks), reported device state, and the write-tracking flags
the control loop maintains. The entries of ``real_trvs`` are
instances of this class, accessed via typed attributes.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Protocol, runtime_checkable

from custom_components.better_thermostat.core.calibrator import CalibratorHealth


@runtime_checkable
class ModelQuirks(Protocol):
    """Structural surface of a model-quirk module.

    Quirk modules are plain modules under ``model_fixes/``; this is the
    contract every one of them provides. ``override_set_valve`` is the
    one optional extension — callers probe it with ``getattr``, and
    :meth:`Trv.capabilities` turns its presence into a capability.
    """

    fix_local_calibration: Callable[..., float]
    fix_target_temperature_calibration: Callable[..., float]
    override_set_hvac_mode: Callable[..., Awaitable[bool]]
    override_set_temperature: Callable[..., Awaitable[bool]]


@dataclass(frozen=True)
class TrvCapabilities:
    """What this TRV can do.

    The adapter module declares what its ecosystem supports
    (``CAPABILITIES`` in ``adapters/*``); this descriptor intersects
    that declaration with the discovered entity surface and the model
    quirks. The kernel expresses intent; whoever writes consults the
    capabilities instead of re-deriving them from scattered quirk and
    entity checks.
    """

    supports_offset_write: bool = False
    supports_valve_write: bool = False
    supports_off_mode: bool = True


@dataclass
class Trv:
    """State, adapter, and quirks of a single TRV."""

    entity_id: str

    # -- Static configuration --------------------------------------------
    integration: str | None = None
    model: str | None = None
    calibration: Any = None
    adapter: ModuleType | None = None
    # A model-quirk module satisfying the ModelQuirks surface, loaded
    # dynamically like the adapter and therefore typed as the module.
    model_quirks: ModuleType | None = None
    advanced: dict[str, Any] = field(default_factory=dict)

    # -- Reported device state -------------------------------------------
    valve_position: float | None = None
    valve_position_entity: str | None = None
    valve_position_writable: bool | None = None
    valve_max_opening: float = 100.0
    max_temp: float | None = None
    min_temp: float | None = None
    target_temp_step: float | None = None
    temperature: float | None = None
    current_temperature: float | None = None
    hvac_modes: list[str] | None = None
    hvac_mode: str | None = None
    hvac_action: str | None = None
    local_temperature_calibration_entity: str | None = None
    local_calibration_min: float = -7
    local_calibration_max: float = 7
    local_calibration_step: float = 0.5

    # -- Write tracking ----------------------------------------------------
    ignore_trv_states: bool = False
    calibration_received: bool = True
    target_temp_received: bool = True
    system_mode_received: bool = True
    # One-shot flag: the next live internal reading after an outage must
    # bypass the debounce so it is not dropped as a stale duplicate.
    accept_next_internal_temp: bool = False
    last_temperature: float | None = None
    last_valve_position: float | None = None
    last_hvac_mode: str | None = None
    last_current_temperature: float | None = None
    last_calibration: float | None = None
    last_valve_percent: float | None = None
    last_valve_method: str | None = None
    # Per-channel write-budget stamps (setpoint, offset, valve) so one
    # channel's write cannot starve another channel's slot.
    last_write_monotonic: float | None = None
    last_offset_write_monotonic: float | None = None
    last_valve_write_monotonic: float | None = None
    # Whether a follow-up control cycle is already scheduled for a
    # budget-deferred setpoint write.
    budget_retry_pending: bool = False
    # Whether a follow-up control cycle is already scheduled for this
    # TRV's next reachability-retry window.
    reachability_retry_pending: bool = False

    # -- Calibration results -----------------------------------------------
    calibration_balance: dict[str, Any] | None = None
    balance: dict[str, Any] | None = None
    # Per-TRV calibrator (BalanceCalibrator): the protocol adapter the
    # dispatch observes every cycle and actuates through when ready.
    calibrator: Any | None = None

    # -- Calibrator annunciation --------------------------------------------
    # Worst health grade the calibrator reported for this TRV, plus the
    # recent commanded percentages the oscillation detector looks at.
    calibrator_health: CalibratorHealth = CalibratorHealth.HEALTHY
    balance_percent_history: deque[float] = field(
        default_factory=lambda: deque(maxlen=10)
    )

    # -- Quirk scratchpad ----------------------------------------------------
    # Model quirks may stash private bookkeeping here (e.g. TRVZB valve
    # bump sequencing) without widening the typed surface.
    extra: dict[str, Any] = field(default_factory=dict)

    def consume_accept_next_internal_temp(self) -> bool:
        """Return and clear the one-shot accept-next-internal-temp flag.

        Returns
        -------
        bool
            ``True`` if the next internal reading should bypass the
            debounce; the flag is reset to ``False`` as a side effect.
        """
        accepted = self.accept_next_internal_temp
        self.accept_next_internal_temp = False
        return accepted

    def capabilities(self) -> TrvCapabilities:
        """Effective capabilities: adapter declaration ∩ discovered surface."""
        quirk_valve = callable(getattr(self.model_quirks, "override_set_valve", None))
        offset_entity = self.local_temperature_calibration_entity is not None
        valve_entity = bool(self.valve_position_entity and self.valve_position_writable)

        declared = getattr(self.adapter, "CAPABILITIES", None)
        if declared is None:
            # Adapter without a declaration: the discovered surface rules.
            offset = offset_entity
            valve = valve_entity
        else:
            offset = declared.offset_write and (
                offset_entity or not declared.offset_needs_entity
            )
            valve = declared.valve_write and (
                valve_entity or not declared.valve_needs_entity
            )

        no_off = (self.hvac_modes is not None and "off" not in self.hvac_modes) or (
            self.advanced or {}
        ).get("no_off_system_mode", False) is True
        return TrvCapabilities(
            supports_offset_write=offset,
            supports_valve_write=valve or quirk_valve,
            supports_off_mode=not no_off,
        )

    @classmethod
    def from_legacy_dict(cls, entity_id: str, data: dict[str, Any]) -> Trv:
        """Build a Trv from a plain per-entity dict.

        Known keys become typed fields; unknown keys land in ``extra``.
        """
        fields_in = {}
        extra: dict[str, Any] = {}
        for key, value in data.items():
            if key != "extra" and key in cls.__dataclass_fields__:
                fields_in[key] = value
            else:
                extra[key] = value
        trv = cls(entity_id=entity_id, **fields_in)
        trv.extra.update(extra)
        return trv
