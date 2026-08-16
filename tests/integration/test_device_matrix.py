"""End-to-end tests across the device forms the integration is used with.

The lifecycle tests drive one device from entry to write. These drive the
axes real devices differ on — the mode vocabulary, the setpoint grid, the
temperature unit, the calibration channel and the role a device plays — and
assert on what arrives at the simulated device rather than on what Better
Thermostat believes it sent.
"""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.const import UnitOfTemperature
import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from .conftest import (
    BT_ENTITY,
    OFFSET_NUMBER_ID,
    TRV_ID,
    assert_on_device_grid,
    assert_profile_adopted,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import (
    AUTO_COOL_AC,
    AUTO_ONLY_TRV,
    DUAL_ROLE,
    FAHRENHEIT_TRV,
    GENERIC_HEAT_TRV,
    HEAT_COOL_TRV,
    INTEGER_GRID_TRV,
    MQTT_OFFSET_TRV,
    SEPARATE_COOLER,
    SINGLE_ROLE_PROFILES,
    TADO_OFFSET_TRV,
    DeviceProfile,
)

_WRITE_BUDGET = (
    "custom_components.better_thermostat.utils.controlling.MIN_WRITE_INTERVAL_S"
)
_COOLER_RESEND = (
    "custom_components.better_thermostat.utils.controlling.COOLER_RESEND_INTERVAL_S"
)

# A mode the device does not offer is dropped before the write, so the wait
# for a mode write that is never sent can only run out. It is bounded tightly
# rather than holding the suite for the full timeout.
_DECLINED_WRITE_TIMEOUT_S = 3.0

_DUAL_ROLE_TRV_WRITE_LANDS_LAST = (
    "one entity configured as both thermostat and cooler receives cool from the "
    "cooler channel and heat from the TRV channel in the same control cycle; the "
    "TRV write lands last, so the device is left in heat at the calibrated heat "
    "setpoint while the entity reports cooling"
)


def _switched_off(profile: DeviceProfile) -> DeviceProfile:
    """Return the profile with the device sitting in off.

    A device already in the mode Better Thermostat wants receives no mode
    write at all, so on a device that was never commanded the absence of a
    write proves nothing. Starting from off makes the write the only way the
    device can reach its heating mode.
    """
    return replace(profile, hvac_mode=HVACMode.OFF)


@pytest.mark.parametrize(
    "fake_trv", SINGLE_ROLE_PROFILES, indirect=True, ids=profile_id
)
async def test_startup_adopts_the_device_capabilities(hass, fake_trv):
    """Startup reads back every capability the device publishes.

    The guard under every other row of the matrix: an axis whose values
    never reach the integration is an axis that proves nothing, and a
    device read too late looks like a device without capabilities.
    """
    profile = fake_trv.profile
    set_room_sensor(hass, profile.current_temperature, profile.temperature_unit)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert_profile_adopted(bt, profile)


@pytest.mark.parametrize(
    ("fake_trv", "heat_auto_swapped"),
    [
        pytest.param(_switched_off(GENERIC_HEAT_TRV), False, id="generic_heat_trv"),
        pytest.param(_switched_off(HEAT_COOL_TRV), False, id="heat_cool_trv"),
        pytest.param(
            _switched_off(AUTO_COOL_AC), True, id="auto_cool_ac-heat_auto_swapped"
        ),
        pytest.param(
            _switched_off(AUTO_ONLY_TRV), True, id="auto_only_trv-heat_auto_swapped"
        ),
    ],
    indirect=["fake_trv"],
)
async def test_mode_write_reaches_a_device_that_offers_the_mode(
    hass, fake_trv, heat_auto_swapped
):
    """Switching the thermostat on writes a mode the device offers.

    Every device names its heating mode differently: plain heat, heat_cool,
    or the auto that the swap option declares to mean heat. Whichever it is,
    the device has to end up in it, so the observables are the write that
    arrived and the mode the device is left in.
    """
    profile = fake_trv.profile
    set_room_sensor(hass, 18.0)
    entry = make_entry(heat_auto_swapped=heat_auto_swapped)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, profile)
    fake_trv.set_hvac_mode_calls.clear()

    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": BT_ENTITY, "hvac_mode": "heat"},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": 22.0},
        blocking=True,
    )

    assert await wait_for(hass, lambda: fake_trv.set_hvac_mode_calls)
    written = fake_trv.set_hvac_mode_calls[-1]
    assert written in profile.hvac_modes
    assert written != HVACMode.OFF
    assert hass.states.get(TRV_ID).state == written


