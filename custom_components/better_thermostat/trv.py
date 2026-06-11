"""Per-TRV domain object.

A :class:`Trv` bundles everything Better Thermostat knows about one
thermostatic radiator valve: static configuration (integration, model,
adapter, quirks), reported device state, and the write-tracking flags
the control loop maintains. The entries of ``real_trvs`` are
instances of this class.

During the migration the class also speaks the dict protocol
(``trv["key"]`` / ``trv.get("key")``), so call sites can move to
attribute access file by file. The bridge disappears once every
consumer is converted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

_NO_DEFAULT = object()


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

    # -- Transitional dict protocol ----------------------------------------

    def _is_field(self, key: str) -> bool:
        """Return True when ``key`` names a typed field (not the scratchpad)."""
        return key != "extra" and key in self.__dataclass_fields__

    def __getitem__(self, key: str) -> Any:
        """Dict-style read of a field or ``extra`` entry."""
        if self._is_field(key):
            return getattr(self, key)
        return self.extra[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """Dict-style write to a field or the ``extra`` scratchpad."""
        if self._is_field(key):
            setattr(self, key, value)
        else:
            self.extra[key] = value

    def __contains__(self, key: object) -> bool:
        """Dict-style membership over fields and ``extra``."""
        return (isinstance(key, str) and self._is_field(key)) or key in self.extra

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style read: field value or ``extra`` entry or ``default``."""
        if self._is_field(key):
            return getattr(self, key)
        return self.extra.get(key, default)

    def pop(self, key: str, default: Any = _NO_DEFAULT) -> Any:
        """Dict-style clear: fields are reset to ``None``, extras removed."""
        if self._is_field(key):
            value = getattr(self, key)
            setattr(self, key, None)
            return value
        if default is _NO_DEFAULT:
            return self.extra.pop(key)
        return self.extra.pop(key, default)
