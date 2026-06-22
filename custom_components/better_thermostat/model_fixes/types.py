"""Shared structural types for the model-fix quirk modules.

These Protocols describe the minimal BetterThermostat and Home Assistant
surface the quirk helpers read, so the helpers can be typed without importing
Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class _StateLike(Protocol):
    """Minimal Home Assistant state surface read by the model fixes."""

    @property
    def attributes(self) -> Mapping[str, float | int | str | None]:
        """State attributes, keyed by name."""
        ...


class _StatesLike(Protocol):
    """Minimal ``hass.states`` registry surface read by the model fixes."""

    def get(self, entity_id: str) -> _StateLike | None:
        """Return the state for an entity id, or None if it is unknown."""
        ...


class _HassLike(Protocol):
    """Minimal Home Assistant core surface read by the model fixes."""

    @property
    def states(self) -> _StatesLike:
        """State registry exposing per-entity state lookups."""
        ...


class ModelFixHost(Protocol):
    """Minimal BetterThermostat surface the model-fix quirks read."""

    cur_temp: float
    bt_target_temp: float

    @property
    def hass(self) -> _HassLike:
        """Home Assistant core the BetterThermostat instance is attached to."""
        ...
