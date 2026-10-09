"""Every path on which Better Thermostat writes a temperature, on a Fahrenheit system.

Better Thermostat computes in Celsius and Home Assistant publishes in the
system unit, so every temperature crosses a conversion on its way in and on
its way out. The paths do not share one: the setpoint, the calibration offset,
the bound a setpoint is clamped to, the valve maintenance run and its
restore, the preset numbers and the defaults the config flow suggests each
convert, or fail to, on their own.

Two devices carry the axis. ``FAHRENHEIT_TRV`` works in Fahrenheit, and every
value it publishes survives the conversion; ``OFF_GRID_FAHRENHEIT_TRV`` works
in Celsius on a Fahrenheit system and has bounds that Home Assistant rounds
on the way out, which is where a conversion that is only right for round
numbers gives itself away.
"""

import copy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    EVENT_CALL_SERVICE,
    UnitOfTemperature,
)
from homeassistant.core import Context
from homeassistant.util.unit_conversion import TemperatureConverter
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.better_thermostat.utils.const import (
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_TARGET_TEMP_MAX,
    CONF_TARGET_TEMP_MIN,
    CONF_TARGET_TEMP_STEP,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTAT,
    SERVICE_RUN_VALVE_MAINTENANCE,
)
from custom_components.better_thermostat.utils.controlling import (
    compute_control_cycle,
    desired_diverges,
)

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    OUTDOOR_ID,
    SENSOR_ID,
    WRITE_BUDGET,
    assert_profile_adopted,
    build_devices,
    form_default,
    make_entry,
    set_room_sensor,
    setpoint_commands,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import (
    FAHRENHEIT_TRV,
    GENERIC_HEAT_TRV,
    OFF_GRID_FAHRENHEIT_TRV,
    TRV_ID,
    DeviceProfile,
    published_temperature,
    published_unit,
)

TRANSLATIONS = (
    Path(__file__).parents[2]
    / "custom_components"
    / "better_thermostat"
    / "translations"
    / "en.json"
)


def _fahrenheit(celsius: float) -> float:
    """Return a Celsius temperature in Fahrenheit."""
    return TemperatureConverter.convert(
        celsius, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
    )


def _published_degree(profile: DeviceProfile) -> float:
    """Return one degree of the published unit, measured in the device's unit.

    A bound Home Assistant rounded to whole published degrees is only known
    to within that much.
    """
    zero, one = (
        TemperatureConverter.convert(
            value, published_unit(profile), profile.temperature_unit
        )
        for value in (0.0, 1.0)
    )
    return abs(one - zero)


def _publish_outdoor(hass, fahrenheit: float) -> None:
    """Publish an outdoor temperature reading in Fahrenheit."""
    hass.states.async_set(
        OUTDOOR_ID,
        str(fahrenheit),
        {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT},
    )


def _publish_room_at_device_reading(hass, profile: DeviceProfile) -> None:
    """Publish a room reading equal to what the device itself reads.

    A calibration that has nothing to correct leaves the setpoint as the one
    value under test.
    """
    set_room_sensor(
        hass,
        published_temperature(profile, profile.current_temperature),
        published_unit(profile),
    )


async def _start(hass, profile: DeviceProfile, **entry_options):
    """Set an entry up for ``profile`` and return the thermostat once it runs."""
    entry = make_entry(profile, **entry_options)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, profile)
    return bt


async def _set_target(hass, fahrenheit: float) -> None:
    """Ask the thermostat for a target, in the unit its user sees."""
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {ATTR_ENTITY_ID: BT_ENTITY, ATTR_TEMPERATURE: fahrenheit},
        blocking=True,
    )


async def _settle_device_at(hass, fahrenheit: float) -> None:
    """Drive the device to a setpoint well inside its range and wait for it.

    A thermostat without a stored target starts on its lowest one, so a test
    about a later write first moves the device somewhere that write has to
    move it away from.
    """
    with patch(WRITE_BUDGET, 0.0):
        await _set_target(hass, fahrenheit)
        assert await wait_for(
            hass,
            lambda: _device_setpoint(hass) == pytest.approx(fahrenheit),
            timeout_seconds=2.0,
        ), _device_setpoint(hass)


