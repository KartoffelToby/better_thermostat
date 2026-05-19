"""Preset management for Better Thermostat."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.components.climate.const import (
    PRESET_ACTIVITY,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    PRESET_SLEEP,
)

_DEFAULT_ENABLED_PRESETS: list[str] = [
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_SLEEP,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ACTIVITY,
    PRESET_HOME,
]

_DEFAULT_TEMPERATURES: dict[str, float] = {
    PRESET_NONE: 20.0,
    PRESET_AWAY: 16.0,
    PRESET_BOOST: 24.0,
    PRESET_COMFORT: 21.0,
    PRESET_ECO: 19.0,
    PRESET_HOME: 20.0,
    PRESET_SLEEP: 18.0,
    PRESET_ACTIVITY: 22.0,
}


@dataclass
class PresetManager:
    """Manages preset modes and their associated temperatures."""

    mode: str = PRESET_NONE
    temperatures: dict[str, float] = field(default_factory=_DEFAULT_TEMPERATURES.copy)
    enabled_presets: list[str] = field(default_factory=_DEFAULT_ENABLED_PRESETS.copy)
    saved_temperature: float | None = None

    @property
    def available_modes(self) -> list[str]:
        """Return list of available preset modes (NONE + enabled)."""
        return [PRESET_NONE] + self.enabled_presets

    def activate(
        self, preset: str, current_temp: float, min_temp: float, max_temp: float
    ) -> float | None:
        """Switch to *preset*. Returns new target temperature, or ``None``."""
        if preset not in self.available_modes:
            return None

        old = self.mode
        self.mode = preset

        # Save temp when leaving NONE
        if old == PRESET_NONE and preset != PRESET_NONE:
            if self.saved_temperature is None:
                self.saved_temperature = current_temp

        # Restore when returning to NONE
        if preset == PRESET_NONE and self.saved_temperature is not None:
            temp = self.saved_temperature
            self.saved_temperature = None
            return temp

        # Apply preset temp — fall back through (preset, PRESET_NONE, midpoint)
        # so an enabled preset missing from ``temperatures`` still produces a
        # sensible clamped target.
        if preset != PRESET_NONE:
            temp = self.temperatures.get(
                preset, self.temperatures.get(PRESET_NONE, (min_temp + max_temp) / 2)
            )
            return min(max_temp, max(min_temp, temp))

        return None

    def deactivate(self) -> float | None:
        """Return to PRESET_NONE. Returns the previously saved temperature."""
        if self.mode == PRESET_NONE:
            return None
        self.mode = PRESET_NONE
        temp = self.saved_temperature
        self.saved_temperature = None
        return temp

    def update_temperature(self, preset: str, value: float) -> None:
        """Set the stored temperature for *preset*."""
        self.temperatures[preset] = value

    def get_temperature(self, preset: str) -> float | None:
        """Return the stored temperature for *preset*, or ``None``."""
        return self.temperatures.get(preset)