@pytest.mark.parametrize(
    "fake_trv",
    [_switched_off(AUTO_COOL_AC), _switched_off(AUTO_ONLY_TRV)],
    indirect=True,
    ids=["auto_cool_ac", "auto_only_trv"],
)
async def test_device_without_an_offered_heating_mode_keeps_its_mode(
    hass, fake_trv, caplog
):
    """A device offering no heating mode is left alone and told about.

    Better Thermostat will not guess which of a device's modes heats: on a
    head that offers auto but no heat, auto may mean heating, and on an air
    conditioner offering the same list it means automatic heat/cool. So no
    mode is written at all, the device keeps the mode it had, and the user
    is pointed at the swap option that would resolve the ambiguity. The
    setpoint is unaffected by any of that and still arrives.

    The counterpart is the swapped parametrization of the mode-write test
    above: the same two devices do receive their heating mode once the entry
    declares what auto means.
    """
    profile = fake_trv.profile
    set_room_sensor(hass, 18.0)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, profile)
    fake_trv.set_hvac_mode_calls.clear()

    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": BT_ENTITY, "hvac_mode": "heat"},
        blocking=True,
    )
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": 22.0},
        blocking=True,
    )
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)

    # No mode write is attempted, so the device stays where it was.
    assert not await wait_for(
        hass, lambda: fake_trv.set_hvac_mode_calls, timeout_s=_DECLINED_WRITE_TIMEOUT_S
    )
    assert hass.states.get(TRV_ID).state == HVACMode.OFF
    assert_on_device_grid(fake_trv.set_temperature_calls[-1], profile)

    # The device is not silently abandoned: the one thing that would make it
    # controllable is named, once.
    declined = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "ERROR" and "does not offer HVAC mode" in record.message
    ]
    assert len(declined) == 1
    assert TRV_ID in declined[0]
    assert "heat auto swapped" in declined[0]


@pytest.mark.parametrize(
    ("fake_trv", "expected_setpoint"),
    [
        pytest.param(GENERIC_HEAT_TRV, 20.5, id="generic_heat_trv"),
        pytest.param(INTEGER_GRID_TRV, 21.0, id="integer_grid_trv"),
    ],
    indirect=["fake_trv"],
)
async def test_setpoint_rounds_to_the_device_grid(hass, fake_trv, expected_setpoint):
    """The setpoint arrives snapped to the grid the device publishes.

    The room sensor reads what the device reads, so the calibration is a
    no-op and the value under test is the target itself. A room demanding
    heat rounds it up onto the grid, so the whole-degree device receives
    the next full degree instead of a setpoint it is already sitting at.
    """
    set_room_sensor(hass, fake_trv.profile.current_temperature)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)

    with patch(_WRITE_BUDGET, 0.0):
        baseline = len(fake_trv.set_temperature_calls)
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 20.5},
            blocking=True,
        )
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > baseline
        )

    assert fake_trv.set_temperature_calls[-1] == pytest.approx(expected_setpoint)


