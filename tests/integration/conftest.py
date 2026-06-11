"""Harness for tests against a real Home Assistant instance.

The unit suite mocks the entity; everything here sets up a real config
entry against a real (simulated) climate entity so the wiring across
the layers — entity lifecycle, queues, kernel, adapters, services — is
exercised end to end.
"""

import asyncio
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

# Pin the repository's custom_components namespace package before the
# hass fixture mounts the plugin's testing config dir: that dir carries
# a regular custom_components package which would otherwise shadow the
# repository and make the loader report "Integration not found".
import custom_components.better_thermostat  # noqa: F401  isort: skip

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.setup import async_setup_component

DOMAIN = "better_thermostat"
TRV_ID = "climate.fake_trv"
SENSOR_ID = "sensor.room_temperature"
WINDOW_ID = "binary_sensor.window"


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


class FakeTrvEntity(ClimateEntity):
    """A well-behaved simulated TRV as a real climate entity.

    Commands arrive through the real climate services and are confirmed
    into the entity state, like a device that applies every write. The
    recorded calls are the assertion surface.
    """

    _attr_name = "fake trv"
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_min_temp = 5.0
    _attr_max_temp = 30.0
    _attr_target_temperature_step = 0.5

    def __init__(self):
        """Initialize device state and the recorded-write assertion surface."""
        self._attr_hvac_mode = HVACMode.HEAT
        self._attr_current_temperature = 19.5
        self._attr_target_temperature = 20.0
        self.set_temperature_calls: list[float] = []
        self.set_hvac_mode_calls: list[str] = []
        # Radio loss simulation: the next setpoint write is recorded but
        # neither applied nor confirmed.
        self.drop_next_setpoint_write = False

    async def async_set_temperature(self, **kwargs) -> None:
        """Apply and confirm a setpoint write.

        The write is always recorded; with ``drop_next_setpoint_write``
        set it is then swallowed instead of applied, simulating a device
        that lost the command over the radio.
        """
        temperature = kwargs[ATTR_TEMPERATURE]
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


@pytest.fixture
async def fake_trv(hass):
    """Register a fake TRV with the real climate component."""
    entity = FakeTrvEntity()
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [entity])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    assert hass.states.get(TRV_ID) is not None
    return entity


def make_entry(*, with_window=False) -> MockConfigEntry:
    """Build a config entry matching the current entry schema."""
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
                    "no_off_system_mode": False,
                },
            }
        ],
        "temperature_sensor": SENSOR_ID,
        "model": "Generic",
        "target_temp_step": "0.5",
        "tolerance": 0.3,
        "off_temperature": 5,
    }
    if with_window:
        data["window_sensors"] = WINDOW_ID
        data["window_off_delay"] = 0
        data["window_off_delay_after"] = 0
    return MockConfigEntry(domain=DOMAIN, version=18, data=data, title="BT Test")


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
