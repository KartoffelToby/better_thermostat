"""Harness for tests against a real Home Assistant instance.

The unit suite mocks the entity; everything here sets up a real config
entry against a real (simulated) climate entity so the wiring across
the layers — entity lifecycle, queues, kernel, adapters, services — is
exercised end to end.

The simulated devices are built from the profiles in ``device_profiles``:
``fake_trv`` and ``device_role`` are parametrized indirectly to move the
whole harness onto another device form, and ``make_entry`` derives the
matching config entry from the same profile.
"""

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
    setup_test_component_platform,
)

# Pin the repository's custom_components namespace package before the
# hass fixture mounts the plugin's testing config dir: that dir carries
# a regular custom_components package which would otherwise shadow the
# repository and make the loader report "Integration not found".
import custom_components.better_thermostat  # noqa: F401  isort: skip

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DOMAIN as CLIMATE_DOMAIN,
    ClimateEntity,
    HVACMode,
)
from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.setup import async_setup_component
from homeassistant.util.unit_conversion import TemperatureConverter
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .device_profiles import (
    COOLER_ID,
    GENERIC_HEAT_TRV,
    HEAT_ONLY,
    OFFSET_NUMBER_ID,
    TRV_ID,
    DeviceProfile,
    OffsetChannel,
    RoleScenario,
)

DOMAIN = "better_thermostat"
SENSOR_ID = "sensor.room_temperature"
WINDOW_ID = "binary_sensor.window"

# The entity every test drives, derived from the default entry title.
BT_ENTITY = "climate.bt_test"

# The integration the simulated devices belong to when they need a device
# registry entry, and the device all of them share.
DEVICE_INTEGRATION = "test"
DEVICE_IDENTIFIERS = {(DEVICE_INTEGRATION, "fake_device")}

# What the test modules import from the harness. The entity ids and the
# profile types are re-exported so a test needs one import, not two.
__all__ = [
    "BT_ENTITY",
    "COOLER_ID",
    "DEVICE_INTEGRATION",
    "DOMAIN",
    "OFFSET_NUMBER_ID",
    "SENSOR_ID",
    "TRV_ID",
    "WINDOW_ID",
    "DeviceProfile",
    "OffsetChannel",
    "RoleScenario",
    "SimulatedClimate",
    "SimulatedOffsetNumber",
    "assert_on_device_grid",
    "assert_profile_adopted",
    "build_devices",
    "make_entry",
    "profile_id",
    "set_room_sensor",
    "setup_entry",
    "wait_for",
    "wait_for_startup",
]


@pytest.fixture(autouse=True)
async def _recorder(recorder_mock):
    """Provide the recorder the integration's manifest depends on.

    Must be instantiated before anything pulls up the hass fixture.
    """
    return


@pytest.fixture(autouse=True)
def _enable_custom_integrations(_recorder, enable_custom_integrations):
    """Let Home Assistant load custom_components from the repository."""
    return


@pytest.fixture(autouse=True)
def _compressed_sleeps():
    """Compress asyncio sleeps so the entity runs in test time.

    The integration sleeps real intervals (startup settle, write
    propagation, poll loops). The replacement keeps the scheduling
    semantics — it always yields to the event loop — without the wall
    time.
    """
    real_sleep = asyncio.sleep

    async def fast(delay, result=None, **kwargs):
        await real_sleep(0.005 if delay and delay > 0 else 0)
        return result

    with patch("asyncio.sleep", new=fast):
        yield


@dataclass
class _ActiveDevices:
    """The devices a bare ``make_entry()`` builds its entry for.

    A test that parametrizes its fixture and then calls ``make_entry()``
    without arguments would otherwise get an entry for the default device,
    which silently flattens the axis it parametrized. ``build_devices`` and
    ``device_role`` point this holder at the devices they registered;
    ``_reset_active_devices`` bounds it to one test.
    """

    profile: DeviceProfile = GENERIC_HEAT_TRV
    cooler_entity_id: str | None = None


_ACTIVE = _ActiveDevices()


@pytest.fixture(autouse=True)
def _reset_active_devices():
    """Point the ambient device state back at the default profile."""
    _ACTIVE.profile = GENERIC_HEAT_TRV
    _ACTIVE.cooler_entity_id = None


