"""An adapter module for a TRV whose device takes every write.

A TRV record carries the adapter of its ecosystem. A test that is not about
the adapter builds its records with this one: it declares an offset and a
valve channel that each need a discovered entity, so the TRV's entity
surface decides what it can write, and a write it receives reaches no
device. A test that asserts what reaches the device patches the call it
watches or sets its own adapter.
"""

from __future__ import annotations

from custom_components.better_thermostat.adapters.base import (
    AdapterCapabilities,
    DeviceChannels,
)
from custom_components.better_thermostat.adapters.types import (
    AdapterHost,
    AdapterProbeHost,
)

CAPABILITIES = AdapterCapabilities(offset_write=True, valve_write=True)


async def get_info(_host: AdapterProbeHost, _entity_id: str, /) -> DeviceChannels:
    """Answer that the device offers no calibration channel of its own."""
    return DeviceChannels()


async def init(_host: AdapterHost, _entity_id: str, /) -> None:
    """Discover nothing."""


async def get_calibration_offset(_host: AdapterHost, _entity_id: str, /) -> float:
    """Answer that the TRV holds no offset."""
    return 0.0


async def get_calibration_offset_step(_host: AdapterHost, _entity_id: str, /) -> float:
    """Answer the step a TRV record starts with."""
    return 0.5


async def get_min_calibration_offset(_host: AdapterHost, _entity_id: str, /) -> float:
    """Answer the lowest offset a TRV record starts with."""
    return -7.0


async def get_max_calibration_offset(_host: AdapterHost, _entity_id: str, /) -> float:
    """Answer the highest offset a TRV record starts with."""
    return 7.0


async def set_temperature(
    _host: AdapterHost, _entity_id: str, _temperature: float, /
) -> None:
    """Take the target temperature."""


async def set_hvac_mode(
    _host: AdapterHost, _entity_id: str, _hvac_mode: str, /
) -> None:
    """Take the HVAC mode."""


async def set_calibration_offset(
    _host: AdapterHost, _entity_id: str, _calibration_offset: float, /
) -> bool:
    """Take the offset."""
    return True


async def set_valve(_host: AdapterHost, _entity_id: str, _valve: float, /) -> None:
    """Take the valve position."""
