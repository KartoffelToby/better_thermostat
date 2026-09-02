"""Device binding for Better Thermostat.

Links a Better Thermostat device to the TRV device it controls in the Home
Assistant device registry, and clears that link again when it no longer
applies. The link is single-valued, so a BT instance carries it only while
it manages exactly one TRV.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_bind_trv_device(
    hass: HomeAssistant, bt_unique_id: str, trv_entity_id: str, bt_entry_id: str
) -> bool:
    """Bind a BT instance to a TRV device in the HA device registry.

    Sets ``via_device`` on the BT device to point to the TRV device. On the HA
    device info page for the BT instance this renders as "via: <TRV name>", and
    the TRV device page lists the BT instance under "Connected devices".
    """
    er_reg = er.async_get(hass)
    dr_reg = dr.async_get(hass)

    trv_entry = er_reg.async_get(trv_entity_id)
    if trv_entry is None or trv_entry.device_id is None:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s not yet in entity registry; skipping device binding",
            bt_unique_id,
            trv_entity_id,
        )
        return False

    trv_device = dr_reg.async_get(trv_entry.device_id)
    if trv_device is None or not trv_device.identifiers:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s has no device registry entry; skipping",
            bt_unique_id,
            trv_entity_id,
        )
        return False

    trv_id = next(iter(trv_device.identifiers))
    dr_reg.async_get_or_create(
        config_entry_id=bt_entry_id,
        identifiers={(DOMAIN, bt_unique_id)},
        via_device=trv_id,
    )

    _LOGGER.debug(
        "better_thermostat %s: bound to TRV device %s (%s)",
        bt_unique_id,
        trv_entity_id,
        trv_device.name_by_user or trv_device.name,
    )
    return True


async def async_unbind_trv_device(hass: HomeAssistant, bt_unique_id: str) -> bool:
    """Clear a stale ``via_device`` link on the BT device.

    Multi-TRV setups carry no ``via_device`` link (it is single-valued), but
    a BT device that was once bound to a single valve keeps that link in the
    device registry until it is cleared explicitly. Passing ``None`` for
    ``via_device_id`` removes the link; the registry treats the omitted
    (UNDEFINED) value as "leave unchanged".

    Returns True when a link was cleared, False otherwise.
    """
    dr_reg = dr.async_get(hass)
    bt_device = dr_reg.async_get_device(identifiers={(DOMAIN, bt_unique_id)})
    if bt_device is None or bt_device.via_device_id is None:
        return False

    dr_reg.async_update_device(bt_device.id, via_device_id=None)
    _LOGGER.debug(
        "better_thermostat %s: cleared stale via_device link on device %s",
        bt_unique_id,
        bt_device.id,
    )
    return True