class SimulatedClimate(ClimateEntity):
    """A simulated climate device, built from a device profile.

    Commands arrive through the real climate services and are confirmed
    into the entity state, like a device that applies every write. The
    recorded calls are the assertion surface. Nothing here rejects a
    command: a device only refuses what Home Assistant's own validation
    refuses on the strength of the published capabilities.

    ``offset_number`` is the calibration entity of a device whose offset
    channel is a number entity, and ``None`` for every other device.
    """

    _attr_should_poll = False

    def __init__(self, profile: DeviceProfile):
        """Publish the profile's capabilities and open the assertion surface."""
        self.profile = profile
        self.offset_number: SimulatedOffsetNumber | None = None
        self._attr_name = profile.entity_name
        self._attr_temperature_unit = profile.temperature_unit
        self._attr_hvac_modes = list(profile.hvac_modes)
        self._attr_supported_features = profile.supported_features
        self._attr_min_temp = profile.min_temp
        self._attr_max_temp = profile.max_temp
        self._attr_target_temperature_step = profile.target_temperature_step
        self._attr_hvac_mode = profile.hvac_mode
        self._attr_current_temperature = profile.current_temperature
        self._attr_target_temperature = profile.target_temperature
        if profile.precision is not None:
            self._attr_precision = profile.precision
        if profile.has_device_registry_entry:
            self._attr_unique_id = f"{_object_id(profile.entity_id)}_climate"
            self._attr_device_info = DeviceInfo(identifiers=DEVICE_IDENTIFIERS)
        if profile.offset_channel is OffsetChannel.ECOSYSTEM_SERVICE:
            # The ecosystem adapter reads the current offset off the climate
            # entity itself instead of a separate calibration entity.
            self._attr_extra_state_attributes = {"offset_celsius": 0.0}
        self.set_temperature_calls: list[float | dict[str, float]] = []
        self.set_hvac_mode_calls: list[str] = []
        # Radio loss simulation: the next setpoint write is recorded but
        # neither applied nor confirmed.
        self.drop_next_setpoint_write = False

    async def async_set_temperature(self, **kwargs) -> None:
        """Apply and confirm a setpoint write.

        The write is always recorded; with ``drop_next_setpoint_write``
        set it is then swallowed instead of applied, simulating a device
        that lost the command over the radio. A ranged write is recorded
        as a dict of both bounds.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            low = kwargs.get(ATTR_TARGET_TEMP_LOW)
            high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
            self.set_temperature_calls.append(
                {ATTR_TARGET_TEMP_LOW: low, ATTR_TARGET_TEMP_HIGH: high}
            )
            if self.drop_next_setpoint_write:
                self.drop_next_setpoint_write = False
                return
            self._attr_target_temperature_low = low
            self._attr_target_temperature_high = high
            self.async_write_ha_state()
            return
        self.set_temperature_calls.append(temperature)
        if self.drop_next_setpoint_write:
            self.drop_next_setpoint_write = False
            return
        self._attr_target_temperature = temperature
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode) -> None:
        """Apply and confirm a mode write."""
        self.set_hvac_mode_calls.append(str(hvac_mode))
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()


class SimulatedOffsetNumber(NumberEntity):
    """The calibration number a device exposes next to its climate entity.

    It sits on the same device as the climate entity and carries the
    translation key calibration discovery looks for. It publishes no device
    class on purpose: a temperature device class would make Home Assistant
    convert the native value, so a read back would not be what was written.
    """

    _attr_should_poll = False
    _attr_name = "local temperature calibration"
    _attr_translation_key = "local_temperature_calibration"
    _attr_native_min_value = -12.0
    _attr_native_max_value = 12.0
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(self, profile: DeviceProfile):
        """Attach the calibration entity to the profile's device."""
        self._attr_unique_id = (
            f"{_object_id(profile.entity_id)}_local_temperature_calibration"
        )
        self._attr_device_info = DeviceInfo(identifiers=DEVICE_IDENTIFIERS)
        self._attr_native_value = 0.0
        self.set_value_calls: list[float] = []

    async def async_set_native_value(self, value: float) -> None:
        """Apply and confirm an offset write."""
        self.set_value_calls.append(value)
        self._attr_native_value = value
        self.async_write_ha_state()


def _object_id(entity_id: str) -> str:
    """Return the object id half of ``entity_id``."""
    return entity_id.split(".", 1)[1]


