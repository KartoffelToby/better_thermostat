"""Running valve maintenance on demand says why it did not run.

The service exercises the valves of every head that has valve maintenance
enabled. On a thermostat where none has, there is nothing to run; the call
says so instead of returning as if it had worked.
"""

from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.better_thermostat.utils.const import (
    SERVICE_RUN_VALVE_MAINTENANCE,
)

from .conftest import DOMAIN, make_entry, set_room_sensor, setup_entry, wait_for_startup
from .device_profiles import GENERIC_HEAT_TRV


async def test_maintenance_without_an_enabled_head_is_refused(hass, fake_trv):
    """No head has valve maintenance enabled, so the call is refused."""
    assert not GENERIC_HEAT_TRV.valve_maintenance
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_VALVE_MAINTENANCE,
            {"entity_id": bt.entity_id},
            blocking=True,
        )

    assert refused.value.translation_key == "valve_maintenance_not_enabled"
    assert str(refused.value) == ("BT Test has no valve with valve maintenance enabled")
