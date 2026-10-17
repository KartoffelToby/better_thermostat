"""Real HA services and BT lifecycle against simulated Shelly actuator entities."""

from datetime import timedelta

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import Platform
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.controlling import control_trv

from .conftest import (
    SENSOR_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for,
    wait_for_startup,
)

ALGORITHMS = [
    "mpc_calibration",
    "mpc_v2_calibration",
    "pid_calibration",
    "tpi_calibration",
    "heating_power_calibration",
]
BT_ID = "climate.bt_test"
DEVICE = DeviceInfo(
    identifiers={("shelly", "blu-studio")},
    name="Studio",
    manufacturer="Shelly",
    model="SBTR-001AEU",
)


class ShellyClimate(FakeTrvEntity):
    _attr_unique_id = "blu-studio-climate"
    _attr_name = "Shelly studio"
    _attr_device_info = DEVICE
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(self):
        super().__init__()
        self._attr_target_temperature = None


class ShellyNumber(NumberEntity):
    _attr_should_poll = False
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_device_info = DEVICE

    def __init__(self, key="valve_position"):
        self._attr_unique_id = "blu-studio-" + key
        self._attr_translation_key = key
        self._attr_name = "Arbitrary " + key
        self._attr_native_value = 0
        self.calls = []

    async def async_set_native_value(self, value):
        self.calls.append(value)
        self._attr_native_value = value
        self.async_write_ha_state()


@pytest.fixture
async def shelly_device(hass):
    climate = ShellyClimate()
    valve = ShellyNumber()
    offset = ShellyNumber("local_temperature_calibration")

    async def setup(hass, entry):
        await hass.config_entries.async_forward_entry_setups(
            entry, [Platform.CLIMATE, Platform.NUMBER]
        )
        return True

    async def unload(hass, entry):
        return await hass.config_entries.async_unload_platforms(
            entry, [Platform.CLIMATE, Platform.NUMBER]
        )

    async def add_climate(hass, entry, add_entities):
        add_entities([climate])

    async def add_numbers(hass, entry, add_entities):
        add_entities([valve, offset])

    mock_integration(
        hass, MockModule("shelly", async_setup_entry=setup, async_unload_entry=unload)
    )
    mock_platform(hass, "shelly.climate", MockPlatform(async_setup_entry=add_climate))
    mock_platform(hass, "shelly.number", MockPlatform(async_setup_entry=add_numbers))
    mock_platform(hass, "shelly.config_flow")
    gateway = MockConfigEntry(domain="shelly")
    gateway.add_to_hass(hass)
    with mock_config_flow("shelly", ConfigFlow):
        assert await hass.config_entries.async_setup(gateway.entry_id)
        await hass.async_block_till_done()
        yield climate, valve, offset


async def start_bt(hass, climate, algorithm):
    hass.states.async_set(SENSOR_ID, "18", {"unit_of_measurement": "°C"})
    entry = make_entry()
    data = dict(entry.data)
    data["thermostat"] = [
        dict(item, advanced=dict(item["advanced"])) for item in entry.data["thermostat"]
    ]
    data["thermostat"][0].update(
        trv=climate.entity_id,
        integration="shelly",
        model="SBTR-001AEU",
        advanced={
            "calibration": "direct_valve_based",
            "calibration_mode": algorithm,
            "valve_maintenance": True,
        },
    )
    entry = MockConfigEntry(
        domain="better_thermostat", version=18, data=data, title="BT Test"
    )
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await bt.control_queue_task.join()
    return entry, bt


