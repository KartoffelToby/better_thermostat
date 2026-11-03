"""The room target a thermostat comes up with when it has none of its own.

A thermostat that starts without a saved target, on a fresh install or from
a stored state that carries no target, takes over the setpoint its heads
already hold, bounded into the range it can publish. The fallback default is
only for heads that report no setpoint at all. A head that is off holds its
off or frost setpoint, not a room target, and a separate cooler is not a head
of the room: neither setpoint says anything about the heating target.
"""

from dataclasses import replace

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.core import State
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.better_thermostat.utils.const import DEFAULT_TARGET_TEMP

from .conftest import (
    BT_ENTITY,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import (
    DUAL_ROLE,
    FAHRENHEIT_TRV,
    GENERIC_HEAT_TRV,
    GROUP_OF_THREE,
    SEPARATE_COOLER,
    GroupScenario,
)

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

    assert bt.heat_target_temperature == pytest.approx(20.0, abs=0.01)


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

    assert bt.heat_target_temperature == expected


@pytest.mark.parametrize("stored", START_KINDS)
async def test_a_range_head_hands_over_its_heating_setpoint(hass, stored):
    """A head publishing a 21-25 °C band and no setpoint gives the room 21 °C."""
    profile = replace(
        GENERIC_HEAT_TRV,
        hvac_modes=(HVACMode.HEAT_COOL, HVACMode.OFF),
        hvac_mode=HVACMode.HEAT_COOL,
        target_temperature=None,
        target_temperature_low=21.0,
        target_temperature_high=25.0,
        supported_features=ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON,
    )

    bt = await _start(hass, profile, stored, profile)

    assert bt.heat_target_temperature == 21.0


@pytest.mark.parametrize("stored", START_KINDS)
async def test_the_cooler_setpoint_does_not_shift_the_heating_target(hass, stored):
    """With a separate cooler at 24 °C, the heating target is the head's 20 °C."""
    scenario = SEPARATE_COOLER
    assert scenario.cooler is not None

    bt = await _start(hass, scenario, stored, scenario.trv, scenario.cooler)

    assert bt.heat_target_temperature == 20.0


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

    assert bt.heat_target_temperature == expected


def _group(*heads: tuple[HVACMode, float | None]) -> GroupScenario:
    """The three-head group with each head on the given mode and setpoint."""
    return GroupScenario(
        name="heads",
        profiles=tuple(
            replace(profile, hvac_mode=mode, target_temperature=setpoint)
            for profile, (mode, setpoint) in zip(
                GROUP_OF_THREE.profiles, heads, strict=True
            )
        ),
    )


@pytest.mark.parametrize("stored", START_KINDS)
@pytest.mark.parametrize(
    ("heads", "expected"),
    [
        pytest.param(
            ((HVACMode.OFF, 5.0), (HVACMode.HEAT, 22.0), (HVACMode.HEAT, None)),
            22.0,
            id="one-head-off",
        ),
        pytest.param(
            ((HVACMode.OFF, 5.0), (HVACMode.HEAT, 22.0), (HVACMode.HEAT, 20.0)),
            21.0,
            id="mean-of-the-heating-heads",
        ),
        pytest.param(
            ((HVACMode.OFF, 12.0), (HVACMode.OFF, 14.0), (HVACMode.OFF, None)),
            DEFAULT_TARGET_TEMP,
            id="every-head-off",
        ),
    ],
)
async def test_a_head_that_is_off_does_not_set_the_room_target(
    hass, heads, expected, stored
):
    """Only heads that are on carry a room target; with none on, the default."""
    group = _group(*heads)

    bt = await _start(hass, group, stored, *group.profiles)

    assert bt.heat_target_temperature == expected


@pytest.mark.parametrize("stored", START_KINDS)
async def test_a_no_off_head_parked_at_its_minimum_does_not_set_the_room_target(
    hass, stored
):
    """A head that cannot switch off and sits at its minimum counts as off.

    Such a device reports heat while it holds its minimum, which is its way of
    being off, so only the two heads that heat carry the room target.
    """
    group = _group((HVACMode.HEAT, 5.0), (HVACMode.HEAT, 22.0), (HVACMode.HEAT, 20.0))
    if stored is not None:
        mock_restore_cache(hass, (stored,))
    await build_devices(hass, *group.profiles)
    set_room_sensor(hass, 19.0)
    base = make_entry(group)
    data = dict(base.data)
    heads = [dict(head, advanced=dict(head["advanced"])) for head in data["thermostat"]]
    heads[0]["advanced"]["no_off_system_mode"] = True
    data["thermostat"] = heads
    entry = MockConfigEntry(
        domain=base.domain, version=base.version, data=data, title=base.title
    )
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert bt.heat_target_temperature == 21.0


_DUAL_ROLE_COOLING = replace(
    DUAL_ROLE.trv, hvac_mode=HVACMode.COOL, target_temperature=26.0
)
_DUAL_ROLE_ON_A_RANGE = replace(
    DUAL_ROLE.trv,
    hvac_modes=(HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL, HVACMode.OFF),
    hvac_mode=HVACMode.HEAT_COOL,
    target_temperature=None,
    target_temperature_low=21.0,
    target_temperature_high=25.0,
    supported_features=ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON,
)


@pytest.mark.parametrize("stored", START_KINDS)
@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        pytest.param(_DUAL_ROLE_COOLING, DEFAULT_TARGET_TEMP, id="cooling"),
        pytest.param(_DUAL_ROLE_ON_A_RANGE, 21.0, id="range"),
    ],
)
async def test_a_shared_head_that_cools_does_not_set_the_room_target(
    hass, profile, expected, stored
):
    """A device that is both head and cooler hands over only a heating setpoint.

    Cooling at 26 °C, its setpoint is a cooling target and leaves the room on
    the default. On a heat/cool range, the lower bound is its heating
    setpoint and becomes the room target.
    """
    scenario = replace(DUAL_ROLE, trv=profile)

    bt = await _start(hass, scenario, stored, profile)

    assert bt.heat_target_temperature == expected
