"""Mode region: OFF / HEAT / COOL / HEAT_COOL, crossed with a preset.

The region is a validated value holder: invalid or unknown inputs leave
the state unchanged instead of corrupting it, and the preset axis is
orthogonal to the HVAC mode axis.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..snapshot import HvacMode, Preset, parse_hvac_mode, parse_preset


@dataclass(frozen=True)
class ModeState:
    """State of the mode region.

    Attributes
    ----------
    hvac_mode : HvacMode
        Current HVAC operating mode (OFF, HEAT, COOL, HEAT_COOL).
    preset : Preset | None
        Active preset, or ``None`` when no preset is active.
    """

    hvac_mode: HvacMode = HvacMode.OFF
    preset: Preset | None = None


def set_hvac_mode(state: ModeState, mode: str | None) -> ModeState:
    """Set the HVAC mode; unknown or missing values leave the state unchanged.

    Parameters
    ----------
    state : ModeState
        Current mode state.
    mode : str | None
        HVAC mode string to parse and apply.

    Returns
    -------
    ModeState
        Updated state, or ``state`` unchanged when ``mode`` is invalid.
    """
    parsed = parse_hvac_mode(mode)
    if parsed is None:
        return state
    return ModeState(hvac_mode=parsed, preset=state.preset)


def set_preset(state: ModeState, preset: str | None) -> ModeState:
    """Set the preset; ``Preset.NONE`` and empty values clear it.

    Parameters
    ----------
    state : ModeState
        Current mode state.
    preset : str | None
        Preset name to apply; ``None``, ``Preset.NONE`` or an empty string
        clears the active preset, an unknown name leaves the state unchanged.

    Returns
    -------
    ModeState
        Updated state with the new (or cleared) preset.
    """
    if preset is None or preset == "":
        return ModeState(hvac_mode=state.hvac_mode, preset=None)
    parsed = parse_preset(preset)
    if parsed is None:
        return state
    if parsed == Preset.NONE:
        return ModeState(hvac_mode=state.hvac_mode, preset=None)
    return ModeState(hvac_mode=state.hvac_mode, preset=parsed)
