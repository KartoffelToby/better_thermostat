import pytest
from unittest.mock import MagicMock, patch
from custom_components.better_thermostat.utils.helpers import find_valve_entity


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_find_valve_entity_ignores_sensor_pi_heating_demand():
    """Test that find_valve_entity ignores sensor.pi_heating_demand but accepts number.pi_heating_demand."""

    # Mock hass
    hass = MagicMock()
    bt_instance = MagicMock()
    bt_instance.hass = hass

    # Mock the target TRV entity
    trv_entity_id = "climate.my_trv"
    trv_config_entry_id = "config_entry_123"
    trv_device_id = "device_123"

    reg_entity_trv = MagicMock()
    reg_entity_trv.config_entry_id = trv_config_entry_id
    reg_entity_trv.device_id = trv_device_id

    # Define candidate entities
    def make_entity(eid, uid):
        e = MagicMock()
        e.entity_id = eid
        e.unique_id = uid
        e.device_id = trv_device_id
        return e

    entity_sensor = make_entity("sensor.pi_heating_demand", "unique_sensor")
    entity_number = make_entity("number.pi_heating_demand", "unique_number")
    entity_input = make_entity("input_number.pi_heating_demand", "unique_input")

    # Patch the dependencies
    # We patch 'er.async_get' where 'er' is the imported module in helpers.py
    with (
        patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get"
        ) as mock_er_get,
        patch(
            "custom_components.better_thermostat.utils.helpers.async_entries_for_config_entry"
        ) as mock_entries,
    ):

        mock_registry = MagicMock()
        mock_er_get.return_value = mock_registry
        mock_registry.async_get.return_value = reg_entity_trv

        # Case 1: Only sensor available -> Should return as read-only candidate
        mock_entries.return_value = [entity_sensor]
        result = await find_valve_entity(bt_instance, trv_entity_id)

        assert result is not None
        assert result["entity_id"] == "sensor.pi_heating_demand"
        assert result["writable"] is False
        assert result["reason"] == "pi_heating_demand"

        # Case 2: Number available -> Should return as writable
        mock_entries.return_value = [entity_number]
        result = await find_valve_entity(bt_instance, trv_entity_id)

        assert result is not None
        assert result["entity_id"] == "number.pi_heating_demand"
        assert result["writable"] is True
        assert result["reason"] == "pi_heating_demand"

        # Case 3: Input Number available -> Should return as writable
        mock_entries.return_value = [entity_input]
        result = await find_valve_entity(bt_instance, trv_entity_id)

        assert result is not None
        assert result["entity_id"] == "input_number.pi_heating_demand"
        assert result["writable"] is True

        # Case 4: Mixed (Sensor and Number) -> Should prefer writable
        # Note: The order in list matters if the code iterates and returns immediately on writable.
        # If sensor comes first, it sets readonly_candidate. Then number comes, it returns immediately.
        mock_entries.return_value = [entity_sensor, entity_number]
        result = await find_valve_entity(bt_instance, trv_entity_id)

        assert result is not None
        assert result["entity_id"] == "number.pi_heating_demand"
        assert result["writable"] is True
