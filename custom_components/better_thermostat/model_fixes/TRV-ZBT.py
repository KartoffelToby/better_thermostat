"""Quirks for the Sonoff TRV-ZBT (TRV Gen2) Zigbee TRV.

The TRV-ZBT regulates on an external temperature once its sensor selector
names one, and reads that temperature from an input nobody else keeps
current. That input and the selector carry the same names as on the TRVZB,
so the TRVZB write is reused to mirror the room temperature into it.

Everything else stays as the default quirks have it. In particular this
module offers no valve channel: heating_valve_position and
idle_valve_position configure the device's own controller and do not
position the valve, so no Better Thermostat position is written to either.
"""

from __future__ import annotations

from custom_components.better_thermostat.model_fixes.default import (
    fix_local_calibration,
    fix_target_temperature_calibration,
    initial_tweak,
    override_set_hvac_mode,
    override_set_temperature,
)
from custom_components.better_thermostat.model_fixes.TRVZB import (
    maybe_set_external_temperature,
)
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    ModelQuirks,
)

__all__ = [
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "initial_tweak",
    "maybe_set_external_temperature",
    "override_set_hvac_mode",
    "override_set_temperature",
]


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    initial_tweak = staticmethod(initial_tweak)
    maybe_set_external_temperature = staticmethod(maybe_set_external_temperature)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_EXTERNAL_TEMPERATURE_QUIRK: ExternalTemperatureQuirk = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
