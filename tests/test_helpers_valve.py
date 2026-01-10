from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.utils.helpers import find_valve_entity


@pytest.fixture
def anyio_backend():
    """Return the async backend to use for tests."""
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
            "custom_components.better_thermostat.utils.helpers.dr.async_get"
        ) as mock_dr_get,
        patch(
            "custom_components.better_thermostat.utils.helpers.async_entries_for_config_entry"
        ) as mock_entries,
    ):
        mock_registry = MagicMock()
        mock_er_get.return_value = mock_registry
        mock_registry.async_get.return_value = reg_entity_trv

        # Minimal device registry; device_id matches in these cases.
        mock_dev_reg = MagicMock()
        mock_dr_get.return_value = mock_dev_reg
        dev = MagicMock()
        dev.identifiers = {("test", "device_123")}
        mock_dev_reg.async_get.return_value = dev

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


@pytest.mark.anyio
async def test_find_valve_entity_trvzb_valve_opening_degree_device_mismatch():
    """TRVZB-style valve entities may be registered under a different device_id.

    The helper should still find them by matching device identifiers.
    """

    hass = MagicMock()
    bt_instance = MagicMock()
    bt_instance.hass = hass

    trv_entity_id = "climate.my_trv"
    trv_config_entry_id = "config_entry_123"
    trv_device_id = "device_trv"
    valve_device_id = "device_valve"

    reg_entity_trv = MagicMock()
    reg_entity_trv.config_entry_id = trv_config_entry_id
    reg_entity_trv.device_id = trv_device_id

    # Candidate number entity under different device_id
    ent = MagicMock()
    ent.entity_id = "number.my_trv_valve_opening_degree"
    ent.unique_id = "0x00124b0000abcd_valve_opening_degree"
    ent.original_name = "Valve Opening Degree"
    ent.device_id = valve_device_id

    with (
        patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get"
        ) as mock_er_get,
        patch(
            "custom_components.better_thermostat.utils.helpers.dr.async_get"
        ) as mock_dr_get,
        patch(
            "custom_components.better_thermostat.utils.helpers.async_entries_for_config_entry"
        ) as mock_entries,
    ):
        mock_registry = MagicMock()
        mock_er_get.return_value = mock_registry
        mock_registry.async_get.return_value = reg_entity_trv
        mock_entries.return_value = [ent]

        # Device registry returns devices with shared identifiers
        mock_dev_reg = MagicMock()
        mock_dr_get.return_value = mock_dev_reg
        trv_dev = MagicMock()
        valve_dev = MagicMock()
        shared = {("z2m", "0x00124b0000abcd")}
        trv_dev.identifiers = shared
        valve_dev.identifiers = shared

        def _get_dev(dev_id):
            return trv_dev if dev_id == trv_device_id else valve_dev

        mock_dev_reg.async_get.side_effect = _get_dev

        result = await find_valve_entity(bt_instance, trv_entity_id)

        assert result is not None
        assert result["entity_id"] == "number.my_trv_valve_opening_degree"
        assert result["writable"] is True
        assert result["reason"] == "valve_opening_degree"


@pytest.mark.anyio
async def test_find_valve_entity_translation_key_detection():
    """Test that find_valve_entity uses translation_key for language-agnostic detection.

    This ensures valve entities are detected regardless of the HA UI language.
    """

    hass = MagicMock()
    bt_instance = MagicMock()
    bt_instance.hass = hass

    trv_entity_id = "climate.my_trv"
    trv_config_entry_id = "config_entry_123"
    trv_device_id = "device_123"

    reg_entity_trv = MagicMock()
    reg_entity_trv.config_entry_id = trv_config_entry_id
    reg_entity_trv.device_id = trv_device_id

    # Create entity with German localized name but English translation_key
    def make_entity_with_translation_key(eid, uid, name, translation_key):
        e = MagicMock()
        e.entity_id = eid
        e.unique_id = uid
        e.device_id = trv_device_id
        e.original_name = name
        e.translation_key = translation_key
        return e

    # German localized name but with translation_key for language-agnostic detection
    entity_german = make_entity_with_translation_key(
        "number.mein_trv_ventiloffnungswinkel",
        "0x00124b0000abcd_valve",
        "Ventilöffnungswinkel",  # German name
        "valve_opening_degree",  # English translation key
    )

    with (
        patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get"
        ) as mock_er_get,
        patch(
            "custom_components.better_thermostat.utils.helpers.dr.async_get"
        ) as mock_dr_get,
        patch(
            "custom_components.better_thermostat.utils.helpers.async_entries_for_config_entry"
        ) as mock_entries,
    ):
        mock_registry = MagicMock()
        mock_er_get.return_value = mock_registry
        mock_registry.async_get.return_value = reg_entity_trv

        mock_dev_reg = MagicMock()
        mock_dr_get.return_value = mock_dev_reg
        dev = MagicMock()
        dev.identifiers = {("test", "device_123")}
        mock_dev_reg.async_get.return_value = dev

        # Test detection via translation_key
        mock_entries.return_value = [entity_german]
        result = await find_valve_entity(bt_instance, trv_entity_id)

        assert result is not None
        assert result["entity_id"] == "number.mein_trv_ventiloffnungswinkel"
        assert result["writable"] is True
        assert result["reason"] == "valve_opening_degree"
        assert result["detection_method"] == "translation_key"
