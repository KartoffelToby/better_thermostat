"""A setpoint is written on the device's grid, whatever step is configured.

A TRV holds every write on its own grid. A thermostat configured with a
finer step than the device's would write values the device never reports
back as sent, and every such write would end in an unconfirmed-write
warning six minutes later.
"""

import logging

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

from .conftest import (
    SENSOR_ID,
    TRV_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"


class HalfDegreeTrv(FakeTrvEntity):
    """A TRV that publishes a 0.5 step and holds every write on that grid."""

    async def async_set_temperature(self, **kwargs) -> None:
        """Record the value sent and hold it rounded onto the 0.5 grid."""
        sent = kwargs[ATTR_TEMPERATURE]
        await super().async_set_temperature(
            **{**kwargs, ATTR_TEMPERATURE: round(sent * 2) / 2}
        )
        self.set_temperature_calls[-1] = sent


@pytest.fixture
async def half_degree_trv(hass):
    """Register the half-degree TRV with the real climate component."""
    entity = HalfDegreeTrv()
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [entity])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    return entity


async def test_a_finer_configured_step_writes_on_the_device_grid(
    hass, half_degree_trv, caplog
):
    """A 0.1 step configured for a 0.5 TRV writes 22.0 for a 21.9 target."""
    hass.states.async_set(SENSOR_ID, "19.5", {"unit_of_measurement": "°C"})
    template = make_entry()
    entry = MockConfigEntry(
        domain=template.domain,
        version=template.version,
        data={**template.data, "target_temp_step": "0.1"},
        title=template.title,
    )
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.real_trvs[TRV_ID].target_temp_step == pytest.approx(0.5)

    caplog.set_level(logging.WARNING)
    baseline = len(half_degree_trv.set_temperature_calls)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": 21.9},
        blocking=True,
    )
    assert await wait_for(
        hass, lambda: len(half_degree_trv.set_temperature_calls) > baseline
    )
    assert await wait_for(hass, lambda: bt.real_trvs[TRV_ID].target_temp_received, 5.0)

    assert bt.bt_target_temp == pytest.approx(21.9)
    assert half_degree_trv.set_temperature_calls[baseline:] == [pytest.approx(22.0)]
    assert "did not confirm the target temperature" not in caplog.text
