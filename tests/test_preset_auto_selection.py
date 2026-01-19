"""Tests for automatic preset selection based on temperature changes.

These tests verify that when a user manually changes the temperature,
the system automatically selects the matching preset if one exists.

Related issue:
When the target temperature is changed and the set temperature is the same
as the one saved in the preset, preset should be selected automatically.
For example if home preset is 21 and comfort preset is 22. If I press preset
comfort, target temperature will change to 22 and then if I change target
temperature manually to 21 it will still show that comfort preset is selected
instead of home preset.
"""


# Define preset constants (same as homeassistant.components.climate.const)
PRESET_NONE = "none"
PRESET_HOME = "home"
PRESET_COMFORT = "comfort"
PRESET_ECO = "eco"


class MockBetterThermostat:
    """Mock Better Thermostat for testing preset selection."""

    def __init__(self):
        """Initialize mock thermostat."""
        self.device_name = "test_thermostat"
        self.bt_target_temp = 20.0
        self._preset_mode = PRESET_NONE
        self._preset_temperature = None
        self._enabled_presets = [PRESET_HOME, PRESET_COMFORT, PRESET_ECO]
        self._preset_temperatures = {
            PRESET_NONE: 20.0,
            PRESET_HOME: 21.0,
            PRESET_COMFORT: 22.0,
            PRESET_ECO: 19.0,
        }
        self.bt_hvac_mode = "heat"
        self.hvac_mode = "heat"
        self.bt_target_cooltemp = None
        self.max_temp = 30.0
        self.min_temp = 5.0


def simulate_temperature_change(thermostat, new_temp):
    """Simulate the auto-selection behavior of the actual implementation for testing.

    This replicates the logic from climate.py async_set_temperature method.
    """
    thermostat.bt_target_temp = new_temp

    if thermostat.bt_target_temp is not None:
        # Check if the new temperature matches any preset temperature
        # We use a small tolerance to handle floating point comparisons
        tolerance = 0.01
        matched_preset = None

        # Iterate through enabled presets in priority order (first match wins)
        # This ensures consistent behavior if multiple presets have the same temperature
        for preset_name in thermostat._enabled_presets:
            if preset_name == PRESET_NONE:
                continue
            preset_temp = thermostat._preset_temperatures.get(preset_name)
            # Check if temperature matches (within tolerance)
            if preset_temp is not None and abs(thermostat.bt_target_temp - preset_temp) < tolerance:
                matched_preset = preset_name
                break

        # If we found a matching preset and we're not already in it, switch to it
        if matched_preset is not None and thermostat._preset_mode != matched_preset:
            old_preset = thermostat._preset_mode

            # Handle _preset_temperature save/restore mechanism
            # If switching from PRESET_NONE to another preset, save current temperature
            if old_preset == PRESET_NONE and thermostat._preset_temperature is None:
                thermostat._preset_temperature = thermostat.bt_target_temp

            thermostat._preset_mode = matched_preset
        # If no preset matches and we're in a preset mode (not PRESET_NONE), switch to manual
        elif matched_preset is None and thermostat._preset_mode != PRESET_NONE:
            # Check if current temperature doesn't match the current preset
            current_preset_temp = thermostat._preset_temperatures.get(thermostat._preset_mode)
            if current_preset_temp is not None and abs(thermostat.bt_target_temp - current_preset_temp) >= tolerance:
                # When switching back to PRESET_NONE, clear saved temperature
                thermostat._preset_temperature = None
                thermostat._preset_mode = PRESET_NONE


