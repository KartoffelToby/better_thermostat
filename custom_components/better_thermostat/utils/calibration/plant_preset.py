"""The plant-prior presets and the flow temperature of MPC v2.

They sit outside the ``mpc_v2`` package so the configuration constants and
the stored state can name them without importing the controller and, through
it, numpy. Like ``mpc_v2`` this module imports nothing from Home Assistant.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# Flow temperature the plant prior assumes, in °C: a boiler running a fixed
# heating curve.
DEFAULT_FLOW_TEMPERATURE: Final = 65.0


class MpcV2PlantPreset(StrEnum):
    """Plant-prior presets for MPC v2.

    ``AUTO`` lets ``make_plant_prior`` derive ``tau_room_min`` from BT's
    learned ``heat_loss_rate``; the other three presets are static
    overrides keyed roughly to room size / envelope speed.
    """

    AUTO = "auto"
    SMALL_ROOM = "small_room"
    MEDIUM_ROOM = "medium_room"
    LARGE_ROOM = "large_room"
