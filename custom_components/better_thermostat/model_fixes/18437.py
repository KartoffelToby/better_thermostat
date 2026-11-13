"""Quirks for the Aqara Radiator Thermostat W600 under Matter.

Home Assistant's Matter integration registers a device with its Matter
product id as the model id, and the W600 reports 18437. A product id is
unique only among one vendor's devices, so another vendor's Matter device
with the same product id is driven by this module as well.

The W600 publishes the local temperature calibration range Matter allows,
-12.7 to 12.7 °C, but applies an offset only from -5.0 to 5.0 °C. An offset
beyond that is taken into the entity and leaves the reported temperature
where it was, so the quirk declares the range the device applies.

Everything else stays as the default quirks have it.
"""

from __future__ import annotations

from typing import Final

from custom_components.better_thermostat.model_fixes.default import (
    fix_local_calibration,
    fix_target_temperature_calibration,
    initial_tweak,
    override_set_hvac_mode,
    override_set_temperature,
)
from custom_components.better_thermostat.model_fixes.types import (
    InitialTweakQuirk,
    ModelQuirks,
    OffsetRangeQuirk,
)

__all__ = [
    "MAX_CALIBRATION_OFFSET",
    "MIN_CALIBRATION_OFFSET",
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "initial_tweak",
    "override_set_hvac_mode",
    "override_set_temperature",
]

MIN_CALIBRATION_OFFSET: Final = -5.0
MAX_CALIBRATION_OFFSET: Final = 5.0


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    initial_tweak = staticmethod(initial_tweak)
    MIN_CALIBRATION_OFFSET = MIN_CALIBRATION_OFFSET
    MAX_CALIBRATION_OFFSET = MAX_CALIBRATION_OFFSET


_MODEL_QUIRKS: ModelQuirks = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
_OFFSET_RANGE_QUIRK: OffsetRangeQuirk = _Surface()
