"""Quirks for the Bosch BTH-RA (Radiator Thermostat II) under Zigbee2MQTT.

Zigbee2MQTT exposes the head's valve as the writable ``pi_heating_demand``
number. The head holds a demand written there for fifteen minutes and then
returns to its own controller, which sets a demand of its own: a valve held
at one opening is shut by the head and reopened by the next control cycle
within seconds. Writing the commanded opening again before that silence ends
keeps the head on Better Thermostat's demand.

The keepalive is due eight minutes after the last valve write the head took.
The reconcile tick reads it every five minutes, so the write goes out
thirteen minutes after the one before at the latest, inside the head's
window, unless that tick falls into a control cycle that is still running
and the next one has to take it. A closed
valve is not written again: Better Thermostat holds the valve shut for hours
while the room is off or a window is open, and a write every few minutes
over those hours costs the head's battery for an opening that does not
change.

Everything else stays as the default quirks have it.
"""

from __future__ import annotations

from typing import Final

from custom_components.better_thermostat.model_fixes.default import (
    VALVE_MAINTENANCE_INTERVAL_HOURS,
    fix_local_calibration,
    fix_target_temperature_calibration,
    initial_tweak,
    override_set_hvac_mode,
    override_set_temperature,
    trv_state_unknown_as_available,
)
from custom_components.better_thermostat.model_fixes.types import (
    InitialTweakQuirk,
    MaintenanceIntervalQuirk,
    ModelQuirks,
    UnknownStateQuirk,
    ValveKeepaliveQuirk,
)

VALVE_KEEPALIVE_INTERVAL_SECONDS: Final = 480.0

__all__ = [
    "VALVE_KEEPALIVE_INTERVAL_SECONDS",
    "VALVE_MAINTENANCE_INTERVAL_HOURS",
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "initial_tweak",
    "override_set_hvac_mode",
    "override_set_temperature",
    "trv_state_unknown_as_available",
]


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    VALVE_MAINTENANCE_INTERVAL_HOURS = VALVE_MAINTENANCE_INTERVAL_HOURS
    initial_tweak = staticmethod(initial_tweak)
    trv_state_unknown_as_available = staticmethod(trv_state_unknown_as_available)
    VALVE_KEEPALIVE_INTERVAL_SECONDS = VALVE_KEEPALIVE_INTERVAL_SECONDS


_MODEL_QUIRKS: ModelQuirks = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
_MAINTENANCE_INTERVAL_QUIRK: MaintenanceIntervalQuirk = _Surface()
_UNKNOWN_STATE_QUIRK: UnknownStateQuirk = _Surface()
_VALVE_KEEPALIVE_QUIRK: ValveKeepaliveQuirk = _Surface()
