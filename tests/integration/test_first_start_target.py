"""The room target a thermostat comes up with when it has none of its own.

A thermostat that starts without a saved target, on a fresh install or from
a stored state that carries no target, takes over the setpoint its heads
already hold, bounded into the range it can publish. The fallback default is
only for heads that report no setpoint at all. A head that is off holds its
off or frost setpoint, not a room target, and a separate cooler is not a head
of the room: neither setpoint says anything about the heating target.
"""

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    ClimateEntityFeature,
    HVACMode,
)
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


class _RangeHead(FakeTrvEntity):
    """A head that publishes a 21-25 °C band and no single setpoint."""

    _attr_hvac_modes = [HVACMode.HEAT_COOL, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(self):
        super().__init__()
        self._attr_hvac_mode = HVACMode.HEAT_COOL
        self._attr_target_temperature = None
        self._attr_target_temperature_low = 21.0
        self._attr_target_temperature_high = 25.0


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


_ENTRY_TRV = make_entry().data["thermostat"][0]


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
async def test_a_range_head_hands_over_its_heating_setpoint(hass, stored):
    """A head publishing a 21-25 °C band and no setpoint gives the room 21 °C."""
    bt = await _start(hass, stored, _RangeHead())

    assert bt.bt_target_temp == 21.0


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


class _Group(FakeTrvEntity):
    """One head of a three-head room, on its own mode and setpoint."""

    def __init__(self, letter, mode, target):
        super().__init__()
        self._attr_name = f"group trv {letter}"
        self._attr_hvac_mode = mode
        self._attr_target_temperature = target


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
    group = [
        _Group(letter, mode, target)
        for letter, (mode, target) in zip("abc", heads, strict=True)
    ]
    trvs = [{**_ENTRY_TRV, "trv": f"climate.group_trv_{letter}"} for letter in "abc"]

    bt = await _start(hass, stored, *group, thermostat=trvs)

    assert bt.bt_target_temp == expected


@pytest.mark.parametrize("stored", START_KINDS)
async def test_a_no_off_head_parked_at_its_minimum_does_not_set_the_room_target(
    hass, stored
):
    """A head that cannot switch off and sits at its minimum counts as off.

    Such a device reports heat while it holds its minimum, which is its way of
    being off, so only the two heads that heat carry the room target.
    """
    heads = ((HVACMode.HEAT, 5.0), (HVACMode.HEAT, 22.0), (HVACMode.HEAT, 20.0))
    group = [
        _Group(letter, mode, target)
        for letter, (mode, target) in zip("abc", heads, strict=True)
    ]
    trvs = [{**_ENTRY_TRV, "trv": f"climate.group_trv_{letter}"} for letter in "abc"]
    trvs[0] = {
        **trvs[0],
        "advanced": {**trvs[0]["advanced"], "no_off_system_mode": True},
    }

    bt = await _start(hass, stored, *group, thermostat=trvs)

    assert bt.bt_target_temp == 21.0


class _ReversibleAc(FakeTrvEntity):
    """One air conditioner serving as the room's head and as its cooler."""

    _attr_name = "reversible ac"
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(self, mode):
        super().__init__()
        self._attr_hvac_mode = mode
        self._attr_current_temperature = 24.0
        self._attr_target_temperature_low = None
        self._attr_target_temperature_high = None
        if mode == HVACMode.HEAT_COOL:
            self._attr_target_temperature = None
            self._attr_target_temperature_low = 21.0
            self._attr_target_temperature_high = 25.0
        else:
            self._attr_target_temperature = 26.0


REVERSIBLE_AC_ID = "climate.reversible_ac"


@pytest.mark.parametrize("stored", START_KINDS)
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param(HVACMode.COOL, DEFAULT_TARGET_TEMP, id="cooling"),
        pytest.param(HVACMode.HEAT_COOL, 21.0, id="range"),
    ],
)
async def test_a_shared_head_that_cools_does_not_set_the_room_target(
    hass, mode, expected, stored
):
    """A device that is both head and cooler hands over only a heating setpoint.

    Cooling at 26 °C, its setpoint is a cooling target and leaves the room on
    the default. On a heat/cool range, the lower bound is its heating
    setpoint and becomes the room target.
    """
    trvs = [{**_ENTRY_TRV, "trv": REVERSIBLE_AC_ID}]

    bt = await _start(
        hass, stored, _ReversibleAc(mode), thermostat=trvs, cooler=REVERSIBLE_AC_ID
    )

    assert bt.bt_target_temp == expected
