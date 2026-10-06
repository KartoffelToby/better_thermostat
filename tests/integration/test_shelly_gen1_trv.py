"""A Shelly TRV (Gen1) closed by Better Thermostat stays switched on.

Home Assistant's Shelly integration reports this TRV as off whenever its
setpoint sits at the minimum of 4 °C. The simulated device here does the
same, so a setpoint written at the minimum reads back as the TRV being
switched off at the device.
"""

from unittest.mock import patch

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE
import pytest

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import DeviceProfile

SHELLY_GEN1_TRV = DeviceProfile(
    name="shelly_gen1_trv",
    integration="shelly",
    calibration="target_temp_based",
    has_device_registry_entry=True,
    model="SHTRV-01",
    min_temp=4.0,
    max_temp=31.0,
    target_temperature=21.0,
)


def _report_off_at_the_minimum(trv) -> None:
    """Make the simulated device derive its mode from its setpoint."""
    apply_setpoint = trv.async_set_temperature
    minimum = trv.profile.min_temp

    async def _set_temperature(**kwargs) -> None:
        await apply_setpoint(**kwargs)
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        trv._attr_hvac_mode = HVACMode.OFF if temperature <= minimum else HVACMode.HEAT
        trv.async_write_ha_state()

    trv.async_set_temperature = _set_temperature


@pytest.mark.parametrize("fake_trv", [SHELLY_GEN1_TRV], indirect=True)
async def test_closing_the_valve_does_not_switch_the_room_off(hass, fake_trv):
    """The lowest setpoint written sits one step above the minimum.

    The room is far above a target at the bottom of the range, so the
    calibration asks for the lowest setpoint the TRV takes. Written at the
    minimum, it would switch the TRV off.
    """
    _report_off_at_the_minimum(fake_trv)
    set_room_sensor(hass, 23.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.real_trvs[fake_trv.entity_id].min_temp == pytest.approx(4.5)

    with patch(WRITE_BUDGET, 0.0):
        baseline = len(fake_trv.set_temperature_calls)
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {"entity_id": BT_ENTITY, "temperature": bt.min_temp},
            blocking=True,
        )
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > baseline
        )
        await hass.async_block_till_done()

    assert fake_trv.set_temperature_calls[-1] == pytest.approx(4.5)
    assert fake_trv.hvac_mode == HVACMode.HEAT
    assert bt.hvac_mode == HVACMode.HEAT
