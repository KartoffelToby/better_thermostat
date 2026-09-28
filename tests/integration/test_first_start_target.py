"""The room target a thermostat comes up with when it has none of its own.

A thermostat that starts without a saved target, on a fresh install or from
a stored state that carries no target, takes over the setpoint its heads
already hold, bounded into the range it can publish. The fallback default is
only for heads that report no setpoint at all. A separate cooler is not a
head of the room: its cooling setpoint says nothing about the heating target.
"""

from dataclasses import replace

from homeassistant.core import State
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.better_thermostat.utils.const import DEFAULT_TARGET_TEMP

from .conftest import (
    BT_ENTITY,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import FAHRENHEIT_TRV, GENERIC_HEAT_TRV, SEPARATE_COOLER

# Each start is run twice: from nothing, and from a stored state that
# carries no target, as a thermostat saved while it was unavailable does.
START_KINDS = (
    pytest.param(None, id="fresh"),
    pytest.param(State(BT_ENTITY, "unavailable", {}), id="stored-without-target"),
)


async def _start(hass, devices, stored: State | None, *profiles):
    """Set up an entry for ``devices`` and return its entity once started."""
    if stored is not None:
        mock_restore_cache(hass, (stored,))
    await build_devices(hass, *profiles)
    set_room_sensor(hass, 19.0)
    entry = make_entry(devices)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


@pytest.mark.parametrize("stored", START_KINDS)
@pytest.mark.parametrize(
    ("unit_system", "profile"),
    [
        pytest.param(METRIC_SYSTEM, GENERIC_HEAT_TRV, id="celsius-head"),
        pytest.param(US_CUSTOMARY_SYSTEM, GENERIC_HEAT_TRV, id="celsius-head-f-sys"),
        pytest.param(US_CUSTOMARY_SYSTEM, FAHRENHEIT_TRV, id="fahrenheit-head"),
    ],
)
async def test_a_start_without_a_target_adopts_the_head_setpoint(
    hass, unit_system, profile, stored
):
    """The thermostat takes the 20 °C its head holds, in either unit."""
    hass.config.units = unit_system

    bt = await _start(hass, profile, stored, profile)

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
    profile = replace(GENERIC_HEAT_TRV, min_temp=7.0, target_temperature=head_setpoint)

    bt = await _start(hass, profile, stored, profile)

    assert bt.bt_target_temp == expected


@pytest.mark.parametrize("stored", START_KINDS)
async def test_the_cooler_setpoint_does_not_shift_the_heating_target(hass, stored):
    """With a separate cooler at 24 °C, the heating target is the head's 20 °C."""
    scenario = SEPARATE_COOLER
    assert scenario.cooler is not None

    bt = await _start(hass, scenario, stored, scenario.trv, scenario.cooler)

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
    profile = replace(GENERIC_HEAT_TRV, min_temp=min_temp, target_temperature=None)

    bt = await _start(hass, profile, stored, profile)

    assert bt.bt_target_temp == expected
