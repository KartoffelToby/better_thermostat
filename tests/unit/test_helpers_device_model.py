"""Model detection through the device registry (helpers.get_device_model).

Priority is ``model_id`` > ``model`` before parentheses > ``model`` > the
configured model > ``"generic"``. The registry lookup is best effort: when it
cannot be performed the fallback chain still yields a name, and the reason is
recorded at debug level.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.utils.helpers import get_device_model

_HELPERS = "custom_components.better_thermostat.utils.helpers"


def _bt(model: str | None = None) -> MagicMock:
    """Build the caller surface get_device_model reads."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Test BT"
    bt.model = model
    return bt


def _registries(device: object | None):
    """Patch the entity and device registries to resolve to *device*."""
    entity_reg = MagicMock()
    entity_reg.async_get.return_value = SimpleNamespace(device_id="dev1")
    dev_reg = MagicMock()
    dev_reg.async_get.return_value = device
    return (
        patch(f"{_HELPERS}.er.async_get", return_value=entity_reg),
        patch(f"{_HELPERS}.dr.async_get", return_value=dev_reg),
    )


def _device(**kwargs) -> SimpleNamespace:
    """Build a device-registry entry with the fields the lookup reads."""
    fields = {
        "manufacturer": "Sonoff",
        "model": None,
        "model_id": None,
        "name": "Valve",
        "identifiers": {("mqtt", "0x1234")},
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
async def test_model_id_wins():
    """A device that reports model_id is identified by it."""
    er_patch, dr_patch = _registries(_device(model_id="TRVZB", model="TRV (Sonoff)"))
    with er_patch, dr_patch:
        assert await get_device_model(_bt(), "climate.trv") == "TRVZB"


@pytest.mark.asyncio
async def test_model_before_parentheses():
    """Without model_id the model up to the description is used."""
    er_patch, dr_patch = _registries(_device(model="TS0601 _TZE284 (Beok)"))
    with er_patch, dr_patch:
        assert await get_device_model(_bt(), "climate.trv") == "TS0601 _TZE284"


@pytest.mark.asyncio
async def test_unknown_device_falls_back_to_configured_model():
    """An unresolvable device falls back to the configured model."""
    er_patch, dr_patch = _registries(None)
    with er_patch, dr_patch:
        assert await get_device_model(_bt(model="TRVZB"), "climate.trv") == "TRVZB"


@pytest.mark.asyncio
async def test_registry_failure_is_traced_and_falls_back(caplog):
    """An unreachable registry yields "generic" and names the entity."""
    with (
        caplog.at_level(logging.DEBUG, logger=_HELPERS),
        patch(f"{_HELPERS}.er.async_get", side_effect=RuntimeError("no registry")),
    ):
        assert await get_device_model(_bt(), "climate.trv") == "generic"
    assert "device registry lookup for climate.trv failed" in caplog.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_successful_lookup_is_not_traced_as_a_failure(caplog):
    """A registry hit reports the device it found, not a failure."""
    er_patch, dr_patch = _registries(_device(model_id="TRVZB"))
    with caplog.at_level(logging.DEBUG, logger=_HELPERS), er_patch, dr_patch:
        assert await get_device_model(_bt(), "climate.trv") == "TRVZB"
    assert "failed" not in caplog.text
    assert "identifiers=[('mqtt', '0x1234')]" in caplog.text
