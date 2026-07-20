"""Device-registry binding helpers.

async_bind_trv_device attaches the BT device to its single TRV via
``via_device``; async_unbind_trv_device clears a stale ``via_device_id``
left behind on the BT device by an earlier single-valve binding pass, which
matters for multi-TRV setups where no binding is written anymore.
"""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.device_binding import (
    async_unbind_trv_device,
)
from custom_components.better_thermostat.utils.const import DOMAIN

_BINDING = "custom_components.better_thermostat.device_binding"
BT_UID = "bt_uid"


def _registry_with(bt_device):
    """Build a device-registry mock returning the given BT device entry."""
    registry = MagicMock()
    registry.async_get_device.return_value = bt_device
    return registry


@pytest.mark.asyncio
async def test_unbind_clears_a_stale_via_device_link():
    """A set via_device_id is cleared with an explicit None update."""
    bt_device = MagicMock()
    bt_device.id = "bt_device_id"
    bt_device.via_device_id = "stale_trv_device_id"
    registry = _registry_with(bt_device)

    with patch(f"{_BINDING}.dr.async_get", return_value=registry):
        result = await async_unbind_trv_device(MagicMock(), BT_UID)

    assert result is True
    registry.async_get_device.assert_called_once_with(
        identifiers={(DOMAIN, BT_UID)}
    )
    registry.async_update_device.assert_called_once_with(
        "bt_device_id", via_device_id=None
    )


@pytest.mark.asyncio
async def test_unbind_is_a_noop_without_a_via_device_link():
    """A BT device without via_device_id is left untouched."""
    bt_device = MagicMock()
    bt_device.via_device_id = None
    registry = _registry_with(bt_device)

    with patch(f"{_BINDING}.dr.async_get", return_value=registry):
        result = await async_unbind_trv_device(MagicMock(), BT_UID)

    assert result is False
    registry.async_update_device.assert_not_called()


@pytest.mark.asyncio
async def test_unbind_is_a_noop_without_a_registry_entry():
    """A missing BT device registry entry is tolerated."""
    registry = _registry_with(None)

    with patch(f"{_BINDING}.dr.async_get", return_value=registry):
        result = await async_unbind_trv_device(MagicMock(), BT_UID)

    assert result is False
    registry.async_update_device.assert_not_called()
