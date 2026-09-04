"""Device-registry binding helper.

async_bind_trv_device attaches the BT device to its single TRV via
``via_device_id``. The registry takes the TRV's device id for that and rejects
a device that names itself, which a TRV entity sitting on the BT device would
ask for.
"""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.device_binding import async_bind_trv_device
from custom_components.better_thermostat.utils.const import DOMAIN

_BINDING = "custom_components.better_thermostat.device_binding"
BT_UID = "bt_uid"
TRV_ID = "climate.trv"
BT_ENTRY_ID = "bt_entry_id"


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


def _non_real_device_registries(kind, *, device_id="trv_device_id"):
    """Build registries whose TRV device id names a device that is not a real one.

    ``kind`` is the inclusion flag the device answers under: a child device is
    returned only while ``include_child_devices`` is set, a composite id only
    while ``include_composite_devices`` is set. Switching that flag off yields
    nothing, which is what the registry does for such an id.
    """
    entity_entry = MagicMock()
    entity_entry.device_id = device_id
    er_reg = MagicMock()
    er_reg.async_get.return_value = entity_entry

    device = _device(device_id, {("mqtt", "0x1234")})

    def _lookup(_device_id, **flags):
        return None if flags.get(kind) is False else device

    dr_reg = MagicMock()
    dr_reg.async_get.side_effect = _lookup
    return er_reg, dr_reg


@pytest.mark.asyncio
async def test_bind_links_the_bt_device_by_the_trv_device_id():
    """The link carries the TRV's registry id, not one of its identifiers."""
    er_reg, dr_reg = _bind_registries(_device("trv_device_id", {("mqtt", "0x1234")}))

    result = await _bind(er_reg, dr_reg)

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
    er_reg, dr_reg = _bind_registries(
        _device("trv_device_id", set(), {("mac", "aa:bb:cc:dd:ee:ff")})
    )

    result = await _bind(er_reg, dr_reg)

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
    er_reg, dr_reg = _bind_registries(
        _device("bt_device_id", {(DOMAIN, BT_UID)}), device_id="bt_device_id"
    )

    result = await _bind(er_reg, dr_reg)

    assert result is False
    dr_reg.async_get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_bind_is_a_noop_without_a_trv_device_entry():
    """A TRV whose device the registry does not hold is skipped."""
    er_reg, dr_reg = _bind_registries(None)

    result = await _bind(er_reg, dr_reg)

    assert result is False
    dr_reg.async_get_or_create.assert_not_called()


@pytest.mark.parametrize("kind", ["include_child_devices", "include_composite_devices"])
@pytest.mark.asyncio
async def test_bind_skips_a_device_that_is_not_a_real_one(kind):
    """A child device and a composite id are both refused as link targets.

    Each answers the lookup only under its own inclusion flag, so a lookup
    that switches both off leaves the TRV in the branch of one whose device
    is not registered.
    """
    er_reg, dr_reg = _non_real_device_registries(kind)

    result = await _bind(er_reg, dr_reg)

    assert result is False
    dr_reg.async_get_or_create.assert_not_called()
