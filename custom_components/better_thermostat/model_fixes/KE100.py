"""Quirks for the Kasa KE100 radiator thermostat.

Paired to Home Assistant over Matter, through the Kasa Smart IoT Hub, the
KE100 is a bridged device without a Matter product id, so the device registry
names it by its model, ``KE100``. Over Matter it publishes a local temperature
calibration entity but applies no offset written to it: neither the
temperature it reports nor its valve follows the offset. An offset
calibration computes every correction on top of the last one and so runs the
offset to the end of its range, so for a KE100 on Matter the quirk declares
the offset ignored. Better Thermostat then neither offers the offset
calibration for it nor writes an offset to it; the target temperature
calibration works as on any device.

The TP-Link and Tapo integrations register the KE100 under the same model,
and through them the offset has been seen to take effect, so the answer is
read off the integration the TRV's entity belongs to.

Everything else stays as the default quirks have it.
"""

from __future__ import annotations

from typing import Final

from homeassistant.helpers import entity_registry as er

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

# Platform Home Assistant's Matter integration registers its entities under.
MATTER_PLATFORM: Final = "matter"


def calibration_offset_ignored(self: RegistryHost, entity_id: str) -> bool:
    """Report whether the KE100 is paired over Matter, where it ignores its offset.

    The platform is read off the TRV's entity registry entry. An entity the
    registry does not know is driven like any other device.

    Parameters
    ----------
    self : RegistryHost
        Caller providing Home Assistant access.
    entity_id : str
        Entity id of the TRV.

    Returns
    -------
    bool
        True when the TRV's entity belongs to the Matter integration.
    """
    entry = er.async_get(self.hass).async_get(entity_id)
    return entry is not None and entry.platform == MATTER_PLATFORM


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    initial_tweak = staticmethod(initial_tweak)
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    calibration_offset_ignored = staticmethod(calibration_offset_ignored)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
_IGNORED_OFFSET_QUIRK: IgnoredOffsetQuirk = _Surface()
