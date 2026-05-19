"""Tests for PresetManager."""

from homeassistant.components.climate.const import (
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
)
import pytest

from custom_components.better_thermostat.utils.preset_manager import (
    _DEFAULT_ENABLED_PRESETS,
    _DEFAULT_TEMPERATURES,
    PresetManager,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mgr() -> PresetManager:
    """Return a fresh PresetManager with defaults."""
    return PresetManager()


@pytest.fixture
def custom_mgr() -> PresetManager:
    """Return a PresetManager with custom presets."""
    return PresetManager(
        enabled_presets=[PRESET_COMFORT, PRESET_ECO],
        temperatures={PRESET_NONE: 20.0, PRESET_COMFORT: 22.0, PRESET_ECO: 18.0},
    )


# ---------------------------------------------------------------------------
# available_modes
# ---------------------------------------------------------------------------


class TestAvailableModes:
    """available_modes property returns PRESET_NONE followed by enabled presets."""

    def test_default_presets(self, mgr: PresetManager):
        """Defaults expose PRESET_NONE plus the standard enabled set."""
        modes = mgr.available_modes
        assert modes[0] == PRESET_NONE
        assert set(modes[1:]) == set(_DEFAULT_ENABLED_PRESETS)

    def test_custom_presets(self, custom_mgr: PresetManager):
        """Custom enabled_presets are exposed in the configured order."""
        assert custom_mgr.available_modes == [PRESET_NONE, PRESET_COMFORT, PRESET_ECO]

    def test_empty_enabled_presets_yields_none_only(self):
        """An explicit empty list disables all presets; only PRESET_NONE remains."""
        mgr = PresetManager(enabled_presets=[])
        assert mgr.available_modes == [PRESET_NONE]


# ---------------------------------------------------------------------------
# activate()
# ---------------------------------------------------------------------------


class TestActivate:
    """activate() switches presets, saves/restores user temperature, clamps to bounds."""

    def test_none_to_comfort_saves_and_returns_preset_temp(self, mgr: PresetManager):
        """Going NONE→COMFORT saves the current temp and returns the preset value."""
        result = mgr.activate(
            PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0
        )
        assert mgr.mode == PRESET_COMFORT
        assert mgr.saved_temperature == 20.0
        assert result == _DEFAULT_TEMPERATURES[PRESET_COMFORT]

    def test_comfort_to_none_restores_saved_temp(self, mgr: PresetManager):
        """Returning to NONE restores the previously saved user temperature."""
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        result = mgr.activate(
            PRESET_NONE, current_temp=21.0, min_temp=5.0, max_temp=30.0
        )
        assert result == 20.0
        assert mgr.saved_temperature is None
        assert mgr.mode == PRESET_NONE

    def test_comfort_to_eco_keeps_saved_temp(self, mgr: PresetManager):
        """Preset→preset transitions preserve the originally saved temperature."""
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        result = mgr.activate(
            PRESET_ECO, current_temp=21.0, min_temp=5.0, max_temp=30.0
        )
        assert result == _DEFAULT_TEMPERATURES[PRESET_ECO]
        # saved_temperature should still hold the original value
        assert mgr.saved_temperature == 20.0

    def test_clamping_to_min(self, mgr: PresetManager):
        """Preset values below min_temp are clamped to min_temp."""
        mgr.temperatures[PRESET_AWAY] = 3.0
        result = mgr.activate(
            PRESET_AWAY, current_temp=20.0, min_temp=5.0, max_temp=30.0
        )
        assert result == 5.0

    def test_clamping_to_max(self, mgr: PresetManager):
        """Preset values above max_temp are clamped to max_temp."""
        mgr.temperatures[PRESET_BOOST] = 50.0
        result = mgr.activate(
            PRESET_BOOST, current_temp=20.0, min_temp=5.0, max_temp=30.0
        )
        assert result == 30.0

    def test_invalid_preset_returns_none(self, mgr: PresetManager):
        """Activating an unknown preset name is a no-op returning None."""
        result = mgr.activate(
            "nonexistent", current_temp=20.0, min_temp=5.0, max_temp=30.0
        )
        assert result is None
        assert mgr.mode == PRESET_NONE

    def test_none_to_none_is_noop(self, mgr: PresetManager):
        """Activating NONE while already on NONE is a no-op (nothing to save)."""
        result = mgr.activate(
            PRESET_NONE, current_temp=20.0, min_temp=5.0, max_temp=30.0
        )
        assert result is None
        assert mgr.saved_temperature is None

    def test_same_preset_is_idempotent(self, mgr: PresetManager):
        """Re-activating the current preset is idempotent and does not re-save."""
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        saved_before = mgr.saved_temperature
        result = mgr.activate(
            PRESET_COMFORT, current_temp=21.0, min_temp=5.0, max_temp=30.0
        )
        assert result == _DEFAULT_TEMPERATURES[PRESET_COMFORT]
        # saved_temperature must not be overwritten
        assert mgr.saved_temperature == saved_before

    def test_double_activate_does_not_overwrite_saved(self, mgr: PresetManager):
        """Activating two presets in a row should keep original saved temp."""
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        mgr.activate(PRESET_ECO, current_temp=21.0, min_temp=5.0, max_temp=30.0)
        assert mgr.saved_temperature == 20.0

    def test_enabled_preset_missing_from_temperatures_falls_back(self):
        """Enabled preset absent from temperatures falls back via PRESET_NONE.

        Still yields a clamped target.
        """
        mgr = PresetManager(
            enabled_presets=[PRESET_COMFORT],
            temperatures={PRESET_NONE: 19.5},  # COMFORT intentionally missing
        )
        result = mgr.activate(
            PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0
        )
        assert result == 19.5
        assert mgr.mode == PRESET_COMFORT

    def test_enabled_preset_missing_and_no_none_default_uses_midpoint(self):
        """No preset value and no PRESET_NONE default → midpoint of min/max."""
        mgr = PresetManager(enabled_presets=[PRESET_COMFORT], temperatures={})
        result = mgr.activate(
            PRESET_COMFORT, current_temp=20.0, min_temp=10.0, max_temp=30.0
        )
        assert result == 20.0  # (10 + 30) / 2


# ---------------------------------------------------------------------------
# deactivate()
# ---------------------------------------------------------------------------


class TestDeactivate:
    """deactivate() returns to PRESET_NONE and restores the saved temperature."""

    def test_deactivate_restores_temp(self, mgr: PresetManager):
        """deactivate() restores the saved temperature and clears state."""
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        result = mgr.deactivate()
        assert result == 20.0
        assert mgr.mode == PRESET_NONE
        assert mgr.saved_temperature is None

    def test_deactivate_from_none_returns_none(self, mgr: PresetManager):
        """deactivate() with no preset active returns None (nothing to restore)."""
        result = mgr.deactivate()
        assert result is None
        assert mgr.mode == PRESET_NONE


# ---------------------------------------------------------------------------
# update_temperature / get_temperature
# ---------------------------------------------------------------------------


class TestTemperatureAccess:
    """update_temperature() and get_temperature() round-trip preset values."""

    def test_update_and_get(self, mgr: PresetManager):
        """A stored preset value is returned verbatim by get_temperature()."""
        mgr.update_temperature(PRESET_COMFORT, 23.5)
        assert mgr.get_temperature(PRESET_COMFORT) == 23.5

    def test_get_unknown_preset_returns_none(self, mgr: PresetManager):
        """get_temperature() returns None for unknown preset names."""
        assert mgr.get_temperature("nonexistent") is None

    def test_update_creates_new_entry(self, mgr: PresetManager):
        """update_temperature() inserts new preset entries on demand."""
        mgr.update_temperature("custom_preset", 19.0)
        assert mgr.get_temperature("custom_preset") == 19.0


# ---------------------------------------------------------------------------
# saved_temperature lifecycle
# ---------------------------------------------------------------------------


class TestSavedTemperatureLifecycle:
    """End-to-end lifecycle of saved_temperature across activate/deactivate cycles."""

    def test_save_on_activate_restore_on_deactivate(self, mgr: PresetManager):
        """Saved temperature is set on activation and cleared on deactivation."""
        mgr.activate(PRESET_AWAY, current_temp=21.5, min_temp=5.0, max_temp=30.0)
        assert mgr.saved_temperature == 21.5
        restored = mgr.deactivate()
        assert restored == 21.5
        assert mgr.saved_temperature is None

    def test_preset_to_preset_keeps_saved(self, mgr: PresetManager):
        """Switching between presets preserves the originally saved temperature."""
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        mgr.activate(PRESET_ECO, current_temp=21.0, min_temp=5.0, max_temp=30.0)
        assert mgr.saved_temperature == 20.0

    def test_double_activate_from_none_does_not_overwrite(self, mgr: PresetManager):
        """Re-activating the same preset does not overwrite the saved temperature."""
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        # Simulate scenario: already in comfort, activate again
        mgr.activate(PRESET_COMFORT, current_temp=25.0, min_temp=5.0, max_temp=30.0)
        assert mgr.saved_temperature == 20.0


# ---------------------------------------------------------------------------
# Dataclass defaults / isolation
# ---------------------------------------------------------------------------


class TestDefaults:
    """Dataclass defaults isolate state between instances and start in the NONE preset."""

    def test_instances_do_not_share_state(self):
        """Each PresetManager gets its own temperatures dict (no shared default)."""
        mgr1 = PresetManager()
        mgr2 = PresetManager()
        mgr1.temperatures[PRESET_COMFORT] = 99.0
        assert (
            mgr2.temperatures[PRESET_COMFORT] == _DEFAULT_TEMPERATURES[PRESET_COMFORT]
        )

    def test_default_mode_is_none(self, mgr: PresetManager):
        """Fresh PresetManager starts in PRESET_NONE."""
        assert mgr.mode == PRESET_NONE

    def test_default_saved_temperature_is_none(self, mgr: PresetManager):
        """Fresh PresetManager has no saved_temperature."""
        assert mgr.saved_temperature is None
