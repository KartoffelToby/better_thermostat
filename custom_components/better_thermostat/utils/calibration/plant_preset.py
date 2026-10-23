"""The configurable parts of the MPC v2 plant prior.

The preset enum and the flow-temperature defaults sit outside the ``mpc_v2``
package so the configuration constants can name them without importing the
controller and, through it, numpy. Like ``mpc_v2`` this module imports
nothing from Home Assistant.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# Supply water temperature the plant prior assumes, in °C, when none is
# configured: a boiler running a fixed heating curve.
DEFAULT_FLOW_TEMPERATURE: Final = 65.0

# The flow temperatures the configuration accepts, in °C and inclusive at both
# ends: from a heat pump on a mild day to a boiler at its limit.
FLOW_TEMPERATURE_BOUNDS: Final = (25.0, 90.0)


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
