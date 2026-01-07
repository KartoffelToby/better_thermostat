"""Tests for device model detection.

Issue #1672: Wrong model detection from Z2M

The bug: Z2M reports device models as "MODEL_ID (Description)" but the code
was extracting text from inside parentheses instead of before them.

Example:
- Input: "TS0601 _TZE284_cvub6xbb (Beok wall thermostat)"
- Expected: "TS0601 _TZE284_cvub6xbb" (the model identifier)
- Actual (bug): "Beok wall thermostat" (the description in parentheses)
"""

import re
from unittest.mock import MagicMock, patch

import pytest


class TestModelDetectionFromString:
    """Tests for model string parsing logic."""

    def test_z2m_format_extracts_model_before_parentheses(self):
        """Test that Z2M format 'MODEL (Description)' extracts the model correctly.

        This is the core bug from issue #1672.
        """
        model_str = "TS0601 _TZE284_cvub6xbb (Beok wall thermostat)"

        # Current buggy behavior - extracts from inside parentheses
        buggy_matches = re.findall(r"\((.+?)\)", model_str)
        buggy_result = buggy_matches[-1].strip() if buggy_matches else model_str
        assert buggy_result == "Beok wall thermostat"  # This is the bug!

        # Expected behavior - extract text BEFORE parentheses
        # Remove everything from first '(' onwards
        expected_result = re.sub(r"\s*\(.*\)\s*$", "", model_str).strip()
        assert expected_result == "TS0601 _TZE284_cvub6xbb"

    def test_model_without_parentheses_unchanged(self):
        """Test that models without parentheses are returned as-is."""
        model_str = "TRVZB"

        # No parentheses, should return as-is
        result = re.sub(r"\s*\(.*\)\s*$", "", model_str).strip()
        assert result == "TRVZB"

    def test_model_with_parentheses_at_end(self):
        """Test various Z2M format strings."""
        test_cases = [
            # (input, expected_output)
            (
                "TS0601 _TZE284_cvub6xbb (Beok wall thermostat)",
                "TS0601 _TZE284_cvub6xbb",
            ),
            ("SNZB-02 (Temperature sensor)", "SNZB-02"),
            ("TRV (Sonoff TRVZB)", "TRV"),
            ("TRVZB", "TRVZB"),  # No parentheses
            (
                "Thermostat radiator valve",
                "Thermostat radiator valve",
            ),  # No parentheses
            ("Model123 (Some Description) ", "Model123"),  # Trailing space
            (" Model123 (Description)", "Model123"),  # Leading space
        ]

        for input_str, expected in test_cases:
            result = re.sub(r"\s*\(.*\)\s*$", "", input_str).strip()
            assert result == expected, f"Failed for input: {input_str!r}"

    def test_nested_parentheses_handled(self):
        """Test that nested parentheses are handled correctly."""
        # Edge case: nested parentheses - should remove trailing parentheses and everything within
        model_str = "Model (Description (with nested))"
        result = re.sub(r"\s*\(.*\)\s*$", "", model_str).strip()
        assert result == "Model"

    def test_parentheses_in_middle_preserved(self):
        """Test that parentheses not at end are preserved."""
        # Parentheses in middle (not at end) - should preserve
        model_str = "Model (v2) Pro"
        # This is a tricky case - the regex only removes trailing parentheses
        result = re.sub(r"\s*\(.*\)\s*$", "", model_str).strip()
        # Since ") Pro" doesn't match "\)\s*$", nothing is removed
        assert result == "Model (v2) Pro"


@pytest.fixture
def anyio_backend() -> str:
    """Configure anyio to use asyncio backend."""
    return "asyncio"


class TestGetDeviceModelFunction:
    """Integration tests for the get_device_model function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.hass = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.model = "configured_model"
        return mock

    @pytest.mark.anyio
    async def test_get_device_model_z2m_format(self, mock_self):
        """Test get_device_model with Z2M format device.model."""
        from custom_components.better_thermostat.utils.helpers import get_device_model

        # Mock entity registry
        mock_entry = MagicMock()
        mock_entry.device_id = "device_123"

        # Mock device with Z2M format model string
        mock_device = MagicMock()
        mock_device.model_id = None  # No model_id, so it falls back to model
        mock_device.model = "TS0601 _TZE284_cvub6xbb (Beok wall thermostat)"
        mock_device.manufacturer = "TuYa"
        mock_device.name = "Thermostat"
        mock_device.identifiers = set()

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get"
        ) as mock_er:
            with patch(
                "custom_components.better_thermostat.utils.helpers.dr.async_get"
            ) as mock_dr:
                mock_entity_reg = MagicMock()
                mock_entity_reg.async_get.return_value = mock_entry
                mock_er.return_value = mock_entity_reg

                mock_dev_reg = MagicMock()
                mock_dev_reg.async_get.return_value = mock_device
                mock_dr.return_value = mock_dev_reg

                result = await get_device_model(mock_self, "climate.test_trv")

                # Should extract model BEFORE parentheses, not inside
                # This verifies the fix for issue #1672
                assert result == "TS0601 _TZE284_cvub6xbb", (
                    f"Expected 'TS0601 _TZE284_cvub6xbb' but got '{result}'"
                )

    @pytest.mark.anyio
    async def test_get_device_model_with_model_id(self, mock_self):
        """Test that model_id takes priority over model string."""
        from custom_components.better_thermostat.utils.helpers import get_device_model

        mock_entry = MagicMock()
        mock_entry.device_id = "device_123"

        mock_device = MagicMock()
        mock_device.model_id = "TS0601"  # Has model_id
        mock_device.model = "TS0601 _TZE284_cvub6xbb (Beok wall thermostat)"
        mock_device.manufacturer = "TuYa"
        mock_device.name = "Thermostat"
        mock_device.identifiers = set()

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get"
        ) as mock_er:
            with patch(
                "custom_components.better_thermostat.utils.helpers.dr.async_get"
            ) as mock_dr:
                mock_entity_reg = MagicMock()
                mock_entity_reg.async_get.return_value = mock_entry
                mock_er.return_value = mock_entity_reg

                mock_dev_reg = MagicMock()
                mock_dev_reg.async_get.return_value = mock_device
                mock_dr.return_value = mock_dev_reg

                result = await get_device_model(mock_self, "climate.test_trv")

                # model_id should take priority
                assert result == "TS0601"

    @pytest.mark.anyio
    async def test_get_device_model_plain_string(self, mock_self):
        """Test model detection with plain string (no parentheses)."""
        from custom_components.better_thermostat.utils.helpers import get_device_model

        mock_entry = MagicMock()
        mock_entry.device_id = "device_123"

        mock_device = MagicMock()
        mock_device.model_id = None
        mock_device.model = "TRVZB"  # Plain string, no parentheses
        mock_device.manufacturer = "Sonoff"
        mock_device.name = "Thermostat"
        mock_device.identifiers = set()

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get"
        ) as mock_er:
            with patch(
                "custom_components.better_thermostat.utils.helpers.dr.async_get"
            ) as mock_dr:
                mock_entity_reg = MagicMock()
                mock_entity_reg.async_get.return_value = mock_entry
                mock_er.return_value = mock_entity_reg

                mock_dev_reg = MagicMock()
                mock_dev_reg.async_get.return_value = mock_device
                mock_dr.return_value = mock_dev_reg

                result = await get_device_model(mock_self, "climate.test_trv")

                assert result == "TRVZB"