@pytest.mark.parametrize("fake_trv", [FAHRENHEIT_TRV], indirect=True, ids=profile_id)
async def test_fahrenheit_device_is_read_and_written_in_its_own_unit(hass, fake_trv):
    """A device on a Fahrenheit system publishes and receives Fahrenheit.

    The range and the reading are converted into Celsius on the way in and
    the step is scaled as the difference it is; the setpoint is converted
    back on the way out and lands on the device's own whole-degree grid. A
    Celsius-sized number would sit far below the device's range, and a value
    rounded before the conversion would sit off its grid.
    """
    profile = fake_trv.profile
    set_room_sensor(hass, 64.4, UnitOfTemperature.FAHRENHEIT)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, profile)

    trv = bt.real_trvs[TRV_ID]
    assert trv.min_temp == pytest.approx(5.0, abs=0.01)
    assert trv.max_temp == pytest.approx(30.0, abs=0.01)
    assert trv.target_temp_step == pytest.approx(0.5556, abs=1e-3)

    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    written = fake_trv.set_temperature_calls[-1]
    assert profile.min_temp <= written <= profile.max_temp
    assert_on_device_grid(written, profile)


@pytest.mark.parametrize("fake_trv", [MQTT_OFFSET_TRV], indirect=True, ids=profile_id)
async def test_offset_reaches_the_calibration_number_entity(hass, fake_trv):
    """A device with a calibration number is corrected through that entity.

    The channel only exists because the device carries a registry entry: the
    calibration entity is found by walking the device the head belongs to.
    """
    room = 21.0
    expected_offset = room - fake_trv.profile.current_temperature
    set_room_sensor(hass, room)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)

    # The calibration entity is zeroed once on startup, so the write under
    # test is the last one, never the only one.
    offset_number = fake_trv.offset_number
    assert await wait_for(
        hass,
        lambda: (
            offset_number.set_value_calls
            and offset_number.set_value_calls[-1] == pytest.approx(expected_offset)
        ),
    ), offset_number.set_value_calls
    assert float(hass.states.get(OFFSET_NUMBER_ID).state) == pytest.approx(
        expected_offset
    )
    assert bt.real_trvs[TRV_ID].last_calibration == pytest.approx(expected_offset)


@pytest.mark.parametrize("fake_trv", [TADO_OFFSET_TRV], indirect=True, ids=profile_id)
async def test_offset_reaches_the_ecosystem_service(hass, fake_trv):
    """A device without a calibration entity is corrected by a service call."""
    offset_calls = async_mock_service(hass, "tado", "set_climate_temperature_offset")
    room = 21.0
    expected_offset = room - fake_trv.profile.current_temperature
    set_room_sensor(hass, room)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)

    assert await wait_for(hass, lambda: offset_calls)
    payload = offset_calls[-1].data
    assert payload["entity_id"] == TRV_ID
    assert payload["offset"] == pytest.approx(expected_offset)


@pytest.mark.parametrize(
    "device_role",
    [
        pytest.param(SEPARATE_COOLER, id="separate_cooler"),
        pytest.param(
            DUAL_ROLE,
            id="dual_role",
            marks=pytest.mark.xfail(
                strict=True, reason=_DUAL_ROLE_TRV_WRITE_LANDS_LAST
            ),
        ),
    ],
    indirect=True,
)
async def test_cooling_demand_reaches_the_cooler(hass, device_role):
    """A room above the cooling target leaves the cooler cooling.

    Both throttles are opened: the write budget paces the thermostat channel
    and the resend interval paces the cooler channel, and the demand appears
    within the window of both.
    """
    thermostat, cooler = device_role[0], device_role[-1]
    set_room_sensor(hass, 27.0)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, thermostat.profile)

    with patch(_WRITE_BUDGET, 0.0), patch(_COOLER_RESEND, 0.0):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": BT_ENTITY, "target_temp_low": 19.0, "target_temp_high": 23.0},
            blocking=True,
        )
        assert await wait_for(hass, lambda: "cool" in cooler.set_hvac_mode_calls)
        assert await wait_for(hass, lambda: cooler.set_temperature_calls)

    assert hass.states.get(cooler.entity_id).state == HVACMode.COOL
    assert cooler.set_temperature_calls[-1] == pytest.approx(23.0)
