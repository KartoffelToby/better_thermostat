"""The device entry BetterThermostat publishes for its own instance.

``device_info`` names the TRV device the BT instance hangs under, so the HA
device page renders "via: <TRV name>". The registry takes the TRV's device id
for that and rejects a device that names itself, which a TRV entity sitting on
the BT device would ask for.
"""

from unittest.mock import MagicMock, patch

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.utils.const import DOMAIN

_CLIMATE = "custom_components.better_thermostat.climate"
BT_UID = "bt_uid"


def _bt(all_trvs):
    """Build the minimum a device_info read touches."""
    bt = MagicMock()
    bt.unique_id = BT_UID
    bt.device_name = "Test BT"
    bt.model = "TRVZB"
    bt.all_trvs = all_trvs
    bt.hass = MagicMock()
    return bt


def _registries(trv_device, *, device_id="trv_device_id"):
    """Build the entity and device registries device_info reads."""
    entity_entry = MagicMock()
    entity_entry.device_id = device_id
    er_reg = MagicMock()
    er_reg.async_get.return_value = entity_entry

    dr_reg = MagicMock()
    dr_reg.async_get.return_value = trv_device
    return er_reg, dr_reg


def _device(device_id, identifiers):
    """Build a device registry entry carrying a real identifier set."""
    device = MagicMock()
    device.id = device_id
    device.identifiers = identifiers
    return device


def _read_device_info(bt, er_reg, dr_reg):
    """Read the property off an instance the test assembled itself."""
    with (
        patch(f"{_CLIMATE}.er.async_get", return_value=er_reg),
        patch(f"{_CLIMATE}.dr.async_get", return_value=dr_reg),
    ):
        return BetterThermostat.device_info.fget(bt)


def test_device_info_names_the_trv_device_by_its_registry_id():
    """The via link carries the TRV's device id, not one of its identifiers."""
    trv_device = _device("trv_device_id", {("mqtt", "0x1234")})
    er_reg, dr_reg = _registries(trv_device)

    info = _read_device_info(_bt([{"trv": "climate.trv"}]), er_reg, dr_reg)

    assert info["via_device_id"] == "trv_device_id"
    assert "via_device" not in info


def test_device_info_links_a_trv_device_that_carries_no_identifiers():
    """A device registered by connections alone still gets the link.

    Every registry entry carries the id the link is written with, so the
    identifiers are read for one thing only: keeping the BT device off its
    own via link.
    """
    er_reg, dr_reg = _registries(_device("trv_device_id", set()))

    info = _read_device_info(_bt([{"trv": "climate.trv"}]), er_reg, dr_reg)

    assert info["via_device_id"] == "trv_device_id"


def test_device_info_omits_the_link_for_a_trv_on_the_bt_device_itself():
    """A self-referencing via link is refused by the registry, so it is not written."""
    trv_device = _device("bt_device_id", {(DOMAIN, BT_UID)})
    er_reg, dr_reg = _registries(trv_device, device_id="bt_device_id")

    info = _read_device_info(_bt([{"trv": "climate.trv"}]), er_reg, dr_reg)

    assert "via_device_id" not in info
    assert info["identifiers"] == {(DOMAIN, BT_UID)}


def test_device_info_omits_the_link_without_a_trv_device_entry():
    """A TRV whose device the registry does not hold leaves the link unset."""
    er_reg, dr_reg = _registries(None)

    info = _read_device_info(_bt([{"trv": "climate.trv"}]), er_reg, dr_reg)

    assert "via_device_id" not in info
