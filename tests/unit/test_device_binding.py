"""Device-registry binding helpers.

async_bind_trv_device attaches the BT device to its single TRV via
``via_device_id``; async_unbind_trv_device clears a stale ``via_device_id``
left behind on the BT device by an earlier single-valve binding pass, which
matters for multi-TRV setups where no binding is written anymore.
"""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.device_binding import (
    async_bind_trv_device,
    async_unbind_trv_device,
)
from custom_components.better_thermostat.utils.const import DOMAIN

_BINDING = "custom_components.better_thermostat.device_binding"
BT_UID = "bt_uid"
TRV_ID = "climate.trv"
BT_ENTRY_ID = "bt_entry_id"


def _registry_with(bt_device):
    """Build a device-registry mock returning the given BT device entry."""
    registry = MagicMock()
    registry.async_get_device.return_value = bt_device
    return registry


def _bind_registries(trv_device, *, device_id="trv_device_id"):
    """Build the entity and device registries the bind path reads.

    ``identifiers`` is a real set on every device entry: the membership test
    that keeps a BT device off its own via link reads it, and a MagicMock
    would answer that test False whatever the entry holds.
    """
    entity_entry = MagicMock()
    entity_entry.device_id = device_id
    er_reg = MagicMock()
    er_reg.async_get.return_value = entity_entry

    dr_reg = MagicMock()
    dr_reg.async_get.return_value = trv_device
    return er_reg, dr_reg


def _device(device_id, identifiers, connections=frozenset()):
    """Build a device registry entry carrying real identifier and connection sets.

    Both are read as sets rather than left as mocks: the membership test that
    keeps a BT device off its own via link reads ``identifiers``, and a device
    registered by connections alone is only that if it carries one.
    """
    device = MagicMock()
    device.id = device_id
    device.identifiers = identifiers
    device.connections = connections
    return device


async def _bind(er_reg, dr_reg):
    """Run the binding against the registries the test assembled."""
    with (
        patch(f"{_BINDING}.er.async_get", return_value=er_reg),
        patch(f"{_BINDING}.dr.async_get", return_value=dr_reg),
    ):
        return await async_bind_trv_device(MagicMock(), BT_UID, TRV_ID, BT_ENTRY_ID)


@pytest.mark.asyncio
async def test_bind_links_the_bt_device_by_the_trv_device_id():
    """The link carries the TRV's registry id, not its identifiers."""
    trv_device = _device("trv_device_id", {("mqtt", "0x1234")})
    er_reg, dr_reg = _bind_registries(trv_device)

    with (
        patch(f"{_BINDING}.er.async_get", return_value=er_reg),
        patch(f"{_BINDING}.dr.async_get", return_value=dr_reg),
    ):
        result = await async_bind_trv_device(MagicMock(), BT_UID, TRV_ID, BT_ENTRY_ID)

    assert result is True
    dr_reg.async_get_or_create.assert_called_once_with(
        config_entry_id=BT_ENTRY_ID,
        identifiers={(DOMAIN, BT_UID)},
        via_device_id="trv_device_id",
    )


@pytest.mark.asyncio
async def test_bind_links_a_trv_device_that_carries_no_identifiers():
    """A device registered by connections alone still gets the link.

    The id the link is written with exists for every registry entry, so a
    device whose integration registered it without identifiers is bindable.
    """
    trv_device = _device("trv_device_id", set(), {("mac", "aa:bb:cc:dd:ee:ff")})
    er_reg, dr_reg = _bind_registries(trv_device)

    with (
        patch(f"{_BINDING}.er.async_get", return_value=er_reg),
        patch(f"{_BINDING}.dr.async_get", return_value=dr_reg),
    ):
        result = await async_bind_trv_device(MagicMock(), BT_UID, TRV_ID, BT_ENTRY_ID)

    assert result is True
    assert dr_reg.async_get_or_create.call_args.kwargs["via_device_id"] == (
        "trv_device_id"
    )


@pytest.mark.asyncio
async def test_bind_skips_a_trv_sitting_on_the_bt_device_itself():
    """A TRV on this BT device would ask for a self-referencing link.

    The registry rejects that with an error rather than ignoring it, so the
    binding has to stand down before the write.
    """
    trv_device = _device("bt_device_id", {(DOMAIN, BT_UID)})
    er_reg, dr_reg = _bind_registries(trv_device, device_id="bt_device_id")

    with (
        patch(f"{_BINDING}.er.async_get", return_value=er_reg),
        patch(f"{_BINDING}.dr.async_get", return_value=dr_reg),
    ):
        result = await async_bind_trv_device(MagicMock(), BT_UID, TRV_ID, BT_ENTRY_ID)

    assert result is False
    dr_reg.async_get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_bind_is_a_noop_without_a_trv_device_entry():
    """A TRV whose device the registry does not hold is skipped."""
    er_reg, dr_reg = _bind_registries(None)

    with (
        patch(f"{_BINDING}.er.async_get", return_value=er_reg),
        patch(f"{_BINDING}.dr.async_get", return_value=dr_reg),
    ):
        result = await async_bind_trv_device(MagicMock(), BT_UID, TRV_ID, BT_ENTRY_ID)

    assert result is False
    dr_reg.async_get_or_create.assert_not_called()


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
    registry.async_get_device.assert_called_once_with(identifiers={(DOMAIN, BT_UID)})
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


@pytest.mark.asyncio
async def test_bind_resolves_the_trv_device_as_a_real_device():
    """Only a real device is offered as the link target.

    The registry answers a child device named as a via device with an error,
    and a pre-migration composite id with a deprecation on the same removal
    deadline the link is written to clear, so a TRV on either takes the same
    branch as a TRV whose device is not registered.
    """
    er_reg, dr_reg = _bind_registries(None)

    result = await _bind(er_reg, dr_reg)

    assert result is False
    assert dr_reg.async_get.call_args.kwargs["include_child_devices"] is False
    assert dr_reg.async_get.call_args.kwargs["include_composite_devices"] is False
    dr_reg.async_get_or_create.assert_not_called()
