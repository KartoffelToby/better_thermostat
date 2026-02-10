"""Tests for mode_remap function.

This module tests the mode_remap function which handles HVAC mode translation
between Better Thermostat and TRVs. This includes handling quirks like
heat_auto_swapped devices and TRVs that only support HEAT_COOL but not HEAT.
"""

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.utils.helpers import mode_remap


class MockThermostat:
    """Mock Better Thermostat instance for testing."""

    def __init__(self, device_name="Test"):
        """Initialize mock thermostat."""
        self.device_name = device_name
        self.real_trvs = {}

    def add_trv(self, entity_id, heat_auto_swapped=False, hvac_modes=None):
        """Add a TRV configuration."""
        if hvac_modes is None:
            hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]

        self.real_trvs[entity_id] = {
            "advanced": {"heat_auto_swapped": heat_auto_swapped},
            "hvac_modes": hvac_modes,
        }


class TestModeRemapBasic:
    """Test basic mode_remap functionality."""

    def test_returns_mode_unchanged_when_no_remapping_needed(self):
        """Test that modes are returned unchanged when no remapping is needed."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test")

        # OFF should stay OFF
        result = mode_remap(mock_bt, "climate.test", HVACMode.OFF)
        assert result == HVACMode.OFF

        # HEAT should stay HEAT
        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT)
        assert result == HVACMode.HEAT

    def test_returns_off_for_unsupported_auto_mode(self):
        """Test that AUTO mode returns OFF when not supported and logs error."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT])

        result = mode_remap(mock_bt, "climate.test", HVACMode.AUTO)
        assert result == HVACMode.OFF


class TestModeRemapHeatAutoSwapped:
    """Test mode_remap with heat_auto_swapped configuration."""

    def test_outbound_heat_becomes_auto_when_swapped(self):
        """Test that HEAT becomes AUTO for outbound when heat_auto_swapped."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", heat_auto_swapped=True)

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.AUTO

    def test_inbound_auto_becomes_heat_when_swapped(self):
        """Test that AUTO becomes HEAT for inbound when heat_auto_swapped."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", heat_auto_swapped=True)

        result = mode_remap(mock_bt, "climate.test", HVACMode.AUTO, inbound=True)
        assert result == HVACMode.HEAT

    def test_other_modes_unchanged_when_swapped(self):
        """Test that other modes are unchanged when heat_auto_swapped."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", heat_auto_swapped=True)

        # OFF should stay OFF
        result = mode_remap(mock_bt, "climate.test", HVACMode.OFF, inbound=False)
        assert result == HVACMode.OFF

        # COOL should stay COOL
        result = mode_remap(mock_bt, "climate.test", HVACMode.COOL, inbound=False)
        assert result == HVACMode.COOL

    def test_heat_auto_swap_takes_precedence(self):
        """Test that heat_auto_swapped takes precedence over other remapping."""
        mock_bt = MockThermostat()
        # TRV that supports HEAT_COOL but has heat_auto_swapped set
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT_COOL],
        )

        # Should swap HEAT to AUTO, not to HEAT_COOL
        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.AUTO


class TestModeRemapHeatCoolTranslation:
    """Test mode_remap translation between HEAT and HEAT_COOL."""

    def test_outbound_heat_becomes_heat_cool_when_no_heat_support(self):
        """Test HEAT becomes HEAT_COOL when TRV only supports HEAT_COOL."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT_COOL])

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT_COOL

    def test_inbound_heat_cool_becomes_heat_when_no_heat_support(self):
        """Test HEAT_COOL becomes HEAT when receiving from TRV."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT_COOL])

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=True)
        assert result == HVACMode.HEAT

    def test_no_translation_when_heat_is_supported(self):
        """Test that HEAT is not translated when TRV supports it."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT

    def test_heat_cool_stays_when_both_supported(self):
        """Test that HEAT_COOL stays when both HEAT and HEAT_COOL supported."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False)
        assert result == HVACMode.HEAT_COOL


class TestModeRemapEdgeCases:
    """Test edge cases and potential bugs."""

    def test_missing_entity_id(self):
        """Test behavior when entity_id is not in real_trvs."""
        mock_bt = MockThermostat()
        # Don't add any TRVs

        try:
            mode_remap(mock_bt, "climate.missing", HVACMode.HEAT, inbound=False)
            pytest.fail("Should have raised KeyError for missing entity")
        except KeyError:
            # Expected - we found a potential crash scenario
            pass

    def test_missing_advanced_key(self):
        """Test behavior when 'advanced' key is missing."""
        mock_bt = MockThermostat()
        # Add TRV without 'advanced' key
        mock_bt.real_trvs["climate.test"] = {
            "hvac_modes": [HVACMode.OFF, HVACMode.HEAT]
        }

        try:
            mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
            pytest.fail("Should have raised KeyError for missing 'advanced' key")
        except KeyError:
            # Expected - we found a potential crash scenario
            pass

    def test_missing_hvac_modes_key(self):
        """Test behavior when 'hvac_modes' key is missing."""
        mock_bt = MockThermostat()
        mock_bt.real_trvs["climate.test"] = {"advanced": {"heat_auto_swapped": False}}

        try:
            mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
            pytest.fail("Should have raised KeyError for missing 'hvac_modes' key")
        except KeyError:
            # Expected - we found a potential crash scenario
            pass

    def test_cool_mode_handling(self):
        """Test handling of COOL mode."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.COOL, inbound=False)
        assert result == HVACMode.COOL

    def test_dry_mode_handling(self):
        """Test handling of DRY mode."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.DRY]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.DRY, inbound=False)
        assert result == HVACMode.DRY

    def test_fan_only_mode_handling(self):
        """Test handling of FAN_ONLY mode."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.FAN_ONLY]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.FAN_ONLY, inbound=False)
        assert result == HVACMode.FAN_ONLY
