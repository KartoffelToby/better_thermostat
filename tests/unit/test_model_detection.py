"""Tests for device model detection.

Zigbee2MQTT registers a device's model as ``MODEL_ID (Description)``, for
example ``TS0601 _TZE284_cvub6xbb (Beok wall thermostat)``; the model
identifier is ``TS0601 _TZE284_cvub6xbb``, the text before the description.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.utils.helpers import get_device_model
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry


@pytest.mark.parametrize(
    ("registry_model", "expected"),
    [
        pytest.param(
            "TS0601 _TZE284_cvub6xbb (Beok wall thermostat)",
            "TS0601 _TZE284_cvub6xbb",
            id="z2m_model_with_description",
        ),
        pytest.param("SNZB-02 (Temperature sensor)", "SNZB-02", id="short_model"),
        pytest.param("TRV (Sonoff TRVZB)", "TRV", id="description_names_a_model"),
        pytest.param("TRVZB", "TRVZB", id="no_parentheses"),
        pytest.param(
            "Thermostat radiator valve",
            "Thermostat radiator valve",
            id="words_without_parentheses",
        ),
        pytest.param("Model123 (Some Description) ", "Model123", id="trailing_space"),
        pytest.param(" Model123 (Description)", "Model123", id="leading_space"),
        pytest.param("Model (Description (with nested))", "Model", id="nested"),
        pytest.param("Model (v2) Pro", "Model (v2) Pro", id="parentheses_in_middle"),
    ],
)
async def test_registry_model_is_read_up_to_its_description(
    hass, registry_model, expected
):
    """The device model is the registry model without a trailing description.

    Zigbee2MQTT registers a device's model as ``MODEL (Description)``; the
    identifier is the part before the parenthesised description. A model
    without a trailing description is used as it stands.
    """
    config_entry = MockConfigEntry(domain="mqtt")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("mqtt", "trv_device")},
        model=registry_model,
    )
    entity = er.async_get(hass).async_get_or_create(
        "climate", "mqtt", "trv_unique", device_id=device.id, config_entry=config_entry
    )
    host = SimpleNamespace(hass=hass, device_name="Test Thermostat")

    assert await get_device_model(host, entity.entity_id) == expected


class TestGetDeviceModelFunction:
    """Integration tests for the get_device_model function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = ThermostatStandIn()
        mock.hass = MagicMock()
        mock.device_name = "Test Thermostat"
        return mock

    async def test_get_device_model_z2m_format(self, mock_self):
        """Test get_device_model with Z2M format device.model."""
        from custom_components.better_thermostat.utils.helpers import get_device_model

        # Mock entity registry
        mock_entry = make_registry_entry("climate.test_trv", device_id="device_123")

        # Mock device with Z2M format model string
        mock_device = MagicMock(spec=dr.DeviceEntry)
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
                mock_entity_reg = make_entity_registry(mock_entry)
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

    async def test_get_device_model_with_model_id(self, mock_self):
        """Test that model_id takes priority over model string."""
        from custom_components.better_thermostat.utils.helpers import get_device_model

        mock_entry = make_registry_entry("climate.test_trv", device_id="device_123")

        mock_device = MagicMock(spec=dr.DeviceEntry)
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
                mock_entity_reg = make_entity_registry(mock_entry)
                mock_er.return_value = mock_entity_reg

                mock_dev_reg = MagicMock()
                mock_dev_reg.async_get.return_value = mock_device
                mock_dr.return_value = mock_dev_reg

                result = await get_device_model(mock_self, "climate.test_trv")

                # model_id should take priority
                assert result == "TS0601"

    async def test_get_device_model_plain_string(self, mock_self):
        """Test model detection with plain string (no parentheses)."""
        from custom_components.better_thermostat.utils.helpers import get_device_model

        mock_entry = make_registry_entry("climate.test_trv", device_id="device_123")

        mock_device = MagicMock(spec=dr.DeviceEntry)
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
                mock_entity_reg = make_entity_registry(mock_entry)
                mock_er.return_value = mock_entity_reg

                mock_dev_reg = MagicMock()
                mock_dev_reg.async_get.return_value = mock_device
                mock_dr.return_value = mock_dev_reg

                result = await get_device_model(mock_self, "climate.test_trv")

                assert result == "TRVZB"
