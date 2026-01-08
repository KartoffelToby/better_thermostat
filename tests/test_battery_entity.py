"""Tests for battery entity detection.

Issue #1794: When a group is used as a window sensor, BT was selecting
a random/wrong battery entity because groups have no device_id (None),
and the code would match any battery entity that also has device_id=None.

The fix:
1. For groups, resolve member entities and find their battery entities
2. Return the battery entity with the lowest battery level
3. For non-groups without device_id, return None
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def anyio_backend():
    """Configure anyio to use asyncio backend for async tests."""
    return "asyncio"


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.states = MagicMock()
    return hass


@pytest.fixture
def mock_bt_instance(mock_hass):
    """Create a mock BetterThermostat instance."""
    bt = MagicMock()
    bt.hass = mock_hass
    return bt


class TestFindBatteryEntity:
    """Tests for find_battery_entity function."""

    @pytest.mark.anyio
    async def test_returns_none_for_unknown_entity(self, mock_bt_instance):
        """Test that None is returned when entity is not in registry."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        mock_registry = MagicMock()
        mock_registry.entities.get.return_value = None

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=mock_registry,
        ):
            result = await find_battery_entity(
                mock_bt_instance, "binary_sensor.unknown"
            )
            assert result is None

    @pytest.mark.anyio
    async def test_returns_battery_for_physical_device(self, mock_bt_instance):
        """Test that battery entity is found for physical device."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        # Mock entity registry
        mock_window_entity = MagicMock()
        mock_window_entity.device_id = "device_123"

        mock_battery_entity = MagicMock()
        mock_battery_entity.device_id = "device_123"
        mock_battery_entity.device_class = "battery"
        mock_battery_entity.original_device_class = "battery"
        mock_battery_entity.entity_id = "sensor.window_battery"

        mock_registry = MagicMock()
        mock_registry.entities.get.return_value = mock_window_entity
        mock_registry.entities.values.return_value = [mock_battery_entity]

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=mock_registry,
        ):
            result = await find_battery_entity(mock_bt_instance, "binary_sensor.window")
            assert result == "sensor.window_battery"

    @pytest.mark.anyio
    async def test_returns_none_for_virtual_entity_without_group(
        self, mock_bt_instance
    ):
        """Test that None is returned for virtual entity that is not a group."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        # Virtual entity with no device_id
        mock_entity = MagicMock()
        mock_entity.device_id = None

        mock_registry = MagicMock()
        mock_registry.entities.get.return_value = mock_entity

        # State has no entity_id attribute (not a group)
        mock_state = MagicMock()
        mock_state.attributes = {}
        mock_bt_instance.hass.states.get.return_value = mock_state

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=mock_registry,
        ):
            result = await find_battery_entity(
                mock_bt_instance, "binary_sensor.virtual"
            )
            assert result is None

    @pytest.mark.anyio
    async def test_returns_lowest_battery_for_group(self, mock_bt_instance):
        """Test that lowest battery is returned for a group of sensors."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        # Group entity with no device_id
        mock_group_entity = MagicMock()
        mock_group_entity.device_id = None

        # Member entities with device_ids
        mock_member1_entity = MagicMock()
        mock_member1_entity.device_id = "device_1"

        mock_member2_entity = MagicMock()
        mock_member2_entity.device_id = "device_2"

        # Battery entities for members
        mock_battery1 = MagicMock()
        mock_battery1.device_id = "device_1"
        mock_battery1.device_class = "battery"
        mock_battery1.original_device_class = "battery"
        mock_battery1.entity_id = "sensor.window1_battery"

        mock_battery2 = MagicMock()
        mock_battery2.device_id = "device_2"
        mock_battery2.device_class = "battery"
        mock_battery2.original_device_class = "battery"
        mock_battery2.entity_id = "sensor.window2_battery"

        def mock_entities_get(entity_id):
            if entity_id == "binary_sensor.window_group":
                return mock_group_entity
            elif entity_id == "binary_sensor.window1":
                return mock_member1_entity
            elif entity_id == "binary_sensor.window2":
                return mock_member2_entity
            return None

        def mock_entities_values():
            return [mock_battery1, mock_battery2]

        mock_registry = MagicMock()
        mock_registry.entities.get.side_effect = mock_entities_get
        mock_registry.entities.values.return_value = mock_entities_values()

        # Group state with members
        mock_group_state = MagicMock()
        mock_group_state.attributes = {
            "entity_id": ["binary_sensor.window1", "binary_sensor.window2"]
        }

        # Battery states - window2 has lower battery
        mock_battery1_state = MagicMock()
        mock_battery1_state.state = "75"

        mock_battery2_state = MagicMock()
        mock_battery2_state.state = "25"  # Lower!

        def mock_states_get(entity_id):
            if entity_id == "binary_sensor.window_group":
                return mock_group_state
            elif entity_id == "sensor.window1_battery":
                return mock_battery1_state
            elif entity_id == "sensor.window2_battery":
                return mock_battery2_state
            return None

        mock_bt_instance.hass.states.get.side_effect = mock_states_get

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=mock_registry,
        ):
            result = await find_battery_entity(
                mock_bt_instance, "binary_sensor.window_group"
            )
            # Should return the battery with the lowest level (25%)
            assert result == "sensor.window2_battery"

    @pytest.mark.anyio
    async def test_group_with_no_batteries_returns_none(self, mock_bt_instance):
        """Test that None is returned for group where no member has battery."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        # Group entity with no device_id
        mock_group_entity = MagicMock()
        mock_group_entity.device_id = None

        # Member entity with no battery
        mock_member_entity = MagicMock()
        mock_member_entity.device_id = "device_1"

        def mock_entities_get(entity_id):
            if entity_id == "binary_sensor.window_group":
                return mock_group_entity
            elif entity_id == "binary_sensor.window1":
                return mock_member_entity
            return None

        mock_registry = MagicMock()
        mock_registry.entities.get.side_effect = mock_entities_get
        mock_registry.entities.values.return_value = []  # No battery entities

        # Group state with members
        mock_group_state = MagicMock()
        mock_group_state.attributes = {"entity_id": ["binary_sensor.window1"]}

        mock_bt_instance.hass.states.get.return_value = mock_group_state

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=mock_registry,
        ):
            result = await find_battery_entity(
                mock_bt_instance, "binary_sensor.window_group"
            )
            assert result is None
