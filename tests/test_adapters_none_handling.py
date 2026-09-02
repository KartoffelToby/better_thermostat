"""Tests for adapter None handling.

Tests that adapters properly handle None states when entities are unavailable.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.trv import Trv


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
    bt.device_name = "Test Thermostat"
    bt.real_trvs = {
        "climate.test_trv": Trv(
            entity_id="climate.test_trv",
            local_temperature_calibration_entity="number.test_calibration",
        )
    }
    return bt


def _state_lookup_like_home_assistant(states):
    """Look a state up the way Home Assistant's state machine does.

    Parameters
    ----------
    states : dict
        States to serve, keyed by entity ID.

    Returns
    -------
    callable
        Lookup that falls back to the lowercased entity ID, and therefore
        raises ``AttributeError`` for ``None`` just as the state machine does.
    """

    def get(entity_id):
        return states.get(entity_id) or states.get(entity_id.lower())

    return get


@pytest.fixture
def mock_bt_instance_without_calibration_entity(mock_hass):
    """Create a mock BetterThermostat whose TRV exposes no calibration entity.

    Its state lookup mirrors Home Assistant's, so asking for the ``None``
    entity ID raises instead of quietly answering ``None``.
    """
    bt = MagicMock()
    bt.hass = mock_hass
    bt.device_name = "Test Thermostat"
    bt.real_trvs = {
        "climate.test_trv": Trv(
            entity_id="climate.test_trv", local_temperature_calibration_entity=None
        )
    }
    bt.hass.states.get = MagicMock(side_effect=_state_lookup_like_home_assistant({}))
    return bt


class TestDeconzAdapter:
    """Tests for deCONZ adapter None handling."""

    async def test_get_info_returns_false_when_state_is_none(self, mock_bt_instance):
        """Test that get_info returns support_offset=False when state is None."""
        from custom_components.better_thermostat.adapters.deconz import get_info

        mock_bt_instance.hass.states.get.return_value = None

        result = await get_info(mock_bt_instance, "climate.missing_entity")

        assert result == {"support_offset": False, "support_valve": False}

    async def test_get_info_returns_true_when_offset_exists(self, mock_bt_instance):
        """Test that get_info returns support_offset=True when offset attribute exists."""
        from custom_components.better_thermostat.adapters.deconz import get_info

        mock_state = MagicMock()
        mock_state.attributes = {"offset": 0.0}
        mock_bt_instance.hass.states.get.return_value = mock_state

        result = await get_info(mock_bt_instance, "climate.test_trv")

        assert result == {"support_offset": True, "support_valve": False}


class TestMqttAdapter:
    """Tests for MQTT adapter None handling."""

    async def test_get_offset_step_returns_default_when_state_is_none(
        self, mock_bt_instance
    ):
        """Test that get_offset_step returns 1.0 when state is None."""
        from custom_components.better_thermostat.adapters.mqtt import get_offset_step

        mock_bt_instance.hass.states.get.return_value = None

        result = await get_offset_step(mock_bt_instance, "climate.test_trv")

        assert result == 1.0

    async def test_get_min_offset_returns_default_when_state_is_none(
        self, mock_bt_instance
    ):
        """Test that get_min_offset returns -10.0 when state is None."""
        from custom_components.better_thermostat.adapters.mqtt import get_min_offset

        mock_bt_instance.hass.states.get.return_value = None

        result = await get_min_offset(mock_bt_instance, "climate.test_trv")

        assert result == -10.0

    async def test_get_max_offset_returns_default_when_state_is_none(
        self, mock_bt_instance
    ):
        """Test that get_max_offset returns 10.0 when state is None."""
        from custom_components.better_thermostat.adapters.mqtt import get_max_offset

        mock_bt_instance.hass.states.get.return_value = None

        result = await get_max_offset(mock_bt_instance, "climate.test_trv")

        assert result == 10.0

    async def test_get_offset_step_returns_attribute_when_state_exists(
        self, mock_bt_instance
    ):
        """Test that get_offset_step returns attribute value when state exists."""
        from custom_components.better_thermostat.adapters.mqtt import get_offset_step

        mock_state = MagicMock()
        mock_state.attributes = {"step": 0.5}
        mock_bt_instance.hass.states.get.return_value = mock_state

        result = await get_offset_step(mock_bt_instance, "climate.test_trv")

        assert result == 0.5

    async def test_get_offset_step_returns_default_without_calibration_entity(
        self, mock_bt_instance_without_calibration_entity
    ):
        """A TRV without a calibration entity reads the default step."""
        from custom_components.better_thermostat.adapters.mqtt import get_offset_step

        result = await get_offset_step(
            mock_bt_instance_without_calibration_entity, "climate.test_trv"
        )

        assert result == 1.0
        mock_bt_instance_without_calibration_entity.hass.states.get.assert_not_called()

    async def test_get_min_offset_returns_default_without_calibration_entity(
        self, mock_bt_instance_without_calibration_entity
    ):
        """A TRV without a calibration entity reads the default lower bound."""
        from custom_components.better_thermostat.adapters.mqtt import get_min_offset

        result = await get_min_offset(
            mock_bt_instance_without_calibration_entity, "climate.test_trv"
        )

        assert result == -10.0
        mock_bt_instance_without_calibration_entity.hass.states.get.assert_not_called()

    async def test_get_max_offset_returns_default_without_calibration_entity(
        self, mock_bt_instance_without_calibration_entity
    ):
        """A TRV without a calibration entity reads the default upper bound."""
        from custom_components.better_thermostat.adapters.mqtt import get_max_offset

        result = await get_max_offset(
            mock_bt_instance_without_calibration_entity, "climate.test_trv"
        )

        assert result == 10.0
        mock_bt_instance_without_calibration_entity.hass.states.get.assert_not_called()


class TestGenericAdapter:
    """Tests for generic adapter None handling."""

    async def test_get_offset_step_returns_none_when_state_is_none(
        self, mock_bt_instance
    ):
        """Test that get_offset_step returns None when state is None."""
        from custom_components.better_thermostat.adapters.generic import get_offset_step

        mock_bt_instance.hass.states.get.return_value = None

        result = await get_offset_step(mock_bt_instance, "climate.test_trv")

        assert result is None

    async def test_get_min_offset_returns_default_when_state_is_none(
        self, mock_bt_instance
    ):
        """Test that get_min_offset returns -6.0 when state is None."""
        from custom_components.better_thermostat.adapters.generic import get_min_offset

        mock_bt_instance.hass.states.get.return_value = None

        result = await get_min_offset(mock_bt_instance, "climate.test_trv")

        assert result == -6.0

    async def test_get_max_offset_returns_default_when_state_is_none(
        self, mock_bt_instance
    ):
        """Test that get_max_offset returns 6.0 when state is None."""
        from custom_components.better_thermostat.adapters.generic import get_max_offset

        mock_bt_instance.hass.states.get.return_value = None

        result = await get_max_offset(mock_bt_instance, "climate.test_trv")

        assert result == 6.0

    async def test_get_offset_step_returns_none_when_no_calibration_entity(
        self, mock_bt_instance
    ):
        """Test that get_offset_step returns None when no calibration entity configured."""
        from custom_components.better_thermostat.adapters.generic import get_offset_step

        mock_bt_instance.real_trvs = {
            "climate.test_trv": Trv(
                entity_id="climate.test_trv", local_temperature_calibration_entity=None
            )
        }

        result = await get_offset_step(mock_bt_instance, "climate.test_trv")

        assert result is None
