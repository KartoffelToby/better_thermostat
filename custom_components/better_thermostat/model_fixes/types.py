"""Shared structural types for the model-fix quirk modules.

These Protocols describe the minimal Better Thermostat surface the quirk
helpers read, so every helper states what it needs from its host. No member
is assignable: a quirk reaches its device through Home Assistant services,
and keeps write state of its own in the TRV record's ``extra`` mapping.

The other direction is described here too. :class:`ModelQuirks` is what
every quirk module provides; each of the single-member Protocols after it
is one function or constant a module may add, and that the dispatch looks
for before it calls. A quirk module is a plain module, so each one lists
its own surface in a ``_Surface`` class and binds that class to every
Protocol it implements, which lets the type checker hold the module to the
signatures written here.

``climate.py`` binds the BetterThermostat entity to :class:`ModelFixHost`
under ``TYPE_CHECKING``, so a member declared here that the entity does not
provide is an error rather than a promise nobody checks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict, runtime_checkable

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant


class QuirkScratchpad(TypedDict, total=False):
    """Write state the quirk modules keep on a TRV record.

    Each key belongs to the one quirk module that reads and writes it.
    """

    _trvzb_valve_bump_seq: int
    """Sequence number of the TRVZB valve bump scheduled last."""

    _trvzb_valve_bump_task: asyncio.Task[None]
    """Delayed TRVZB valve write that follows a bump."""

    _zwa021_valve_mode_engaged: bool
    """Whether the ZWA021 manufacturer-specific valve mode went through."""


class ModelFixTrv(Protocol):
    """Minimal per-TRV record the model fixes read."""

    @property
    def advanced(self) -> Mapping[str, object]:
        """Per-TRV advanced options, keyed by option name."""
        ...

    @property
    def model(self) -> str | None:
        """Device model string, or None while it is undetermined."""
        ...

    @property
    def extra(self) -> QuirkScratchpad:
        """Scratch space a quirk keeps its own write state in."""
        ...

    @property
    def model_quirks(self) -> ModelQuirks | None:
        """Quirk module loaded for the TRV, or None before it is loaded."""
        ...

    @property
    def last_valve_percent(self) -> float | None:
        """Valve opening last commanded, or None before the first write."""
        ...


class QuirkTaskOwner(Protocol):
    """Where a quirk starts work that runs past the call that scheduled it."""

    def create_task(
        self, coro: Coroutine[Any, Any, None], name: str | None = None
    ) -> asyncio.Task[None] | None:
        """Start ``coro`` as a task the thermostat cancels on removal.

        Returns ``None``, and starts nothing, once the thermostat is being
        removed.
        """
        ...


class ModelFixHost(Protocol):
    """Minimal BetterThermostat surface the model-fix quirks read."""

    @property
    def room_temperature(self) -> float | None:
        """Room temperature Better Thermostat is regulating on.

        None while no reading is available, which the entity reports from
        construction until the startup sequence has resolved a temperature.
        """
        ...

    @property
    def heat_target_temperature(self) -> float | None:
        """Setpoint Better Thermostat is regulating towards.

        Optional on the entity, and the DEFAULT calibration mode is the one
        mode that does not demand a setpoint before calling into the quirks.
        """
        ...

    @property
    def in_maintenance(self) -> bool:
        """Whether a valve maintenance run is under way."""
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

    @property
    def task_manager(self) -> QuirkTaskOwner:
        """Owner of the background work the thermostat cancels on removal."""
        ...

    @property
    def is_removed(self) -> bool:
        """Whether the thermostat has been removed from Home Assistant."""
        ...


class QuirkLoaderHost(Protocol):
    """Surface the quirk loader reads from its caller."""

    @property
    def device_name(self) -> str:
        """Name of the caller, for log lines."""
        ...

    @property
    def hass(self) -> HomeAssistant:
        """Home Assistant core the caller is attached to."""
        ...


@runtime_checkable
class ModelQuirks(Protocol):
    """Functions every model quirk module provides.

    The dispatch in ``model_quirks.py`` calls these without asking first.
    Each takes the Better Thermostat host as its first argument, which the
    modules name ``self``. The parameters are positional only here, so their
    names are not part of the contract.
    """

    def fix_local_calibration(
        self, host: ModelFixHost, entity_id: str, calibration_offset: float, /
    ) -> float:
        """Return the calibration offset the TRV is to be written."""
        ...

    def fix_target_temperature_calibration(
        self, host: ModelFixHost, entity_id: str, temperature: float, /
    ) -> float:
        """Return the setpoint the TRV is to be written."""
        ...

    async def override_set_hvac_mode(
        self, host: ModelFixHost, entity_id: str, hvac_mode: str, /
    ) -> bool:
        """Write the HVAC mode the model's own way; True when it did."""
        ...

    async def override_set_temperature(
        self, host: ModelFixHost, entity_id: str, temperature: float, /
    ) -> bool:
        """Write the setpoint the model's own way; True when it did."""
        ...


