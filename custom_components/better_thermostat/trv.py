"""Per-TRV domain object.

A :class:`Trv` bundles everything Better Thermostat knows about one
thermostatic radiator valve: static configuration (integration, model,
adapter, quirks), reported device state, and the write-tracking flags
the control loop maintains. The entries of ``real_trvs`` are
instances of this class, accessed via typed attributes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any


@dataclass
class Trv:
    """State, adapter, and quirks of a single TRV."""

    entity_id: str

    # -- Static configuration --------------------------------------------
    integration: str | None = None
    model: str | None = None
    calibration: Any = None
    adapter: ModuleType | None = None
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

    # -- Calibration results -----------------------------------------------
    calibration_balance: dict[str, Any] | None = None
    balance: dict[str, Any] | None = None

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

    @classmethod
    def from_legacy_dict(cls, entity_id: str, data: dict[str, Any]) -> Trv:
        """Build a Trv from a plain per-entity dict.

        Known keys become typed fields; unknown keys land in ``extra``.
        A legacy ``extra`` dict is merged into ``extra`` rather than
        nested, and a legacy ``entity_id`` key is ignored in favor of
        the ``entity_id`` argument.

        Parameters
        ----------
        entity_id : str
            Entity id of the TRV this state belongs to.
        data : dict[str, Any]
            Legacy per-entity dict as previously stored in ``real_trvs``.

        Returns
        -------
        Trv
            Typed equivalent of ``data``.
        """
        fields_in: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in data.items():
            if key == "entity_id":
                continue
            if key == "extra":
                if isinstance(value, dict):
                    extra.update(value)
                else:
                    extra[key] = value
            elif key in cls.__dataclass_fields__:
                fields_in[key] = value
            else:
                extra[key] = value
        trv = cls(entity_id=entity_id, **fields_in)
        trv.extra.update(extra)
        return trv