def _device_setpoint(hass) -> float:
    """Return the setpoint the device publishes, in the system unit."""
    return hass.states.get(TRV_ID).attributes[ATTR_TEMPERATURE]


async def _run_create_flow(hass, user_input, advanced=None):
    """Run the create flow over its three steps, submitting through Home Assistant.

    Returns the entry it created.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["step_id"] == "advanced", result
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], advanced or {}
    )
    assert result["step_id"] == "confirm", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return entry


def _step_label(token: str) -> str:
    """Return the label the step dropdown shows for ``token``."""
    translations = json.loads(TRANSLATIONS.read_text(encoding="utf-8"))
    return translations["selector"]["target_temp_step"]["options"][token]


# -- setpoint -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("fake_trv", "requested"),
    [
        pytest.param(FAHRENHEIT_TRV, 70.0, id="fahrenheit_trv-heating"),
        pytest.param(FAHRENHEIT_TRV, 63.0, id="fahrenheit_trv-idle-63"),
        pytest.param(FAHRENHEIT_TRV, 64.0, id="fahrenheit_trv-idle-64"),
        pytest.param(
            OFF_GRID_FAHRENHEIT_TRV, 70.0, id="off_grid_fahrenheit_trv-heating"
        ),
        pytest.param(OFF_GRID_FAHRENHEIT_TRV, 64.0, id="off_grid_fahrenheit_trv-idle"),
    ],
    indirect=["fake_trv"],
)
async def test_setpoint_reaches_the_device_in_the_system_unit(
    hass, fake_trv, requested
):
    """A target asked for in Fahrenheit arrives at the device as that target.

    The user asks in the system unit, Better Thermostat computes in Celsius,
    and the write goes out in the system unit again. Whole degrees Fahrenheit
    sit on the grid of both devices, so the value that arrives is the one that
    was asked for, whichever way the room makes the thermostat round: up for a
    room calling for heat (70 °F), down for a room already warmer (63 and
    64 °F). A
    value on the grid is not moved by rounding in either direction.
    """
    profile = fake_trv.profile
    _publish_room_at_device_reading(hass, profile)
    await _start(hass, profile)
    dispatched = async_capture_events(hass, EVENT_CALL_SERVICE)

    with patch(WRITE_BUDGET, 0.0):
        await _set_target(hass, requested)
        assert await wait_for(
            hass,
            lambda: len(setpoint_commands(dispatched, TRV_ID)) > 0,
            timeout_seconds=2.0,
        )

    written = setpoint_commands(dispatched, TRV_ID)[-1][ATTR_TEMPERATURE]
    assert written == pytest.approx(requested)
    assert _device_setpoint(hass) == pytest.approx(requested)


OFF_GRID_FAHRENHEIT_TRV_TARGET_BASED = replace(
    OFF_GRID_FAHRENHEIT_TRV,
    name="off_grid_fahrenheit_trv_target_based",
    calibration="target_temp_based",
)
"""The Celsius head on a Fahrenheit system, calibrated through its setpoint."""


@pytest.mark.parametrize(
    ("fake_trv", "room", "requested"),
    [
        pytest.param(
            OFF_GRID_FAHRENHEIT_TRV_TARGET_BASED, 66.5, 70.0, id="target_temp_based"
        ),
        pytest.param(OFF_GRID_FAHRENHEIT_TRV, 68.0, 70.5, id="local_calibration"),
    ],
    indirect=["fake_trv"],
)
async def test_a_setpoint_published_in_whole_degrees_is_confirmed_and_not_adopted(
    hass, fake_trv, room, requested
):
    """A write the device publishes rounded to a whole degree is the write coming back.

    The head publishes no precision, so Home Assistant rounds the setpoint it
    holds to a whole degree Fahrenheit, and its half-degree Celsius step reads
    as half a degree Fahrenheit. A setpoint written on a half degree comes back
    half a degree away. That report confirms the write, and the head's next
    routine report leaves the room's target where the user put it.
    """
    profile = fake_trv.profile
    set_room_sensor(hass, room, UnitOfTemperature.FAHRENHEIT)
    bt = await _start(hass, profile)
    trv = bt.real_trvs[TRV_ID]

    with patch(WRITE_BUDGET, 0.0):
        baseline = len(fake_trv.set_temperature_calls)
        await _set_target(hass, requested)
        assert await wait_for(
            hass,
            lambda: len(fake_trv.set_temperature_calls) > baseline,
            timeout_seconds=2.0,
        )
        written = fake_trv.set_temperature_calls[-1]
        # The write sits on a half degree, which is the case under test.
        assert _fahrenheit(written) % 1.0 == pytest.approx(0.5, abs=0.06)
        assert await wait_for(
            hass,
            lambda: (
                trv.confirmed_setpoint is not None
                and trv.confirmed_setpoint == pytest.approx(written, abs=0.01)
            ),
            timeout_seconds=1.0,
        ), (trv.confirmed_setpoint, written, _device_setpoint(hass))
        assert await wait_for(
            hass, lambda: trv.target_temperature_received and not bt.ignore_states
        )
        # The reconciler sees the device where it was sent, not a lost write.
        assert not desired_diverges(
            bt, *compute_control_cycle(bt, record=False, commit=False)
        )

        # The head's next routine report carries the rounded setpoint again.
        fake_trv._attr_current_temperature = fake_trv.current_temperature + 0.3
        fake_trv.async_set_context(Context())
        fake_trv.async_write_ha_state()
        for _ in range(20):
            await hass.async_block_till_done()

    assert hass.states.get(BT_ENTITY).attributes[ATTR_TEMPERATURE] == requested


# -- bound clamping -----------------------------------------------------------


FAHRENHEIT_TRV_MIN_39 = replace(
    FAHRENHEIT_TRV, name="fahrenheit_trv_min_39", min_temp=39.0
)
"""The Fahrenheit device with a lower bound of 39 °F, which is 3.8889 °C."""


@pytest.mark.parametrize(
    ("fake_trv", "bound"),
    [
        pytest.param(FAHRENHEIT_TRV, "min_temp", id="fahrenheit_trv-min"),
        pytest.param(FAHRENHEIT_TRV, "max_temp", id="fahrenheit_trv-max"),
        pytest.param(FAHRENHEIT_TRV_MIN_39, "min_temp", id="fahrenheit_trv_min_39-min"),
        pytest.param(
            OFF_GRID_FAHRENHEIT_TRV, "min_temp", id="off_grid_fahrenheit_trv-min"
        ),
        pytest.param(
            OFF_GRID_FAHRENHEIT_TRV, "max_temp", id="off_grid_fahrenheit_trv-max"
        ),
    ],
    indirect=["fake_trv"],
)
async def test_a_target_at_the_edge_of_the_range_reaches_the_device(
    hass, fake_trv, bound
):
    """A target at the thermostat's own limit is a setpoint the device accepts.

    The thermostat offers the range its device publishes, and a user asking
    for its lowest or highest value is asking for the device's bound. The
    setpoint that goes out is clamped into the device's range, and it has to
    be inside the range the device really has, not the one Home Assistant
    rounded for display: Home Assistant checks the setpoint against the
    unrounded range and refuses one outside it before the device sees it.
    """
    profile = fake_trv.profile
    _publish_room_at_device_reading(hass, profile)
    await _start(hass, profile)
    await _settle_device_at(hass, 68.0)
    offered = hass.states.get(BT_ENTITY).attributes[bound]
    device_bound = getattr(profile, bound)

    with patch(WRITE_BUDGET, 0.0):
        baseline = len(fake_trv.set_temperature_calls)
        await _set_target(hass, offered)
        assert await wait_for(
            hass,
            lambda: len(fake_trv.set_temperature_calls) > baseline,
            timeout_seconds=2.0,
        ), f"no setpoint at {offered} °F reached the device"

    # In the device's own unit: within one published degree of its bound.
    received = fake_trv.set_temperature_calls[-1]
    assert received == pytest.approx(device_bound, abs=_published_degree(profile))


@pytest.mark.parametrize(
    "fake_trv",
    [
        pytest.param(
            replace(
                FAHRENHEIT_TRV,
                name="fahrenheit_trv_without_off",
                hvac_modes=(HVACMode.HEAT,),
            ),
            id="fahrenheit_trv",
        ),
        pytest.param(
            replace(
                OFF_GRID_FAHRENHEIT_TRV,
                name="off_grid_fahrenheit_trv_without_off",
                hvac_modes=(HVACMode.HEAT,),
            ),
            id="off_grid_fahrenheit_trv",
        ),
    ],
    indirect=True,
)
async def test_a_device_without_an_off_mode_is_parked_at_its_minimum(hass, fake_trv):
    """Switching off a device that cannot be switched off parks it at its minimum.

    A head that offers no off mode is turned off by the lowest setpoint it
    takes. That setpoint is the device's bound, so it has to be one the
    device accepts, or the head goes on heating a room that was switched off.
    """
    profile = fake_trv.profile
    _publish_room_at_device_reading(hass, profile)
    await _start(hass, profile)
    await _settle_device_at(hass, 68.0)

    with patch(WRITE_BUDGET, 0.0):
        baseline = len(fake_trv.set_temperature_calls)
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_hvac_mode",
            {ATTR_ENTITY_ID: BT_ENTITY, "hvac_mode": HVACMode.OFF},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: len(fake_trv.set_temperature_calls) > baseline,
            timeout_seconds=2.0,
        ), "no setpoint reached the device"

    received = fake_trv.set_temperature_calls[-1]
    assert received == pytest.approx(profile.min_temp, abs=_published_degree(profile))


FAHRENHEIT_TRV_WITHOUT_OFF = replace(
    FAHRENHEIT_TRV, name="fahrenheit_trv_without_off", hvac_modes=(HVACMode.HEAT,)
)
"""The Fahrenheit device offering no off mode, with its whole-degree grid."""


async def _start_without_off_mode(hass, profile: DeviceProfile):
    """Start a thermostat that runs ``profile`` as a ``no_off_system_mode`` head."""
    _publish_room_at_device_reading(hass, profile)
    data = copy.deepcopy(dict(make_entry(profile).data))
    data["thermostat"][0]["advanced"]["no_off_system_mode"] = True
    entry = MockConfigEntry(domain=DOMAIN, version=18, data=data, title=data["name"])
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, profile)
    return bt


CELSIUS_TRV_WITHOUT_OFF_ON_FAHRENHEIT = replace(
    GENERIC_HEAT_TRV,
    name="celsius_trv_without_off_on_fahrenheit",
    hvac_modes=(HVACMode.HEAT,),
    system_unit=UnitOfTemperature.FAHRENHEIT,
    configured_target_temperature_step="0.0",
)
"""A Celsius head without an off mode, published in whole degrees Fahrenheit."""


@pytest.mark.parametrize(
    "fake_trv",
    [
        pytest.param(FAHRENHEIT_TRV_WITHOUT_OFF, id="fahrenheit_trv"),
        pytest.param(CELSIUS_TRV_WITHOUT_OFF_ON_FAHRENHEIT, id="celsius_trv"),
    ],
    indirect=True,
)
async def test_a_device_parked_at_its_minimum_keeps_the_room_off(hass, fake_trv):
    """A head without an off mode, parked at the minimum, reads as off.

    Switching the room off parks the head at the lowest setpoint the
    thermostat writes. The head keeps reporting that setpoint, on the grid
    of a reading, and every such report is the head sitting at its minimum,
    not a user turning the room on.
    """
    bt = await _start_without_off_mode(hass, fake_trv.profile)
    await _settle_device_at(hass, 68.0)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_hvac_mode",
        {ATTR_ENTITY_ID: BT_ENTITY, "hvac_mode": HVACMode.OFF},
        blocking=True,
    )
    assert await wait_for(
        hass, lambda: _device_setpoint(hass) < 68.0, timeout_seconds=2.0
    )
    trv = bt.real_trvs[TRV_ID]
    assert await wait_for(
        hass, lambda: trv.target_temperature_received and not bt.ignore_states
    )

    # The head's next routine report carries the parked setpoint again.
    fake_trv._attr_current_temperature = fake_trv.current_temperature + 0.5
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()
    for _ in range(20):
        await hass.async_block_till_done()

    assert bt.bt_hvac_mode == HVACMode.OFF
    assert hass.states.get(BT_ENTITY).state == HVACMode.OFF


@pytest.mark.parametrize(
    "fake_trv", [FAHRENHEIT_TRV_WITHOUT_OFF], indirect=True, ids=["fahrenheit_trv"]
)
async def test_a_device_turned_down_to_its_own_minimum_switches_the_room_off(
    hass, fake_trv
):
    """Turning a head without an off mode to its end stop switches the room off.

    The head's own minimum, 41 °F, lies below the lowest setpoint the
    thermostat writes to it, which sits inside the whole degree Home
    Assistant may have rounded the bound to. A head turned down that far is
    at its minimum all the same.
    """
    bt = await _start_without_off_mode(hass, fake_trv.profile)
    await _settle_device_at(hass, 68.0)

    trv = bt.real_trvs[TRV_ID]
    assert await wait_for(
        hass, lambda: trv.target_temperature_received and not bt.ignore_states
    )

    # A turn at the device reaches Home Assistant as a state of its own,
    # outside the context of the thermostat's last write.
    fake_trv._attr_target_temperature = fake_trv.profile.min_temp
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()

    assert await wait_for(
        hass, lambda: bt.bt_hvac_mode == HVACMode.OFF, timeout_seconds=2.0
    )


# -- valve maintenance --------------------------------------------------------


@pytest.mark.parametrize(
    "fake_trv",
    [
        pytest.param(
            replace(FAHRENHEIT_TRV, valve_maintenance=True), id="fahrenheit_trv"
        ),
        pytest.param(OFF_GRID_FAHRENHEIT_TRV, id="off_grid_fahrenheit_trv"),
    ],
    indirect=True,
)
async def test_valve_maintenance_exercises_the_device_and_restores_its_setpoint(
    hass, fake_trv
):
    """A maintenance run drives the device to both bounds and hands it back.

    Maintenance exercises a valve by writing the device's maximum and minimum
    setpoint in turn, and ends by writing back the setpoint the device held
    before the run. Every one of those writes is a temperature, and all of
    them cross the unit conversion: the bounds out of the range the device
    publishes, the restore out of the setpoint it published.
    """
    profile = fake_trv.profile
    _publish_room_at_device_reading(hass, profile)
    await _start(hass, profile)
    await _settle_device_at(hass, 68.0)
    held = _device_setpoint(hass)
    baseline = len(fake_trv.set_temperature_calls)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RUN_VALVE_MAINTENANCE,
        {ATTR_ENTITY_ID: BT_ENTITY},
        blocking=True,
    )

    received = fake_trv.set_temperature_calls[baseline:]
    native_degree = _published_degree(profile)
    assert any(
        value == pytest.approx(profile.max_temp, abs=native_degree)
        for value in received
    ), received
    assert any(
        value == pytest.approx(profile.min_temp, abs=native_degree)
        for value in received
    ), received
    assert _device_setpoint(hass) == pytest.approx(held)


# -- preset numbers -----------------------------------------------------------


async def test_a_preset_temperature_set_in_fahrenheit_reaches_the_device(hass):
    """A preset temperature entered in Fahrenheit is the target the device gets.

    The preset number stores Celsius and Home Assistant converts what the user
    types into it; the active preset then drives the thermostat's target,
    which goes out converted back. 72 °F in, 72 °F at the device; the room is
    cooler than that, so the target is rounded up onto the device's grid.
    """
    (fake_trv,) = await build_devices(hass, FAHRENHEIT_TRV)
    profile = fake_trv.profile
    _publish_room_at_device_reading(hass, profile)
    data = dict(make_entry(profile).data) | {"presets": ["eco"]}
    entry = MockConfigEntry(domain=DOMAIN, version=18, data=data, title=data["name"])
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, profile)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_preset_mode",
        {ATTR_ENTITY_ID: BT_ENTITY, "preset_mode": "eco"},
        blocking=True,
    )

    with patch(WRITE_BUDGET, 0.0):
        baseline = len(fake_trv.set_temperature_calls)
        await hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: "number.bt_test_eco", "value": 72.0},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: any(
                value == pytest.approx(72.0)
                for value in fake_trv.set_temperature_calls[baseline:]
            ),
            timeout_seconds=2.0,
        ), fake_trv.set_temperature_calls[baseline:]

    assert float(hass.states.get("number.bt_test_eco").state) == pytest.approx(72.0)
    assert hass.states.get(BT_ENTITY).attributes[ATTR_TEMPERATURE] == pytest.approx(
        72.0
    )


# -- outdoor threshold --------------------------------------------------------


@pytest.mark.parametrize(
    ("outdoor", "heats"),
    [pytest.param(40.0, True, id="cold-40F"), pytest.param(60.0, False, id="mild-60F")],
)
async def test_a_stored_off_temperature_is_read_in_the_system_unit(
    hass, outdoor, heats
):
    """An outdoor threshold entered in Fahrenheit is compared as Fahrenheit.

    The entry stores the threshold the user typed, in the unit the user saw.
    50 °F is 10 °C: 40 °F outside is heating weather and 60 °F is not.
    """
    (fake_trv,) = await build_devices(hass, OFF_GRID_FAHRENHEIT_TRV)
    profile = fake_trv.profile
    _publish_room_at_device_reading(hass, profile)
    _publish_outdoor(hass, outdoor)
    bt = await _start(hass, profile, with_outdoor_sensor=True, off_temperature=50)

    # A reading on the same side of the threshold re-runs the outdoor check.
    _publish_outdoor(hass, outdoor + 0.5)
    assert await wait_for(
        hass,
        lambda: (
            bt.outdoor_source.damping is not None
            and bt.outdoor_source.damping.reading
            == pytest.approx((outdoor + 0.5 - 32) / 1.8, abs=0.01)
        ),
    )

    assert bt.off_temperature == pytest.approx(10.0)
    assert bt.call_for_heat is heats


# -- config flow defaults and labels ------------------------------------------


@pytest.mark.parametrize(
    ("unit_system", "suggested"),
    [
        pytest.param(METRIC_SYSTEM, 20, id="celsius"),
        pytest.param(US_CUSTOMARY_SYSTEM, 68, id="fahrenheit"),
    ],
)
async def test_the_flow_suggests_an_off_temperature_of_20_celsius(
    hass, unit_system, suggested
):
    """The suggested outdoor threshold is 20 °C, expressed in the system unit.

    The field takes the threshold in the unit the user sees, and that is how
    the thermostat reads it back. A suggestion is meant as the same
    temperature everywhere, so on a Fahrenheit system it has to be 68.
    """
    hass.config.units = unit_system
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert form_default(result, CONF_OFF_TEMPERATURE) == suggested


async def test_accepting_the_suggested_off_temperature_keeps_a_cold_room_heating(hass):
    """An entry created with the suggested threshold heats in heating weather.

    A user on a Fahrenheit system who wires an outdoor sensor and accepts the
    suggestion gets a thermostat that heats at 40 °F outside. The whole path
    runs: the form's own suggestion, the stored entry, and the threshold the
    thermostat compares the outdoor reading against.
    """
    (fake_trv,) = await build_devices(hass, OFF_GRID_FAHRENHEIT_TRV)
    profile = fake_trv.profile
    _publish_room_at_device_reading(hass, profile)
    _publish_outdoor(hass, 40.0)
    entry = await _run_create_flow(
        hass,
        {
            "name": "BT Test",
            CONF_THERMOSTAT: [TRV_ID],
            CONF_TEMPERATURE_SENSOR: SENSOR_ID,
            CONF_OUTDOOR_SENSOR: OUTDOOR_ID,
        },
    )
    bt = await wait_for_startup(hass, entry)

    # A reading on the same side of the threshold re-runs the outdoor check.
    _publish_outdoor(hass, 40.5)
    assert await wait_for(
        hass,
        lambda: (
            bt.outdoor_source.damping is not None
            and bt.outdoor_source.damping.reading
            == pytest.approx((40.5 - 32) / 1.8, abs=0.01)
        ),
    )

    assert bt.call_for_heat is True


async def test_configured_bounds_are_read_in_the_unit_their_label_names(hass):
    """Bounds picked from a dropdown labelled in Celsius are taken as Celsius.

    The range dropdown offers whole degrees Celsius on every system. The
    thermostat that comes up holds 16 to 24 °C, which its entity publishes as
    60.8 to 75.2 °F.
    """
    (fake_trv,) = await build_devices(hass, FAHRENHEIT_TRV)
    _publish_room_at_device_reading(hass, fake_trv.profile)
    entry = await _run_create_flow(
        hass,
        {
            "name": "BT Test",
            CONF_THERMOSTAT: [TRV_ID],
            CONF_TEMPERATURE_SENSOR: SENSOR_ID,
            CONF_TARGET_TEMP_MIN: "min_max_16",
            CONF_TARGET_TEMP_MAX: "min_max_24",
        },
    )
    bt = await wait_for_startup(hass, entry)

    assert (bt.min_temp, bt.max_temp) == (16.0, 24.0)
    state = hass.states.get(BT_ENTITY)
    # Published in whole degrees Fahrenheit: 61 and 75.
    assert state.attributes["min_temp"] == pytest.approx(_fahrenheit(16.0), abs=0.5)
    assert state.attributes["max_temp"] == pytest.approx(_fahrenheit(24.0), abs=0.5)


async def test_a_configured_step_is_read_in_the_unit_its_label_names(hass):
    """A step picked from a dropdown labelled in Celsius is a step in Celsius.

    The step and the range sit in the same form and carry the same unit in
    their labels, so both are read in it. The device's own grid of a whole
    degree Fahrenheit is coarser, so the device keeps writing on that.
    """
    (fake_trv,) = await build_devices(hass, FAHRENHEIT_TRV)
    _publish_room_at_device_reading(hass, fake_trv.profile)
    label = _step_label("step_0_5")
    labelled_value, labelled_unit = label.split(" ")
    assert labelled_unit == UnitOfTemperature.CELSIUS

    entry = await _run_create_flow(
        hass,
        {
            "name": "BT Test",
            CONF_THERMOSTAT: [TRV_ID],
            CONF_TEMPERATURE_SENSOR: SENSOR_ID,
            CONF_TARGET_TEMP_STEP: "step_0_5",
        },
    )
    bt = await wait_for_startup(hass, entry)

    assert bt._configured_temperature_step == pytest.approx(float(labelled_value))
    assert bt.real_trvs[TRV_ID].target_temp_step == pytest.approx(5 / 9, abs=1e-3)


@pytest.mark.parametrize(
    ("configured", "fake_trv", "published_step"),
    [
        pytest.param("step_0_5", FAHRENHEIT_TRV, 0.9, id="configured_0_5_celsius"),
        pytest.param(None, FAHRENHEIT_TRV, 1.0, id="device_step_1_fahrenheit"),
    ],
    indirect=["fake_trv"],
)
async def test_the_thermostat_publishes_its_step_in_the_system_unit(
    hass, configured, fake_trv, published_step
):
    """The step the thermostat publishes is a step in the unit its target is shown in.

    Home Assistant converts the target it publishes into Fahrenheit and
    publishes the step unconverted, and the frontend moves the target by
    that step. A step of 0.5 °C is 0.9 °F; the device's own 1 °F grid stays
    1 °F.
    """
    _publish_room_at_device_reading(hass, fake_trv.profile)
    user_input = {
        "name": "BT Test",
        CONF_THERMOSTAT: [TRV_ID],
        CONF_TEMPERATURE_SENSOR: SENSOR_ID,
    }
    if configured is not None:
        user_input[CONF_TARGET_TEMP_STEP] = configured
    entry = await _run_create_flow(hass, user_input)
    await wait_for_startup(hass, entry)

    state = hass.states.get(BT_ENTITY)
    assert state.attributes["target_temp_step"] == pytest.approx(published_step)


async def test_the_thermostat_publishes_its_range_in_tenths_inside_the_device_range(
    hass,
):
    """The range the thermostat publishes lies inside the device's, in tenths.

    The device publishes 39 and 87 °F for a range of 4 to 30.5 °C. The
    thermostat reads each half a published degree inward and publishes that
    as it holds it, 39.5 and 86.5 °F, not rounded back out to whole degrees.
    """
    (fake_trv,) = await build_devices(hass, OFF_GRID_FAHRENHEIT_TRV)
    _publish_room_at_device_reading(hass, fake_trv.profile)
    await _start(hass, fake_trv.profile)

    state = hass.states.get(BT_ENTITY)
    assert state.attributes["min_temp"] == 39.5
    assert state.attributes["max_temp"] == 86.5
    assert _fahrenheit(4.0) <= state.attributes["min_temp"]
    assert state.attributes["max_temp"] <= _fahrenheit(30.5)


async def test_a_target_from_the_fahrenheit_slider_lands_on_the_configured_step(hass):
    """A target set in Fahrenheit is held on the Celsius step the user configured.

    The frontend steps the target by 0.9 °F, the configured 0.5 °C, counted
    from 0 °F, so it offers 68.4 °F, which is 20.22 °C. The thermostat holds
    the point of its own step that is closest, 20 °C, and publishes it as
    68 °F.
    """
    (fake_trv,) = await build_devices(hass, FAHRENHEIT_TRV)
    _publish_room_at_device_reading(hass, fake_trv.profile)
    entry = await _run_create_flow(
        hass,
        {
            "name": "BT Test",
            CONF_THERMOSTAT: [TRV_ID],
            CONF_TEMPERATURE_SENSOR: SENSOR_ID,
            CONF_TARGET_TEMP_STEP: "step_0_5",
        },
    )
    bt = await wait_for_startup(hass, entry)

    with patch(WRITE_BUDGET, 0.0):
        await _set_target(hass, 68.4)

    assert bt.heat_target_temperature == pytest.approx(20.0)
    assert hass.states.get(BT_ENTITY).attributes[ATTR_TEMPERATURE] == 68.0


async def test_a_preset_number_steps_in_the_system_unit(hass):
    """A preset number steps by the configured step, shown in Fahrenheit.

    Home Assistant converts the number's value and range into Fahrenheit but
    publishes its step as the number gives it, so the number gives the step
    the thermostat publishes: 0.5 °C as 0.9 °F.
    """
    profile = replace(FAHRENHEIT_TRV, configured_target_temperature_step="0.5")
    (fake_trv,) = await build_devices(hass, profile)
    _publish_room_at_device_reading(hass, profile)
    data = dict(make_entry(profile).data) | {"presets": ["eco"]}
    entry = MockConfigEntry(domain=DOMAIN, version=18, data=data, title=data["name"])
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert hass.states.get("number.bt_test_eco").attributes["step"] == 0.9
    assert hass.states.get(BT_ENTITY).attributes["target_temp_step"] == 0.9
