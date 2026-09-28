"""A thermostat on a Fahrenheit system reads and writes in the unit it sees.

Home Assistant publishes every climate temperature in the system unit, rounded
to the entity's precision, and converts a setpoint written through the climate
service back into the device's own unit before checking it against the
device's unrounded range. Better Thermostat computes in Celsius in between, so
every value crosses a conversion twice, and a value that is only right for
round numbers or for Celsius systems gives itself away here.
"""

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_conversion import TemperatureConverter
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)
import voluptuous as vol

from custom_components.better_thermostat.utils.const import (
    CONF_OFF_TEMPERATURE,
    SERVICE_RUN_VALVE_MAINTENANCE,
)

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    TRV_ID,
    FakeTrvEntity,
    setup_entry,
    wait_for,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"


def _fahrenheit(celsius: float) -> float:
    return TemperatureConverter.convert(
        celsius, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
    )


async def _register_trv(
    hass,
    *,
    unit: UnitOfTemperature,
    min_temp: float,
    max_temp: float,
    step: float,
    current: float,
    target: float,
    hvac_modes=(HVACMode.HEAT, HVACMode.OFF),
) -> FakeTrvEntity:
    """Register a TRV that works in ``unit`` on the current system."""
    entity = FakeTrvEntity()
    entity._attr_temperature_unit = unit
    entity._attr_min_temp = min_temp
    entity._attr_max_temp = max_temp
    entity._attr_target_temperature_step = step
    entity._attr_current_temperature = current
    entity._attr_target_temperature = target
    entity._attr_hvac_modes = list(hvac_modes)
    if HVACMode.OFF not in hvac_modes:
        entity._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [entity])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    assert hass.states.get(TRV_ID) is not None
    return entity


async def _fahrenheit_trv(hass, **overrides) -> FakeTrvEntity:
    """A TRV working in whole degrees Fahrenheit from 41 to 86 °F."""
    spec = {
        "unit": UnitOfTemperature.FAHRENHEIT,
        "min_temp": 41.0,
        "max_temp": 86.0,
        "step": 1.0,
        "current": 67.0,
        "target": 68.0,
    } | overrides
    return await _register_trv(hass, **spec)


async def _off_grid_trv(hass) -> FakeTrvEntity:
    """A Celsius TRV whose range, 4 to 30.5 °C, lies on no whole degree Fahrenheit."""
    return await _register_trv(
        hass,
        unit=UnitOfTemperature.CELSIUS,
        min_temp=4.0,
        max_temp=30.5,
        step=0.5,
        current=19.5,
        target=20.0,
    )


def _publish_room(hass, fahrenheit: float) -> None:
    hass.states.async_set(
        SENSOR_ID,
        str(fahrenheit),
        {"unit_of_measurement": UnitOfTemperature.FAHRENHEIT},
    )


def _entry(*, step="0.0", no_off=False, valve_maintenance=False) -> MockConfigEntry:
    data = {
        "name": "BT Test",
        "thermostat": [
            {
                "trv": TRV_ID,
                "integration": "generic_thermostat",
                "model": "Generic",
                "advanced": {
                    "calibration": "target_temp_based",
                    "calibration_mode": "default",
                    "no_off_system_mode": no_off,
                    "valve_maintenance": valve_maintenance,
                },
            }
        ],
        "temperature_sensor": SENSOR_ID,
        "model": "Generic",
        "target_temp_step": step,
        "tolerance": 0.0,
        "off_temperature": 50,
    }
    return MockConfigEntry(domain=DOMAIN, version=18, data=data, title="BT Test")


async def _start(hass, **entry_options):
    entry = _entry(**entry_options)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


async def _set_target(hass, fahrenheit: float) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {ATTR_ENTITY_ID: BT_ENTITY, ATTR_TEMPERATURE: fahrenheit},
        blocking=True,
    )


def _form_default(form, key):
    for marker in form["data_schema"].schema:
        if marker == key:
            assert marker.default is not vol.UNDEFINED
            return marker.default()
    raise AssertionError(f"the form has no field {key!r}")


