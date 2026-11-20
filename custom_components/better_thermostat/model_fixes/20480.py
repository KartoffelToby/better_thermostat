"""Quirks for the IKEA LILJEBAGGE (E2497) radiator thermostat under Matter.

Home Assistant's Matter integration registers a device with its Matter
product id as the model id, and the LILJEBAGGE reports 20480. A product id is
unique only among one vendor's devices, so another vendor's Matter device
with the same product id is driven by this module as well.

The LILJEBAGGE publishes a local temperature calibration entity but, on
firmware 1.1.6, applies no offset written to it: neither the temperature it
reports nor its valve follows the offset. An offset calibration computes
every correction on top of the last one and so runs the offset to the end
of its range, so the quirk declares the offset ignored. Better Thermostat
then neither offers the offset calibration for this device nor writes an
offset to it; the target temperature calibration works as on any device.

Everything else stays as the default quirks have it.
"""

from __future__ import annotations

from custom_components.better_thermostat.model_fixes.default import (
    fix_local_calibration,
    fix_target_temperature_calibration,
    initial_tweak,
    override_set_hvac_mode,
    override_set_temperature,
)
from custom_components.better_thermostat.model_fixes.types import (
    IgnoredOffsetQuirk,
    InitialTweakQuirk,
    ModelQuirks,
    RegistryHost,
)

__all__ = [
    "calibration_offset_ignored",
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "initial_tweak",
    "override_set_hvac_mode",
    "override_set_temperature",
]


def calibration_offset_ignored(self: RegistryHost, entity_id: str) -> bool:
    """Report that the LILJEBAGGE ignores its offset.

    The product id this module is keyed to is a Matter one, so every device
    it loads for is on Matter and the answer needs no registry lookup.

    Parameters
    ----------
    self : RegistryHost
        Caller providing Home Assistant access.
    entity_id : str
        Entity id of the TRV.

    Returns
    -------
    bool
        Always True.
    """
    return True


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    initial_tweak = staticmethod(initial_tweak)
    calibration_offset_ignored = staticmethod(calibration_offset_ignored)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_IGNORED_OFFSET_QUIRK: IgnoredOffsetQuirk = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
