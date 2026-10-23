"""A group thermostat clears a via device link an earlier single valve left.

The via device link is single-valued, so a thermostat driving several heads
carries none. A link written while the entry had one head stays in the device
registry until the thermostat clears it at startup. The lookup goes through
the registry API that is unambiguous per config entry; the older lookup by
identifiers alone reports a deprecation into the log.
"""

import logging

from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import DOMAIN, make_entry, set_room_sensor, wait_for_startup
from .device_profiles import GROUP_OF_THREE


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True)
async def test_a_group_clears_a_stale_via_device_link(hass, trv_group, caplog):
    """The startup drops the link without a deprecated registry lookup."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(trv_group.scenario)
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    # The valve the entry once drove alone, on a device of its own integration.
    valve_entry = MockConfigEntry(domain="former_valve")
    valve_entry.add_to_hass(hass)
    former_valve = dev_reg.async_get_or_create(
        config_entry_id=valve_entry.entry_id, identifiers={("former_valve", "1")}
    )
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        via_device_id=former_valve.id,
    )

    caplog.set_level(logging.WARNING)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    bt_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert bt_device is not None
    assert bt_device.via_device_id is None
    assert "device_registry.async_get_device" not in caplog.text
