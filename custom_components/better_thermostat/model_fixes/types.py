"""Shared structural types for the model-fix quirk modules.

These Protocols describe the minimal Better Thermostat surface the quirk
helpers read, so every helper states what it needs from its host. No member
is assignable: a quirk reaches its device through Home Assistant services,
and keeps write state of its own in the TRV record's ``extra`` mapping.

``climate.py`` binds the BetterThermostat entity to :class:`ModelFixHost`
under ``TYPE_CHECKING``, so a member declared here that the entity does not
provide is an error rather than a promise nobody checks.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant


class ModelFixTrv(Protocol):
    """Minimal per-TRV record the model fixes read."""

    @property
    def advanced(self) -> Mapping[str, Any]:
        """Per-TRV advanced options, keyed by option name."""
        ...

    @property
    def model(self) -> str | None:
        """Device model string, or None while it is undetermined."""
        ...

    @property
    def extra(self) -> MutableMapping[str, Any]:
        """Scratch space a quirk keeps its own write state in."""
        ...

    @property
    def last_valve_percent(self) -> float | None:
        """Valve opening last commanded, or None before the first write."""
        ...


class ModelFixHost(Protocol):
    """Minimal BetterThermostat surface the model-fix quirks read."""

    @property
    def cur_temp(self) -> float | None:
        """Room temperature Better Thermostat is regulating on.

        None while no reading is available, which the entity reports from
        construction until the startup sequence has resolved a temperature.
        """
        ...

    @property
    def bt_target_temp(self) -> float | None:
        """Setpoint Better Thermostat is regulating towards.

        Optional on the entity, and the DEFAULT calibration mode is the one
        mode that does not demand a setpoint before calling into the quirks.
        """
        ...

    @property
    def device_name(self) -> str:
        """Name of the Better Thermostat instance, for log lines."""
        ...

    @property
    def context(self) -> Context | None:
        """Origin passed straight back into service calls.

        A command a quirk issues is attributed to the same origin as the
        rest of the cycle.
        """
        ...

    @property
    def hass(self) -> HomeAssistant:
        """Home Assistant core the BetterThermostat instance is attached to."""
        ...

    @property
    def real_trvs(self) -> Mapping[str, ModelFixTrv]:
        """Per-TRV records, keyed by climate entity id."""
        ...


__all__ = ["ModelFixHost", "ModelFixTrv"]
