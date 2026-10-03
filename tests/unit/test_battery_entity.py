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

from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.states = MagicMock()
    return hass


@pytest.fixture
def mock_bt_instance(mock_hass):
    """Create a mock BetterThermostat instance."""
    bt = ThermostatStandIn()
    bt.hass = mock_hass
    return bt


class TestFindBatteryEntity:
    """Tests for find_battery_entity function."""

    async def test_returns_none_for_unknown_entity(self, mock_bt_instance):
        """Test that None is returned when entity is not in registry."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        mock_registry = make_entity_registry()

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=mock_registry,
        ):
            result = await find_battery_entity(
                mock_bt_instance, "binary_sensor.unknown"
            )
            assert result is None

    async def test_returns_battery_for_physical_device(self, mock_bt_instance):
        """Test that battery entity is found for physical device."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        # Entity registry
        mock_window_entity = make_registry_entry(
            "binary_sensor.window", device_id="device_123"
        )

        mock_battery_entity = make_registry_entry(
            "sensor.window_battery",
            device_id="device_123",
            device_class="battery",
            original_device_class="battery",
        )

        mock_registry = make_entity_registry(mock_window_entity, mock_battery_entity)

        with patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=mock_registry,
        ):
            result = await find_battery_entity(mock_bt_instance, "binary_sensor.window")
            assert result == "sensor.window_battery"

    async def test_returns_none_for_virtual_entity_without_group(
        self, mock_bt_instance
    ):
        """Test that None is returned for virtual entity that is not a group."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        # Virtual entity with no device_id
        mock_entity = make_registry_entry("binary_sensor.virtual", device_id=None)

        mock_registry = make_entity_registry(mock_entity)

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

    async def test_returns_lowest_battery_for_group(self, mock_bt_instance):
        """Test that lowest battery is returned for a group of sensors."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        # Group entity with no device_id
        mock_group_entity = make_registry_entry(
            "binary_sensor.window_group", platform="group", device_id=None
        )

        # Member entities with device_ids
        mock_member1_entity = make_registry_entry(
            "binary_sensor.window1", device_id="device_1"
        )

        mock_member2_entity = make_registry_entry(
            "binary_sensor.window2", device_id="device_2"
        )

        # Battery entities for members
        mock_battery1 = make_registry_entry(
            "sensor.window1_battery",
            device_id="device_1",
            device_class="battery",
            original_device_class="battery",
        )

        mock_battery2 = make_registry_entry(
            "sensor.window2_battery",
            device_id="device_2",
            device_class="battery",
            original_device_class="battery",
        )

        mock_registry = make_entity_registry(
            mock_group_entity,
            mock_member1_entity,
            mock_member2_entity,
            mock_battery1,
            mock_battery2,
        )

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

    async def test_group_with_no_batteries_returns_none(self, mock_bt_instance):
        """Test that None is returned for group where no member has battery."""
        from custom_components.better_thermostat.utils.helpers import (
            find_battery_entity,
        )

        # Group entity with no device_id
        mock_group_entity = make_registry_entry(
            "binary_sensor.window_group", platform="group", device_id=None
        )

        # Member entity with no battery
        mock_member_entity = make_registry_entry(
            "binary_sensor.window1", device_id="device_1"
        )

        # No battery entities
        mock_registry = make_entity_registry(mock_group_entity, mock_member_entity)

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
