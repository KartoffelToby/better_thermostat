"""The room target a thermostat comes up with when it has none of its own.

A thermostat that starts without a saved target, on a fresh install or from
a stored state that carries no target, takes over the setpoint its heads
already hold, bounded into the range it can publish. The fallback default is
only for heads that report no setpoint at all. A separate cooler is not a
head of the room: its cooling setpoint says nothing about the heating target.
"""

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    setup_test_component_platform,
)

from custom_components.better_thermostat.utils.const import DEFAULT_TARGET_TEMP

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"
COOLER_ID = "climate.fake_cooler"

# Each start is run twice: from nothing, and from a stored state that
# carries no target, as a thermostat saved while it was unavailable does.
START_KINDS = (
    pytest.param(None, id="fresh"),
    pytest.param(State(BT_ENTITY, "unavailable", {}), id="stored-without-target"),
)


class _Head(FakeTrvEntity):
    """The fake TRV, on a configurable setpoint, range and unit."""

    def __init__(self, *, target, min_temp=5.0, max_temp=30.0, unit=None):
        super().__init__()
        self._attr_target_temperature = target
        self._attr_min_temp = min_temp
        self._attr_max_temp = max_temp
        if unit is not None:
            self._attr_temperature_unit = unit
            self._attr_current_temperature = 67.1
            self._attr_target_temperature_step = 1.0


class _Cooler(FakeTrvEntity):
    """A separate air conditioner at 24 °C with a narrower range."""

    _attr_name = "fake cooler"
    _attr_hvac_modes = [HVACMode.COOL, HVACMode.OFF]
    _attr_min_temp = 16.0

    def __init__(self):
        super().__init__()
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_current_temperature = 22.0
        self._attr_target_temperature = 24.0


async def _start(hass, stored, *entities, **entry_data):
    """Register ``entities``, set an entry up and return its thermostat."""
    if stored is not None:
        mock_restore_cache(hass, (stored,))
    setup_test_component_platform(hass, CLIMATE_DOMAIN, list(entities))
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    base = make_entry()
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=base.version,
        data={**base.data, **entry_data},
        title=base.title,
    )
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


@pytest.mark.parametrize("stored", START_KINDS)
@pytest.mark.parametrize(
    ("unit_system", "head"),
    [
        pytest.param(METRIC_SYSTEM, lambda: _Head(target=20.0), id="celsius-head"),
        pytest.param(
            US_CUSTOMARY_SYSTEM, lambda: _Head(target=20.0), id="celsius-head-f-sys"
        ),
        pytest.param(
            US_CUSTOMARY_SYSTEM,
            lambda: _Head(
                target=68.0,
                min_temp=41.0,
                max_temp=86.0,
                unit=UnitOfTemperature.FAHRENHEIT,
            ),
            id="fahrenheit-head",
        ),
    ],
)
async def test_a_start_without_a_target_adopts_the_head_setpoint(
    hass, unit_system, head, stored
):
    """The thermostat takes the 20 °C its head holds, in either unit."""
    hass.config.units = unit_system

    bt = await _start(hass, stored, head())

    assert bt.bt_target_temp == pytest.approx(20.0, abs=0.01)


@pytest.mark.parametrize("stored", START_KINDS)
@pytest.mark.parametrize(
    ("head_setpoint", "expected"),
    [pytest.param(35.0, 30.0, id="above-max"), pytest.param(6.0, 7.0, id="below-min")],
)
async def test_an_adopted_setpoint_is_bounded_into_the_range(
    hass, head_setpoint, expected, stored
):
    """A head setpoint outside the published range lands on the nearer bound."""
    bt = await _start(hass, stored, _Head(target=head_setpoint, min_temp=7.0))

    assert bt.bt_target_temp == expected


@pytest.mark.parametrize("stored", START_KINDS)
async def test_the_cooler_setpoint_does_not_shift_the_heating_target(hass, stored):
    """With a separate cooler at 24 °C, the heating target is the head's 20 °C."""
    bt = await _start(hass, stored, _Head(target=20.0), _Cooler(), cooler=COOLER_ID)

    assert bt.bt_target_temp == 20.0


@pytest.mark.parametrize("stored", START_KINDS)
@pytest.mark.parametrize(
    ("min_temp", "expected"),
    [
        pytest.param(5.0, DEFAULT_TARGET_TEMP, id="default-in-range"),
        pytest.param(7.0, 7.0, id="default-below-min"),
    ],
)
async def test_heads_without_a_setpoint_get_the_default_inside_the_range(
    hass, min_temp, expected, stored
):
    """No head reports a setpoint: the default target, bounded into the range."""
    bt = await _start(hass, stored, _Head(target=None, min_temp=min_temp))

    assert bt.bt_target_temp == expected