async def build_devices(hass, *profiles: DeviceProfile) -> list[SimulatedClimate]:
    """Register one simulated device per profile and return the entities.

    The first profile becomes the ambient one a bare ``make_entry()`` builds
    its entry for. Profiles must agree on ``has_device_registry_entry``: the
    two registration routes are mutually exclusive.
    """
    if not profiles:
        raise ValueError("build_devices needs at least one profile")
    with_device = profiles[0].has_device_registry_entry
    if any(p.has_device_registry_entry is not with_device for p in profiles):
        raise ValueError("profiles must agree on has_device_registry_entry")
    if sum(p.offset_channel is OffsetChannel.NUMBER_ENTITY for p in profiles) > 1:
        raise ValueError("only one device may carry a calibration number entity")

    _ACTIVE.profile = profiles[0]

    # The system unit is captured into the entity at entry setup and is the
    # fallback behind every unit resolution, so it has to be in place before
    # the devices exist — a device unit alone changes nothing Better
    # Thermostat can observe, because a climate entity publishes no unit.
    if profiles[0].temperature_unit is UnitOfTemperature.FAHRENHEIT:
        hass.config.units = US_CUSTOMARY_SYSTEM

    entities = []
    numbers = []
    for profile in profiles:
        entity = SimulatedClimate(profile)
        # Pinned before adding: without it a device-backed entity is
        # registered under an id derived from the device name.
        entity.entity_id = profile.entity_id
        if profile.offset_channel is OffsetChannel.NUMBER_ENTITY:
            number = SimulatedOffsetNumber(profile)
            number.entity_id = OFFSET_NUMBER_ID
            entity.offset_number = number
            numbers.append(number)
        entities.append(entity)

    if with_device:
        await _add_devices_from_config_entry(hass, entities, numbers)
    else:
        await _add_devices_from_yaml(hass, entities)

    await hass.async_block_till_done()
    for entity in entities:
        assert hass.states.get(entity.entity_id) is not None
    for number in numbers:
        assert hass.states.get(number.entity_id) is not None
    return entities


async def _add_devices_from_yaml(hass, entities) -> None:
    """Add the entities through a YAML platform: no registry row, no device."""
    setup_test_component_platform(hass, CLIMATE_DOMAIN, entities)
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )


