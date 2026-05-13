"""Tests for PresetManager."""

import pytest
from homeassistant.components.climate.const import (
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
)

from custom_components.better_thermostat.utils.preset_manager import (
    PresetManager,
    _DEFAULT_ENABLED_PRESETS,
    _DEFAULT_TEMPERATURES,
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
        temperatures={
            PRESET_NONE: 20.0,
            PRESET_COMFORT: 22.0,
            PRESET_ECO: 18.0,
        },
    )


# ---------------------------------------------------------------------------
# available_modes
# ---------------------------------------------------------------------------

class TestAvailableModes:
    def test_default_presets(self, mgr: PresetManager):
        modes = mgr.available_modes
        assert modes[0] == PRESET_NONE
        assert set(modes[1:]) == set(_DEFAULT_ENABLED_PRESETS)

    def test_custom_presets(self, custom_mgr: PresetManager):
        assert custom_mgr.available_modes == [PRESET_NONE, PRESET_COMFORT, PRESET_ECO]


# ---------------------------------------------------------------------------
# activate()
# ---------------------------------------------------------------------------

class TestActivate:
    def test_none_to_comfort_saves_and_returns_preset_temp(self, mgr: PresetManager):
        result = mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        assert mgr.mode == PRESET_COMFORT
        assert mgr.saved_temperature == 20.0
        assert result == _DEFAULT_TEMPERATURES[PRESET_COMFORT]

    def test_comfort_to_none_restores_saved_temp(self, mgr: PresetManager):
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        result = mgr.activate(PRESET_NONE, current_temp=21.0, min_temp=5.0, max_temp=30.0)
        assert result == 20.0
        assert mgr.saved_temperature is None
        assert mgr.mode == PRESET_NONE

    def test_comfort_to_eco_keeps_saved_temp(self, mgr: PresetManager):
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        result = mgr.activate(PRESET_ECO, current_temp=21.0, min_temp=5.0, max_temp=30.0)
        assert result == _DEFAULT_TEMPERATURES[PRESET_ECO]
        # saved_temperature should still hold the original value
        assert mgr.saved_temperature == 20.0

    def test_clamping_to_min(self, mgr: PresetManager):
        mgr.temperatures[PRESET_AWAY] = 3.0
        result = mgr.activate(PRESET_AWAY, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        assert result == 5.0

    def test_clamping_to_max(self, mgr: PresetManager):
        mgr.temperatures[PRESET_BOOST] = 50.0
        result = mgr.activate(PRESET_BOOST, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        assert result == 30.0

    def test_invalid_preset_returns_none(self, mgr: PresetManager):
        result = mgr.activate("nonexistent", current_temp=20.0, min_temp=5.0, max_temp=30.0)
        assert result is None
        assert mgr.mode == PRESET_NONE

    def test_none_to_none_is_noop(self, mgr: PresetManager):
        result = mgr.activate(PRESET_NONE, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        assert result is None
        assert mgr.saved_temperature is None

    def test_same_preset_is_idempotent(self, mgr: PresetManager):
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        saved_before = mgr.saved_temperature
        result = mgr.activate(PRESET_COMFORT, current_temp=21.0, min_temp=5.0, max_temp=30.0)
        assert result == _DEFAULT_TEMPERATURES[PRESET_COMFORT]
        # saved_temperature must not be overwritten
        assert mgr.saved_temperature == saved_before

    def test_double_activate_does_not_overwrite_saved(self, mgr: PresetManager):
        """Activating two presets in a row should keep original saved temp."""
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        mgr.activate(PRESET_ECO, current_temp=21.0, min_temp=5.0, max_temp=30.0)
        assert mgr.saved_temperature == 20.0


# ---------------------------------------------------------------------------
# deactivate()
# ---------------------------------------------------------------------------

class TestDeactivate:
    def test_deactivate_restores_temp(self, mgr: PresetManager):
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        result = mgr.deactivate()
        assert result == 20.0
        assert mgr.mode == PRESET_NONE
        assert mgr.saved_temperature is None

    def test_deactivate_from_none_returns_none(self, mgr: PresetManager):
        result = mgr.deactivate()
        assert result is None
        assert mgr.mode == PRESET_NONE


# ---------------------------------------------------------------------------
# update_temperature / get_temperature
# ---------------------------------------------------------------------------

class TestTemperatureAccess:
    def test_update_and_get(self, mgr: PresetManager):
        mgr.update_temperature(PRESET_COMFORT, 23.5)
        assert mgr.get_temperature(PRESET_COMFORT) == 23.5

    def test_get_unknown_preset_returns_none(self, mgr: PresetManager):
        assert mgr.get_temperature("nonexistent") is None

    def test_update_creates_new_entry(self, mgr: PresetManager):
        mgr.update_temperature("custom_preset", 19.0)
        assert mgr.get_temperature("custom_preset") == 19.0


# ---------------------------------------------------------------------------
# saved_temperature lifecycle
# ---------------------------------------------------------------------------

class TestSavedTemperatureLifecycle:
    def test_save_on_activate_restore_on_deactivate(self, mgr: PresetManager):
        mgr.activate(PRESET_AWAY, current_temp=21.5, min_temp=5.0, max_temp=30.0)
        assert mgr.saved_temperature == 21.5
        restored = mgr.deactivate()
        assert restored == 21.5
        assert mgr.saved_temperature is None

    def test_preset_to_preset_keeps_saved(self, mgr: PresetManager):
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        mgr.activate(PRESET_ECO, current_temp=21.0, min_temp=5.0, max_temp=30.0)
        assert mgr.saved_temperature == 20.0

    def test_double_activate_from_none_does_not_overwrite(self, mgr: PresetManager):
        mgr.activate(PRESET_COMFORT, current_temp=20.0, min_temp=5.0, max_temp=30.0)
        # Simulate scenario: already in comfort, activate again
        mgr.activate(PRESET_COMFORT, current_temp=25.0, min_temp=5.0, max_temp=30.0)
        assert mgr.saved_temperature == 20.0


# ---------------------------------------------------------------------------
# Dataclass defaults / isolation
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_instances_do_not_share_state(self):
        mgr1 = PresetManager()
        mgr2 = PresetManager()
        mgr1.temperatures[PRESET_COMFORT] = 99.0
        assert mgr2.temperatures[PRESET_COMFORT] == _DEFAULT_TEMPERATURES[PRESET_COMFORT]

    def test_default_mode_is_none(self, mgr: PresetManager):
        assert mgr.mode == PRESET_NONE

    def test_default_saved_temperature_is_none(self, mgr: PresetManager):
        assert mgr.saved_temperature is None
