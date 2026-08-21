"""Shared structural types for the calibration helpers.

These Protocols describe the minimal BetterThermostat surface that the
Home-Assistant-free calibration code reads, so the helpers can be typed
without importing Home Assistant.
"""

from __future__ import annotations

from typing import Protocol


class CalibrationHost(Protocol):
    """Minimal BetterThermostat surface used to build calibration state keys."""

    bt_target_temp: float | None

    @property
    def unique_id(self) -> str | None:
        """Home Assistant entity unique id, if one is assigned."""
        ...
