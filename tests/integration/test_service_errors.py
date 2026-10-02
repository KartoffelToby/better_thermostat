"""A refused service call explains itself in the user's language.

Home Assistant validates the HVAC mode of ``climate.set_hvac_mode`` itself,
but passes the one ``climate.set_temperature`` carries through to the
entity. The thermostat refuses a mode it does not offer with a message from
its translation catalog, listing the modes it does offer.
"""

from homeassistant.exceptions import ServiceValidationError
import pytest

from .conftest import DOMAIN, make_entry, set_room_sensor, setup_entry, wait_for_startup


async def test_an_unsupported_mode_is_refused_with_the_offered_modes(hass, fake_trv):
    """The message names the refused mode and the modes this thermostat offers."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": bt.entity_id, "temperature": 21, "hvac_mode": "cool"},
            blocking=True,
        )

    assert refused.value.translation_domain == DOMAIN
    assert refused.value.translation_key == "unsupported_hvac_mode"
    assert str(refused.value) == (
        "BT Test does not support the HVAC mode cool. Supported modes: heat, off"
    )
    assert bt.hvac_mode != "cool"