async def service(hass, name, **data):
    await hass.services.async_call(
        "climate", name, {"entity_id": BT_ID, **data}, blocking=True
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("algorithm", ALGORITHMS)
async def test_actual_algorithms_control_only_valve_after_standard_startup(
    hass, shelly_device, algorithm
):
    climate, valve, offset = shelly_device
    entry, bt = await start_bt(hass, climate, algorithm)
    # Standard BT startup resets the physical offset; regulation must not write it.
    assert offset.calls == [0]
    trv = bt.real_trvs[climate.entity_id]
    assert trv.exclusive_direct_valve
    assert climate.target_temperature is None
    await service(hass, "set_temperature", temperature=23)
    await bt.control_queue_task.join()
    assert await wait_for(hass, lambda: valve.calls and valve.calls[-1] > 0)
    assert trv.calibration_balance["apply_valve"] is True
    assert trv.calibration_balance["valve_percent"] == valve.calls[-1]
    assert all(0 <= value <= 100 for value in valve.calls)

    # The physical device offers no OFF mode, yet UI OFF must close the number.
    await service(hass, "set_hvac_mode", hvac_mode="off")
    await bt.control_queue_task.join()
    assert valve.calls[-1] == 0
    assert not climate.set_temperature_calls
    assert not climate.set_hvac_mode_calls
    assert offset.calls == [0]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_contacts_boost_telemetry_maintenance_reload_and_manual_handover(
    hass, shelly_device
):
    climate, valve, offset = shelly_device
    entry, bt = await start_bt(hass, climate, "heating_power_calibration")
    assert offset.calls == [0]
    trv = bt.real_trvs[climate.entity_id]
    await service(hass, "set_temperature", temperature=23)
    await bt.control_queue_task.join()
    trv.valve_max_opening = 61
    await service(hass, "set_preset_mode", preset_mode="boost")
    await bt.control_queue_task.join()
    assert valve.calls[-1] == 61

    # Exercise both contact inputs through the existing logical contact property.
    for contact in ("window_open", "door_open"):
        setattr(bt, contact, True)
        before = len(valve.calls)
        await control_trv(bt, climate.entity_id)
        assert valve.calls[before:] == [0]
        setattr(bt, contact, False)
    bt.call_for_heat = False
    await control_trv(bt, climate.entity_id)
    assert valve.calls[-1] == 0
    bt.call_for_heat = True

    target, mode = bt.bt_target_temp, bt.bt_hvac_mode
    bt.last_internal_sensor_change = dt_util.now() - timedelta(seconds=10)
    climate._attr_target_temperature = 5
    climate._attr_hvac_mode = HVACMode.OFF
    climate._attr_current_temperature = 24
    climate.async_write_ha_state()
    assert await wait_for(hass, lambda: trv.current_temperature == 24)
    await bt.control_queue_task.join()
    assert bt.bt_target_temp == target
    assert bt.bt_hvac_mode == mode

    await bt._run_valve_maintenance([climate.entity_id])
    await bt.control_queue_task.join()
    assert not climate.set_temperature_calls
    assert not climate.set_hvac_mode_calls
    assert offset.calls == [0]

    assert await hass.config_entries.async_reload(entry.entry_id)
    bt = await wait_for_startup(hass, entry)
    await bt.control_queue_task.join()
    assert bt.real_trvs[climate.entity_id].exclusive_direct_valve
    assert not climate.set_temperature_calls
    assert not climate.set_hvac_mode_calls
    # Reload repeats standard initialization without adding control-loop offsets.
    assert offset.calls == [0, 0]

    # Simulate the user's manual re-enable of the Shelly thermostat, then
    # reload a normal BT configuration. The capability must no longer block it.
    climate._attr_hvac_mode = HVACMode.HEAT
    climate._attr_target_temperature = 20
    climate.async_write_ha_state()
    data = dict(entry.data)
    data["thermostat"] = [
        dict(item, advanced=dict(item["advanced"])) for item in entry.data["thermostat"]
    ]
    data["thermostat"][0]["advanced"].update(
        calibration="target_temp_based", calibration_mode=CalibrationMode.NO_CALIBRATION
    )
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)
    await service(hass, "set_temperature", temperature=25)
    assert await wait_for(hass, lambda: climate.set_temperature_calls)
    assert not bt.real_trvs[climate.entity_id].exclusive_direct_valve
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_maintenance_with_missing_valve_never_falls_back_to_climate(
    hass, shelly_device
):
    climate, valve, offset = shelly_device
    entry, bt = await start_bt(hass, climate, "heating_power_calibration")
    await service(hass, "set_hvac_mode", hvac_mode="off")
    await bt.control_queue_task.join()
    before = list(valve.calls)
    hass.states.async_remove(valve.entity_id)
    await bt._run_valve_maintenance([climate.entity_id])
    assert valve.calls == before
    assert not climate.set_temperature_calls
    assert not climate.set_hvac_mode_calls
    assert offset.calls == [0]
    assert not bt.real_trvs[climate.entity_id].ignore_trv_states
    assert await hass.config_entries.async_unload(entry.entry_id)
