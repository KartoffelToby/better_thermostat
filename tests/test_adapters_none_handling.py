"""Tests for adapter None handling.

Tests that adapters properly handle None states when entities are unavailable.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.adapters import generic, mqtt, zwave_js
from custom_components.better_thermostat.trv import Trv

# Adapters whose offset bounds come from an entity discovery found.
ENTITY_ADAPTERS = (generic, mqtt, zwave_js)


def _adapter_id(adapter):
    """Name an adapter module by its ecosystem, for readable test ids."""
    return adapter.__name__.rsplit(".", 1)[-1]


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


class TestBoundsOfAnEntityThatDeclaresNone:
    """Every entity-backed adapter answers one undeclared default.

    A TRV whose calibration entity was never discovered and one whose
    entity has not reported a state leave the bounds equally undeclared.
    The ecosystem the TRV happens to belong to does not change what that
    means, so the answer may not depend on which adapter was asked: the
    shell records these numbers once at startup and clamps every later
    offset write against them.
    """

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS, ids=_adapter_id)
    async def test_step_of_a_stateless_entity(self, adapter, mock_bt_instance):
        """An entity that reports nothing publishes no granularity."""
        mock_bt_instance.hass.states.get.return_value = None

        result = await adapter.get_offset_step(mock_bt_instance, "climate.test_trv")

        assert result == 1.0

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS, ids=_adapter_id)
    async def test_min_of_a_stateless_entity(self, adapter, mock_bt_instance):
        """An entity that reports nothing publishes no lower bound."""
        mock_bt_instance.hass.states.get.return_value = None

        result = await adapter.get_min_offset(mock_bt_instance, "climate.test_trv")

        assert result == -10.0

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS, ids=_adapter_id)
    async def test_max_of_a_stateless_entity(self, adapter, mock_bt_instance):
        """An entity that reports nothing publishes no upper bound."""
        mock_bt_instance.hass.states.get.return_value = None

        result = await adapter.get_max_offset(mock_bt_instance, "climate.test_trv")

        assert result == 10.0

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS, ids=_adapter_id)
    async def test_step_without_a_calibration_entity(self, adapter, mock_bt_instance):
        """A TRV discovery found no entity for gets the same answer."""
        mock_bt_instance.real_trvs = {
            "climate.test_trv": Trv(
                entity_id="climate.test_trv", local_temperature_calibration_entity=None
            )
        }

        result = await adapter.get_offset_step(mock_bt_instance, "climate.test_trv")

        assert result == 1.0

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS, ids=_adapter_id)
    async def test_step_comes_from_the_entity_when_it_publishes_one(
        self, adapter, mock_bt_instance
    ):
        """A published step is what the adapter reports, not the default."""
        mock_state = MagicMock()
        mock_state.domain = "number"
        mock_state.attributes = {"step": 0.5}
        mock_bt_instance.hass.states.get.return_value = mock_state

        result = await adapter.get_offset_step(mock_bt_instance, "climate.test_trv")

        assert result == 0.5
