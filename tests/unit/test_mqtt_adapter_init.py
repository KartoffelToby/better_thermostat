"""Early valve discovery in the MQTT adapter's init.

Zigbee2MQTT exposes the valve position as a separate number entity, which the
adapter looks up once at startup. The lookup walks the entity and device
registries, so it is best effort: a lookup that fails leaves the TRV without a
valve entity and records why, and startup continues.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.adapters.mqtt import init
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.test_trv"
_MQTT_LOGGER = "custom_components.better_thermostat.adapters.mqtt"
_FIND_VALVE = "custom_components.better_thermostat.adapters.mqtt.find_valve_entity"


def _bt() -> MagicMock:
    """Build a BetterThermostat stand-in whose calibration needs no lookup."""
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID, calibration=1)}
    return bt


@pytest.mark.asyncio
async def test_writable_valve_entity_is_adopted():
    """A writable valve entity is stored together with its writability."""
    bt = _bt()
    valve = {"entity_id": "number.valve_position", "writable": True}
    with patch(_FIND_VALVE, AsyncMock(return_value=valve)):
        await init(bt, ENTITY_ID)
    assert bt.real_trvs[ENTITY_ID].valve_position_entity == "number.valve_position"
    assert bt.real_trvs[ENTITY_ID].valve_position_writable is True


@pytest.mark.asyncio
async def test_read_only_valve_entity_is_marked_unwritable():
    """A valve entity without write support is stored as read-only."""
    bt = _bt()
    valve = {"entity_id": "sensor.valve_position", "writable": False}
    with patch(_FIND_VALVE, AsyncMock(return_value=valve)):
        await init(bt, ENTITY_ID)
    assert bt.real_trvs[ENTITY_ID].valve_position_writable is False


@pytest.mark.asyncio
async def test_no_valve_entity_leaves_the_trv_untouched():
    """Without a discovered entity the TRV keeps its defaults."""
    bt = _bt()
    with patch(_FIND_VALVE, AsyncMock(return_value=None)):
        await init(bt, ENTITY_ID)
    assert bt.real_trvs[ENTITY_ID].valve_position_entity is None


@pytest.mark.asyncio
async def test_failed_discovery_is_traced_and_init_continues(caplog):
    """A discovery failure names the TRV and leaves the entity unset."""
    bt = _bt()
    with (
        caplog.at_level(logging.DEBUG, logger=_MQTT_LOGGER),
        patch(_FIND_VALVE, AsyncMock(side_effect=RuntimeError("no registry"))),
    ):
        await init(bt, ENTITY_ID)
    assert bt.real_trvs[ENTITY_ID].valve_position_entity is None
    assert f"valve entity discovery for {ENTITY_ID} failed" in caplog.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_successful_discovery_is_not_traced_as_a_failure(caplog):
    """A discovery that lands reports nothing."""
    bt = _bt()
    valve = {"entity_id": "number.valve_position", "writable": True}
    with (
        caplog.at_level(logging.DEBUG, logger=_MQTT_LOGGER),
        patch(_FIND_VALVE, AsyncMock(return_value=valve)),
    ):
        await init(bt, ENTITY_ID)
    assert "failed" not in caplog.text