@pytest.fixture
def fahrenheit_system(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM


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
    the thermostat reads it back, so on a Fahrenheit system the suggestion is
    68.
    """
    hass.config.units = unit_system
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert _form_default(result, CONF_OFF_TEMPERATURE) == suggested


@pytest.mark.usefixtures("fahrenheit_system")
async def test_a_configured_step_is_a_celsius_step_published_in_fahrenheit(hass):
    """A step picked as 0.5 °C is the device's grid and is shown as 0.9 °F.

    The step dropdown is labelled in Celsius, like the range next to it, so
    it is read as Celsius. Home Assistant publishes the step unconverted next
    to a target it converted, so the thermostat states it in Fahrenheit.
    """
    await _fahrenheit_trv(hass)
    _publish_room(hass, 67.0)
    bt = await _start(hass, step="0.5")

    assert bt.real_trvs[TRV_ID].target_temp_step == pytest.approx(0.5)
    assert hass.states.get(BT_ENTITY).attributes["target_temp_step"] == 0.9


@pytest.mark.usefixtures("fahrenheit_system")
async def test_a_target_from_the_fahrenheit_slider_lands_on_the_configured_step(hass):
    """A target set in Fahrenheit is held on the Celsius step the user configured.

    The frontend steps the target by 0.9 °F from 0 °F, so it offers 68.4 °F,
    20.22 °C. The thermostat holds the closest point of its own step, 20 °C,
    and publishes it as 68 °F.
    """
    await _fahrenheit_trv(hass)
    _publish_room(hass, 67.0)
    bt = await _start(hass, step="0.5")

    await _set_target(hass, 68.4)

    assert bt.bt_target_temp == pytest.approx(20.0)
    assert hass.states.get(BT_ENTITY).attributes[ATTR_TEMPERATURE] == 68.0


@pytest.mark.usefixtures("fahrenheit_system")
async def test_the_range_is_published_in_tenths_inside_the_device_range(hass):
    """The range the thermostat publishes lies inside the device's, in tenths.

    The device's 4 to 30.5 °C is published as 39 and 87 °F. The thermostat
    reads each half a published degree inward and publishes that as it holds
    it, 39.5 and 86.5 °F, not rounded back out to whole degrees.
    """
    await _off_grid_trv(hass)
    _publish_room(hass, 67.0)
    await _start(hass)

    state = hass.states.get(BT_ENTITY)
    assert state.attributes["min_temp"] == 39.5
    assert state.attributes["max_temp"] == 86.5
    assert _fahrenheit(4.0) <= state.attributes["min_temp"]
    assert state.attributes["max_temp"] <= _fahrenheit(30.5)


@pytest.mark.usefixtures("fahrenheit_system")
@pytest.mark.parametrize(
    ("bound", "device_bound"), [("min_temp", 4.0), ("max_temp", 30.5)]
)
async def test_a_target_at_the_edge_of_the_range_reaches_the_device(
    hass, bound, device_bound
):
    """A target at the thermostat's own limit is a setpoint the device accepts.

    Home Assistant checks the setpoint against the device's unrounded range
    and refuses one outside it before the device sees it, so the limit the
    thermostat offers has to lie inside that range.
    """
    fake_trv = await _off_grid_trv(hass)
    _publish_room(hass, 67.0)
    await _start(hass)
    await _set_target(hass, 68.0)
    # Away from both bounds first, so the write at the bound is a change.
    assert await wait_for(
        hass, lambda: 10.0 < fake_trv.target_temperature < 25.0, timeout_s=2.0
    ), fake_trv.set_temperature_calls
    offered = hass.states.get(BT_ENTITY).attributes[bound]
    baseline = len(fake_trv.set_temperature_calls)

    await _set_target(hass, offered)
    assert await wait_for(
        hass, lambda: len(fake_trv.set_temperature_calls) > baseline, timeout_s=2.0
    ), f"no setpoint at {offered} °F reached the device"

    assert fake_trv.set_temperature_calls[-1] == pytest.approx(device_bound, abs=0.5)


@pytest.mark.usefixtures("fahrenheit_system")
@pytest.mark.parametrize("device", ["fahrenheit", "off_grid"])
async def test_valve_maintenance_exercises_the_device_and_restores_its_setpoint(
    hass, device
):
    """A maintenance run drives the device to both bounds and hands it back.

    Every write of the run crosses the unit conversion: the bounds out of the
    range the device publishes, the restore out of the setpoint it published.
    """
    if device == "fahrenheit":
        fake_trv = await _fahrenheit_trv(hass)
        min_temp, max_temp, tolerance = 41.0, 86.0, 1.0
    else:
        fake_trv = await _off_grid_trv(hass)
        min_temp, max_temp, tolerance = 4.0, 30.5, 0.5
    _publish_room(hass, 67.0)
    await _start(hass, valve_maintenance=True)
    await _set_target(hass, 68.0)
    assert await wait_for(
        hass,
        lambda: float(hass.states.get(TRV_ID).attributes[ATTR_TEMPERATURE]) == 68.0,
        timeout_s=2.0,
    )
    held = hass.states.get(TRV_ID).attributes[ATTR_TEMPERATURE]
    baseline = len(fake_trv.set_temperature_calls)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RUN_VALVE_MAINTENANCE,
        {ATTR_ENTITY_ID: BT_ENTITY},
        blocking=True,
    )

    received = fake_trv.set_temperature_calls[baseline:]
    assert any(v == pytest.approx(max_temp, abs=tolerance) for v in received), received
    assert any(v == pytest.approx(min_temp, abs=tolerance) for v in received), received
    assert hass.states.get(TRV_ID).attributes[ATTR_TEMPERATURE] == pytest.approx(held)
