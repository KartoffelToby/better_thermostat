"""Shared structural types for the model-fix quirk modules.

These Protocols describe the minimal BetterThermostat and Home Assistant
surface the quirk helpers read, so the helpers can be typed without importing
Home Assistant.
"""

from __future__ import annotations

from collections.abc import Coroutine, Mapping
from typing import Any, Protocol


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


class _ServicesLike(Protocol):
    """Minimal ``hass.services`` surface the model fixes call into."""

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: Mapping[str, Any] | None = ...,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call a Home Assistant service."""
        ...


class _HassLike(Protocol):
    """Minimal Home Assistant core surface read by the model fixes."""

    @property
    def states(self) -> _StatesLike:
        """State registry exposing per-entity state lookups."""
        ...

    @property
    def services(self) -> _ServicesLike:
        """Service registry the quirks write through."""
        ...

    def async_create_background_task(
        self, target: Coroutine[Any, Any, Any], name: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Schedule a coroutine that outlives the calling handler."""
        ...


class _TrvLike(Protocol):
    """Minimal per-TRV record the model fixes read."""

    advanced: Mapping[str, Any] | None
    model: str | None


class ModelFixHost(Protocol):
    """Minimal BetterThermostat surface the model-fix quirks read."""

    cur_temp: float
    bt_target_temp: float
    device_name: str
    # Passed straight back into service calls so a command the quirk issues
    # is attributed to the same origin as the rest of the cycle.
    context: Any
    real_trvs: Mapping[str, _TrvLike]

    @property
    def hass(self) -> _HassLike:
        """Home Assistant core the BetterThermostat instance is attached to."""
        ...


__all__ = ["ModelFixHost"]