async def _add_devices_from_config_entry(hass, entities, numbers) -> None:
    """Add the entities through a config entry, so they get a device.

    Every line is load-bearing: without the mocked config flow platform the
    setup fails with "Platform test.config_flow not found", and without the
    mocked flow handler with "Flow handler not found".
    """
    platforms = [CLIMATE_DOMAIN] + ([NUMBER_DOMAIN] if numbers else [])

    async def async_setup_entry(hass, entry):
        await hass.config_entries.async_forward_entry_setups(entry, platforms)
        return True

    mock_integration(
        hass, MockModule(DEVICE_INTEGRATION, async_setup_entry=async_setup_entry)
    )
    mock_platform(hass, f"{DEVICE_INTEGRATION}.config_flow", None)
    setup_test_component_platform(
        hass, CLIMATE_DOMAIN, entities, from_config_entry=True
    )
    if numbers:
        setup_test_component_platform(
            hass, NUMBER_DOMAIN, numbers, from_config_entry=True
        )
    device_entry = MockConfigEntry(domain=DEVICE_INTEGRATION)
    device_entry.add_to_hass(hass)
    with mock_config_flow(DEVICE_INTEGRATION, ConfigFlow):
        assert await hass.config_entries.async_setup(device_entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def fake_trv(hass, request, _reset_active_devices):
    """Register a fake TRV with the real climate component.

    Parametrize indirectly with a ``DeviceProfile`` to move the test onto
    another device form.
    """
    profile = getattr(request, "param", GENERIC_HEAT_TRV)
    (entity,) = await build_devices(hass, profile)
    return entity


@pytest.fixture
async def device_role(hass, request, _reset_active_devices):
    """Register the devices of a role scenario and wire the entry to them.

    Parametrize indirectly with a ``RoleScenario``. The returned entities are
    the thermostat first, then a separate cooler if the scenario has one; a
    dual-role device yields a single entity that the entry names twice.
    """
    scenario: RoleScenario = getattr(request, "param", HEAT_ONLY)
    profiles = [scenario.trv]
    if scenario.cooler is not None:
        profiles.append(scenario.cooler)
    entities = await build_devices(hass, *profiles)
    _ACTIVE.cooler_entity_id = scenario.cooler_entity_id
    return entities


def make_entry(
    *,
    profile: DeviceProfile | None = None,
    with_window: bool = False,
    name: str = "BT Test",
    cooler: str | None = None,
    heat_auto_swapped: bool = False,
) -> MockConfigEntry:
    """Build a config entry matching the current entry schema.

    ``profile`` and ``cooler`` default to the devices most recently built,
    so a test that parametrizes its fixture gets the matching entry without
    naming the device twice.
    """
    profile = profile or _ACTIVE.profile
    if cooler is None:
        cooler = _ACTIVE.cooler_entity_id
    data = {
        "name": name,
        "thermostat": [
            {
                "trv": profile.entity_id,
                "integration": profile.integration,
                "model": "Generic",
                "advanced": {
                    "calibration": profile.calibration,
                    "calibration_mode": "default",
                    "no_off_system_mode": False,
                    "heat_auto_swapped": heat_auto_swapped,
                },
            }
        ],
        "temperature_sensor": SENSOR_ID,
        "model": "Generic",
        "target_temp_step": profile.configured_target_temp_step,
        "tolerance": 0.3,
        "off_temperature": 5,
    }
    if cooler is not None:
        data["cooler"] = cooler
    if with_window:
        data["window_sensors"] = WINDOW_ID
        data["window_off_delay"] = 0
        data["window_off_delay_after"] = 0
    return MockConfigEntry(domain=DOMAIN, version=18, data=data, title=name)


async def setup_entry(hass, entry) -> None:
    """Set the entry up and let the startup sequence settle."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def wait_for(hass, predicate, timeout_s=10.0) -> bool:
    """Yield to the loop until ``predicate()`` is true or time runs out."""
    deadline = hass.loop.time() + timeout_s
    while hass.loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0)
        await hass.async_block_till_done()
    return predicate()


async def wait_for_startup(hass, entry):
    """Return the BT entity once its startup task has fully finished.

    The startup background task keeps running after the entry setup
    returns; events fired before it registers its state listeners would
    be lost. ``_async_unsub_state_changed`` is assigned in the listener
    registration block at the end of the sequence.
    """
    bt = hass.data[DOMAIN][entry.entry_id]["climate"]
    assert await wait_for(
        hass,
        lambda: not bt.startup_running and bt._async_unsub_state_changed is not None,
    )
    return bt


def profile_id(spec) -> str:
    """Name a parametrized case after the device form or wiring it drives."""
    return spec.name


def set_room_sensor(hass, value, unit=UnitOfTemperature.CELSIUS) -> None:
    """Publish an external room temperature reading in ``unit``."""
    hass.states.async_set(SENSOR_ID, str(value), {"unit_of_measurement": unit})


def assert_on_device_grid(value: float, profile: DeviceProfile) -> None:
    """Fail unless ``value`` is a multiple of the device's setpoint step.

    A device only accepts setpoints on the grid it published, so every value
    that leaves the integration has to be zero-anchored on that grid — which
    is a different number per device form. ``value`` is in the device's own
    unit, like the step it is checked against.
    """
    step = profile.target_temperature_step
    assert value == pytest.approx(round(value / step) * step, abs=1e-6)


def assert_profile_adopted(bt, profile: DeviceProfile) -> None:
    """Fail if Better Thermostat did not read this device's capabilities.

    The guard that keeps a parametrized axis honest: a device whose state
    arrived too late is read as a device without capabilities, and every
    assertion downstream then measures that one case instead of the profile.
    """
    trv = bt.real_trvs[profile.entity_id]
    assert trv.hvac_modes == list(profile.hvac_modes)
    assert trv.target_temp_step == pytest.approx(
        _celsius_step(profile.target_temperature_step, profile.temperature_unit),
        abs=1e-3,
    )
    assert trv.min_temp == pytest.approx(
        _celsius(profile.min_temp, profile.temperature_unit), abs=1e-2
    )
    assert trv.max_temp == pytest.approx(
        _celsius(profile.max_temp, profile.temperature_unit), abs=1e-2
    )
    assert trv.capabilities().supports_off_mode is (HVACMode.OFF in profile.hvac_modes)


def _celsius(value: float, unit: UnitOfTemperature) -> float:
    """Convert a temperature reading from the device's unit to Celsius."""
    return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)


def _celsius_step(value: float, unit: UnitOfTemperature) -> float:
    """Convert a step from the device's unit to Celsius.

    A step is a difference, so a Fahrenheit step is scaled instead of run
    through the absolute conversion, which is the rule the integration
    applies to a reported step.
    """
    if unit is UnitOfTemperature.FAHRENHEIT:
        return round(value * 5.0 / 9.0, 4)
    return value
