"""Structural host types shared by the ecosystem adapters.

These Protocols describe the minimal surface an adapter reads off the
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
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant

    from custom_components.better_thermostat.trv import Trv


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


__all__ = ["AdapterHost", "AdapterProbeHost"]