@runtime_checkable
class InitialTweakQuirk(Protocol):
    """A quirk that prepares its device once the TRV is set up."""

    async def initial_tweak(self, host: ModelFixHost, entity_id: str, /) -> None:
        """Bring the device's own settings in line with Better Thermostat."""
        ...


@runtime_checkable
class LowestSetpointQuirk(Protocol):
    """A quirk that knows the published minimum means something else."""

    def lowest_setpoint(
        self, host: ModelFixHost, entity_id: str, lowest: float, /
    ) -> float:
        """Return the lowest setpoint to write, in Celsius."""
        ...


@runtime_checkable
class UnknownStateQuirk(Protocol):
    """A quirk that can tell an ``unknown`` state from a missing device."""

    def trv_state_unknown_as_available(
        self, host: ModelFixHost, entity_id: str, /
    ) -> bool:
        """Whether ``unknown`` is how the model reports operating."""
        ...


@runtime_checkable
class SetpointOffsetQuirk(Protocol):
    """A quirk that knows on which side its device applies the offset."""

    def local_calibration_shifts_setpoint(
        self, host: ModelFixHost, entity_id: str, /
    ) -> bool:
        """Whether the device adds the offset to its setpoint."""
        ...


@runtime_checkable
class ValveQuirk(Protocol):
    """A quirk that drives its device's valve itself."""

    async def override_set_valve(
        self, host: ModelFixHost, entity_id: str, percent: int, /
    ) -> bool:
        """Write the valve opening; True when the quirk took it."""
        ...


@runtime_checkable
class ValveChannelQuirk(Protocol):
    """A valve quirk that can tell whether its device offers a valve to write."""

    def has_valve_channel(self, host: ModelFixHost, entity_id: str, /) -> bool:
        """Whether ``override_set_valve`` has a device entity to write to."""
        ...


@runtime_checkable
class ExternalTemperatureQuirk(Protocol):
    """A quirk that mirrors the room temperature into its device."""

    async def maybe_set_external_temperature(
        self, host: ModelFixHost, entity_id: str, temperature: float, /
    ) -> bool:
        """Write the room temperature to the device; True when it did."""
        ...


# Hours between two valve maintenance runs for a TRV whose quirks set none.
DEFAULT_VALVE_MAINTENANCE_INTERVAL_HOURS: Final = 7 * 24


@runtime_checkable
class MaintenanceIntervalQuirk(Protocol):
    """A quirk that sets how often its valve is exercised."""

    VALVE_MAINTENANCE_INTERVAL_HOURS: Final[int]
    """Hours between two valve maintenance runs."""


__all__ = [
    "DEFAULT_VALVE_MAINTENANCE_INTERVAL_HOURS",
    "ExternalTemperatureQuirk",
    "InitialTweakQuirk",
    "LowestSetpointQuirk",
    "MaintenanceIntervalQuirk",
    "ModelFixHost",
    "ModelFixTrv",
    "ModelQuirks",
    "QuirkLoaderHost",
    "QuirkScratchpad",
    "SetpointOffsetQuirk",
    "UnknownStateQuirk",
    "ValveChannelQuirk",
    "ValveQuirk",
]
