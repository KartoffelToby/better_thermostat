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
from homeassistant.util.unit_conversion import TemperatureConverter
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.better_thermostat.utils.const import (
    CONF_HEATER,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_TARGET_TEMP_MAX,
    CONF_TARGET_TEMP_MIN,
    CONF_TARGET_TEMP_STEP,
    SERVICE_RUN_VALVE_MAINTENANCE,
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
            timeout_s=2.0,
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
            hass, lambda: len(setpoint_commands(dispatched, TRV_ID)) > 0, timeout_s=2.0
        )

    written = setpoint_commands(dispatched, TRV_ID)[-1][ATTR_TEMPERATURE]
    assert written == pytest.approx(requested)
    assert _device_setpoint(hass) == pytest.approx(requested)


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
            hass, lambda: len(fake_trv.set_temperature_calls) > baseline, timeout_s=2.0
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
            hass, lambda: len(fake_trv.set_temperature_calls) > baseline, timeout_s=2.0
        ), "no setpoint reached the device"

    received = fake_trv.set_temperature_calls[-1]
    assert received == pytest.approx(profile.min_temp, abs=_published_degree(profile))


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
            timeout_s=2.0,
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
    assert await wait_for(hass, lambda: bt.last_avg_outdoor_temp is not None)

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
            CONF_HEATER: [TRV_ID],
            CONF_SENSOR: SENSOR_ID,
            CONF_OUTDOOR_SENSOR: OUTDOOR_ID,
        },
    )
    bt = await wait_for_startup(hass, entry)

    # A reading on the same side of the threshold re-runs the outdoor check.
    _publish_outdoor(hass, 40.5)
    assert await wait_for(hass, lambda: bt.last_avg_outdoor_temp is not None)

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
            CONF_HEATER: [TRV_ID],
            CONF_SENSOR: SENSOR_ID,
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
    their labels, so both are read in it. The configured step overrides the
    device's own grid, so it is the grid every setpoint write is rounded to.
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
            CONF_HEATER: [TRV_ID],
            CONF_SENSOR: SENSOR_ID,
            CONF_TARGET_TEMP_STEP: "step_0_5",
        },
    )
    bt = await wait_for_startup(hass, entry)

    assert bt.real_trvs[TRV_ID].target_temp_step == pytest.approx(float(labelled_value))
