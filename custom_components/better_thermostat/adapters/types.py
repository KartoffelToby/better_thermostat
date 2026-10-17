"""Structural types shared by the ecosystem adapters.

The host Protocols describe the minimal surface an adapter reads off the
object it is handed, so an adapter can be typed without depending on the
concrete Better Thermostat entity class. The members that carry a value
onwards name that value's own type: an adapter hands ``hass`` to helpers
declared against ``HomeAssistant`` and reads fields off the per-TRV record
declared in :mod:`custom_components.better_thermostat.trv`, so restating
either structurally would only let the restatement drift from the type the
value actually has.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant

    from custom_components.better_thermostat.trv import Trv

    from .base import AdapterCapabilities, DeviceChannels


class AdapterProbeHost(Protocol):
    """Host surface an adapter reads while probing a device.

    ``get_info`` answers from what Home Assistant already knows about the
    device, so it runs on a config flow handler as well as on the climate
    entity. Neither the per-TRV records nor a call origin exist at that
    point.
    """

    @property
    def hass(self) -> HomeAssistant:
        """Home Assistant core the host is attached to."""
        ...

    @property
    def device_name(self) -> str:
        """Name the host logs under."""
        ...


class AdapterHost(AdapterProbeHost, Protocol):
    """Host surface an adapter reads while driving a configured TRV.

    Everything past discovery runs on the climate entity, which adds the
    per-TRV records and the origin its service calls carry.
    """

    @property
    def context(self) -> Context | None:
        """Origin every service call the adapter issues is attributed to."""
        ...

    @property
    def real_trvs(self) -> Mapping[str, Trv]:
        """Per-TRV records, keyed by entity id."""
        ...


@runtime_checkable
class TrvAdapter(Protocol):
    """The surface every ecosystem adapter module provides.

    An adapter is a module, not a class: its module-level functions take
    the host as their first argument, which a protocol method spells as
    the first parameter after the protocol's own ``self``. The parameters
    are positional-only because an adapter names its host parameter
    ``self`` while the delegate passes every argument by position.

    The runtime check of a ``runtime_checkable`` Protocol asks only
    whether each member is present; the signatures are held by the type
    checker through :mod:`.conformance`.

    Attributes
    ----------
    CAPABILITIES : AdapterCapabilities
        What the ecosystem can write, before the discovered entity surface
        of one TRV narrows it.
    """

    CAPABILITIES: AdapterCapabilities

    async def get_info(
        self, host: AdapterProbeHost, entity_id: str, /
    ) -> DeviceChannels:
        """Answer which calibration channels the device offers."""
        ...

    async def init(self, host: AdapterHost, entity_id: str, /) -> None:
        """Discover the helper entities the TRV is driven through."""
        ...

    async def get_calibration_offset(
        self, host: AdapterHost, entity_id: str, /
    ) -> float:
        """Answer the calibration offset the TRV holds."""
        ...

    async def get_calibration_offset_step(
        self, host: AdapterHost, entity_id: str, /
    ) -> float:
        """Answer the step the calibration offset moves in."""
        ...

    async def get_min_calibration_offset(
        self, host: AdapterHost, entity_id: str, /
    ) -> float:
        """Answer the lowest calibration offset the TRV takes."""
        ...

    async def get_max_calibration_offset(
        self, host: AdapterHost, entity_id: str, /
    ) -> float:
        """Answer the highest calibration offset the TRV takes."""
        ...

    async def set_temperature(
        self, host: AdapterHost, entity_id: str, temperature: float, /
    ) -> None:
        """Write a target temperature, in degrees Celsius."""
        ...

    async def set_hvac_mode(
        self, host: AdapterHost, entity_id: str, hvac_mode: str, /
    ) -> None:
        """Write an HVAC mode."""
        ...

    async def set_calibration_offset(
        self, host: AdapterHost, entity_id: str, calibration_offset: float, /
    ) -> bool:
        """Write a calibration offset, answering whether it went out."""
        ...

    async def set_valve(
        self, host: AdapterHost, entity_id: str, valve: float, /
    ) -> None:
        """Write a valve position, in percent."""
        ...


__all__ = ["AdapterHost", "AdapterProbeHost", "TrvAdapter"]
