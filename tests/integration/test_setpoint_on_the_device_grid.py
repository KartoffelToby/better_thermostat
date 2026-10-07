"""A setpoint is written on the device's grid, whatever step is configured.

A TRV holds every write on its own grid. A thermostat configured with a
finer step than the device's would write values the device never reports
back as sent, and every such write would end in an unconfirmed-write
warning six minutes later.
"""

from dataclasses import replace
import logging

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import ATTR_TEMPERATURE
import pytest

from .conftest import (
    BT_ENTITY,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, TRV_ID

HALF_DEGREE_TRV_WITH_A_FINE_CONFIGURED_STEP = replace(
    GENERIC_HEAT_TRV,
    name="half_degree_trv_fine_configured_step",
    target_temperature_step=0.5,
    configured_target_temp_step="0.1",
)


def _hold_writes_on_the_half_degree_grid(trv) -> None:
    """Make the simulated device hold every write rounded onto its 0.5 grid."""
    apply_setpoint = trv.async_set_temperature

    async def _set_temperature(**kwargs) -> None:
        sent = kwargs.get(ATTR_TEMPERATURE)
        if sent is None:
            await apply_setpoint(**kwargs)
            return
        await apply_setpoint(**{**kwargs, ATTR_TEMPERATURE: round(sent * 2) / 2})
        trv.set_temperature_calls[-1] = sent

    trv.async_set_temperature = _set_temperature


@pytest.mark.parametrize(
    "fake_trv", [HALF_DEGREE_TRV_WITH_A_FINE_CONFIGURED_STEP], indirect=True
)
async def test_a_finer_configured_step_writes_on_the_device_grid(
    hass, fake_trv, caplog
):
    """A 0.1 step configured for a 0.5 TRV writes 22.0 for a 21.9 target."""
    _hold_writes_on_the_half_degree_grid(fake_trv)
    set_room_sensor(hass, fake_trv.profile.current_temperature)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.real_trvs[TRV_ID].target_temp_step == pytest.approx(0.5)

    caplog.set_level(logging.WARNING)
    baseline = len(fake_trv.set_temperature_calls)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": 21.9},
        blocking=True,
    )
    assert await wait_for(hass, lambda: len(fake_trv.set_temperature_calls) > baseline)
    assert await wait_for(hass, lambda: bt.real_trvs[TRV_ID].target_temp_received, 5.0)

    assert bt.heat_target_temperature == pytest.approx(21.9)
    assert fake_trv.set_temperature_calls[baseline:] == [pytest.approx(22.0)]
    assert "did not confirm the target temperature" not in caplog.text
