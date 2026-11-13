"""Quirks for the Sonoff TRV-ZBT (TRV Gen2) Zigbee TRV.

The TRV-ZBT regulates on an external temperature once its sensor selector
names one, and reads that temperature from an input nobody else keeps
current. That input and the selector carry the same names as on the TRVZB,
so the TRVZB write is reused to mirror the room temperature into it.

Its valve takes the opening its own controller holds while heating and the
one it holds while idle. Better Thermostat writes its position into both,
so the valve holds that position whichever state the controller is in.
The TRVZB write handles the pair; the close bump the TRVZB motor needs is
left out.
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
    has_valve_channel,
    maybe_set_external_temperature,
    maybe_set_sonoff_valve_percent,
    valve_readings,
)
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    ModelFixHost,
    ModelQuirks,
    ValveChannelQuirk,
    ValveQuirk,
    ValveReadingsQuirk,
)

__all__ = [
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "has_valve_channel",
    "initial_tweak",
    "maybe_set_external_temperature",
    "override_set_hvac_mode",
    "override_set_temperature",
    "override_set_valve",
    "valve_readings",
]


async def override_set_valve(self: ModelFixHost, entity_id: str, percent: int) -> bool:
    """Write the valve position into the heating and the idle opening.

    Returns True when the position went out, False to let the adapter
    fallback run.
    """
    return await maybe_set_sonoff_valve_percent(
        self, entity_id, max(0, min(100, percent))
    )


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
    override_set_valve = staticmethod(override_set_valve)
    has_valve_channel = staticmethod(has_valve_channel)
    valve_readings = staticmethod(valve_readings)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_EXTERNAL_TEMPERATURE_QUIRK: ExternalTemperatureQuirk = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
_VALVE_QUIRK: ValveQuirk = _Surface()
_VALVE_CHANNEL_QUIRK: ValveChannelQuirk = _Surface()
_VALVE_READINGS_QUIRK: ValveReadingsQuirk = _Surface()