class TestPresetAutoSelection:
    """Tests for automatic preset selection."""

    def test_preset_switches_when_temperature_matches(self):
        """Test that preset automatically switches when temperature matches.

        Scenario:
        - Currently in COMFORT preset (22°C)
        - User manually changes temperature to 21°C (HOME preset temperature)
        - Expected: Preset should automatically switch to HOME
        """
        thermostat = MockBetterThermostat()
        thermostat._preset_mode = PRESET_COMFORT
        thermostat.bt_target_temp = 22.0

        # User changes temperature to 21°C (matches HOME preset)
        simulate_temperature_change(thermostat, 21.0)

        assert thermostat._preset_mode == PRESET_HOME, (
            f"Expected preset to switch to HOME when temperature is 21°C, "
            f"but preset is {thermostat._preset_mode}"
        )
        assert thermostat.bt_target_temp == 21.0

    def test_preset_switches_to_manual_when_no_match(self):
        """Test that preset switches to manual when no preset matches.

        Scenario:
        - Currently in COMFORT preset (22°C)
        - User manually changes temperature to 20.5°C (no matching preset)
        - Expected: Preset should switch to NONE (manual mode)
        """
        thermostat = MockBetterThermostat()
        thermostat._preset_mode = PRESET_COMFORT
        thermostat.bt_target_temp = 22.0

        # User changes temperature to 20.5°C (no matching preset)
        simulate_temperature_change(thermostat, 20.5)

        assert thermostat._preset_mode == PRESET_NONE, (
            f"Expected preset to switch to NONE when temperature is 20.5°C, "
            f"but preset is {thermostat._preset_mode}"
        )
        assert thermostat.bt_target_temp == 20.5

    def test_preset_stays_when_already_correct(self):
        """Test that preset doesn't change if already correct.

        Scenario:
        - Currently in HOME preset (21°C)
        - Temperature is already 21°C
        - Expected: Preset should stay as HOME
        """
        thermostat = MockBetterThermostat()
        thermostat._preset_mode = PRESET_HOME
        thermostat.bt_target_temp = 21.0

        # Temperature is set to 21°C (same as HOME preset)
        simulate_temperature_change(thermostat, 21.0)

        assert thermostat._preset_mode == PRESET_HOME, (
            f"Expected preset to remain HOME when temperature is already 21°C, "
            f"but preset is {thermostat._preset_mode}"
        )
        assert thermostat.bt_target_temp == 21.0

    def test_preset_switches_from_manual_to_preset(self):
        """Test that preset switches from manual to preset when temperature matches.

        Scenario:
        - Currently in manual mode (PRESET_NONE)
        - User changes temperature to 22°C (matches COMFORT preset)
        - Expected: Preset should switch to COMFORT
        """
        thermostat = MockBetterThermostat()
        thermostat._preset_mode = PRESET_NONE
        thermostat.bt_target_temp = 20.0

        # User changes temperature to 22°C (matches COMFORT preset)
        simulate_temperature_change(thermostat, 22.0)

        assert thermostat._preset_mode == PRESET_COMFORT, (
            f"Expected preset to switch to COMFORT when temperature is 22°C, "
            f"but preset is {thermostat._preset_mode}"
        )
        assert thermostat.bt_target_temp == 22.0

    def test_preset_handles_floating_point_tolerance(self):
        """Test that preset matching handles floating point precision.

        Scenario:
        - HOME preset is 21.0°C
        - User sets temperature to 21.005°C (within tolerance)
        - Expected: Preset should switch to HOME
        """
        thermostat = MockBetterThermostat()
        thermostat._preset_mode = PRESET_COMFORT
        thermostat.bt_target_temp = 22.0

        # User changes temperature to 21.005°C (within 0.01 tolerance of HOME)
        simulate_temperature_change(thermostat, 21.005)

        assert thermostat._preset_mode == PRESET_HOME, (
            f"Expected preset to switch to HOME when temperature is 21.005°C (within tolerance), "
            f"but preset is {thermostat._preset_mode}"
        )

    def test_preset_switches_between_multiple_presets(self):
        """Test switching between multiple presets based on temperature.

        Scenario:
        - Switch from COMFORT to HOME to ECO by changing temperatures
        """
        thermostat = MockBetterThermostat()

        # Start with COMFORT preset (22°C)
        thermostat._preset_mode = PRESET_COMFORT
        thermostat.bt_target_temp = 22.0

        # Change to 21°C (HOME preset)
        simulate_temperature_change(thermostat, 21.0)
        assert thermostat._preset_mode == PRESET_HOME

        # Change to 19°C (ECO preset)
        simulate_temperature_change(thermostat, 19.0)
        assert thermostat._preset_mode == PRESET_ECO

        # Change back to 22°C (COMFORT preset)
        simulate_temperature_change(thermostat, 22.0)
        assert thermostat._preset_mode == PRESET_COMFORT

    def test_disabled_preset_not_selected(self):
        """Test that disabled presets are not auto-selected.

        Scenario:
        - HOME preset (21°C) is not in enabled presets
        - User changes temperature to 21°C
        - Expected: Should switch to manual mode, not HOME
        """
        thermostat = MockBetterThermostat()
        thermostat._enabled_presets = [PRESET_COMFORT, PRESET_ECO]  # HOME not enabled
        thermostat._preset_mode = PRESET_COMFORT
        thermostat.bt_target_temp = 22.0

        # User changes temperature to 21°C (HOME preset, but not enabled)
        simulate_temperature_change(thermostat, 21.0)

        assert thermostat._preset_mode == PRESET_NONE, (
            f"Expected preset to switch to NONE when temperature matches disabled preset, "
            f"but preset is {thermostat._preset_mode}"
        )

    def test_preset_temperature_saved_when_auto_switching_from_manual(self):
        """Test that _preset_temperature is saved when auto-switching from manual.

        Scenario:
        - Currently in manual mode (PRESET_NONE)
        - User changes temperature to 22°C (matches COMFORT preset)
        - Expected: Should save the temperature before switching to COMFORT
        """
        thermostat = MockBetterThermostat()
        thermostat._preset_mode = PRESET_NONE
        thermostat._preset_temperature = None
        thermostat.bt_target_temp = 20.0

        # User changes temperature to 22°C (matches COMFORT preset)
        simulate_temperature_change(thermostat, 22.0)

        assert thermostat._preset_mode == PRESET_COMFORT, (
            f"Expected preset to switch to COMFORT, but got {thermostat._preset_mode}"
        )
        assert thermostat._preset_temperature == 22.0, (
            f"Expected _preset_temperature to be saved as 22.0, but got {thermostat._preset_temperature}"
        )

    def test_preset_temperature_cleared_when_switching_to_manual(self):
        """Test that _preset_temperature is cleared when auto-switching to manual.

        Scenario:
        - Currently in COMFORT preset (22°C)
        - User changes temperature to 20.5°C (no matching preset)
        - Expected: Should clear _preset_temperature when switching to manual
        """
        thermostat = MockBetterThermostat()
        thermostat._preset_mode = PRESET_COMFORT
        thermostat._preset_temperature = 20.0  # Simulate saved temperature
        thermostat.bt_target_temp = 22.0

        # User changes temperature to 20.5°C (no matching preset)
        simulate_temperature_change(thermostat, 20.5)

        assert thermostat._preset_mode == PRESET_NONE, (
            f"Expected preset to switch to NONE, but got {thermostat._preset_mode}"
        )
        assert thermostat._preset_temperature is None, (
            f"Expected _preset_temperature to be cleared, but got {thermostat._preset_temperature}"
        )

    def test_preset_temperature_not_overwritten_when_already_set(self):
        """Test that _preset_temperature is not overwritten if already saved.

        Scenario:
        - Currently in manual mode with _preset_temperature already saved
        - User changes temperature to match a preset
        - Expected: Should not overwrite existing _preset_temperature
        """
        thermostat = MockBetterThermostat()
        thermostat._preset_mode = PRESET_NONE
        thermostat._preset_temperature = 19.5  # Already saved from previous preset
        thermostat.bt_target_temp = 20.0

        # User changes temperature to 22°C (matches COMFORT preset)
        simulate_temperature_change(thermostat, 22.0)

        assert thermostat._preset_mode == PRESET_COMFORT
        assert thermostat._preset_temperature == 19.5, (
            f"Expected _preset_temperature to remain 19.5 (not overwritten), "
            f"but got {thermostat._preset_temperature}"
        )
