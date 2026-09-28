"""Entities built from the thermostat must survive the order of a real boot.

Every platform besides climate builds its entities from the climate entity.
The climate entity resolves the device range and the step in its startup,
and on a boot that startup waits until Home Assistant has started, long after
the platforms are built; a TRV's name is known once its own integration
reports a state, which on a boot can also come later. Each test runs twice:
once on a Home Assistant that is booting (``boot``), and once on one that is
already running (``running``), where the startup happens to finish first.
"""

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.const import ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    TRV_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for_startup,
)

_ORDERS = pytest.mark.parametrize("order", ["boot", "running"])
"""Every test runs on a booting and on a running Home Assistant."""


async def _set_up_during_boot(hass, entry):
    """Set ``entry`` up while Home Assistant is still starting.

    Returns the climate entity once every platform of the entry is added;
    its startup sequence has not run yet.
    """
    hass.set_state(CoreState.starting)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bt = hass.data[DOMAIN][entry.entry_id]["climate"]
    # The state listeners are registered at the end of the startup, so their
    # absence shows the startup is still waiting for Home Assistant.
    assert bt._async_unsub_state_changed is None
    return bt


async def _finish_boot(hass, entry):
    """Announce that Home Assistant has started and wait for the startup."""
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    return await wait_for_startup(hass, entry)


async def _set_up(hass, entry, order):
    """Set ``entry`` up in ``order`` and return the started climate entity."""
    if order == "boot":
        await _set_up_during_boot(hass, entry)
        return await _finish_boot(hass, entry)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


def _entry_with(entry, **extra):
    """Return ``entry`` with ``extra`` merged into its data."""
    return MockConfigEntry(
        domain=DOMAIN, version=18, data={**entry.data, **extra}, title=entry.title
    )


def _set_room_sensor(hass, value="18.0"):
    """Set the external room temperature sensor to ``value`` (°C)."""
    hass.states.async_set(SENSOR_ID, value, {"unit_of_measurement": "°C"})


def _number_state(hass, bt, suffix):
    """Return the state of the number whose unique id ends in ``suffix``."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{bt.unique_id}_{suffix}"
    )
    assert entity_id is not None, suffix
    return hass.states.get(entity_id)


class _NamedTrv(FakeTrvEntity):
    """A fake TRV with its own name, and with it its own entity id."""

    def __init__(self, name, *, hvac_modes=None, min_temp=5.0, max_temp=30.0):
        super().__init__()
        self._attr_name = name
        self._attr_min_temp = min_temp
        self._attr_max_temp = max_temp
        if hvac_modes is not None:
            self._attr_hvac_modes = hvac_modes
            self._attr_hvac_mode = hvac_modes[0]


async def _register(hass, *entities):
    """Register ``entities`` with the real climate component."""
    setup_test_component_platform(hass, CLIMATE_DOMAIN, list(entities))
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()


@_ORDERS
@pytest.mark.parametrize(
    ("trv_range", "extra", "expected"),
    [
        pytest.param((7.0, 28.0), {}, (7.0, 28.0), id="device_range"),
        pytest.param(
            (5.0, 30.0),
            {"target_temp_min": 10.0, "target_temp_max": 35.0},
            (10.0, 35.0),
            id="configured_range",
        ),
    ],
)
async def test_preset_number_offers_the_thermostat_range(
    hass, trv_range, extra, expected, order
):
    """A preset number accepts exactly the range the thermostat accepts.

    The range comes from the device or from the configured bounds, and a
    preset outside it is one the thermostat cannot run.
    """
    await _register(
        hass, _NamedTrv("fake trv", min_temp=trv_range[0], max_temp=trv_range[1])
    )
    _set_room_sensor(hass)
    bt = await _set_up(hass, _entry_with(make_entry(), **extra), order)
    assert (bt.min_temp, bt.max_temp) == expected

    number = _number_state(hass, bt, "preset_comfort")
    assert (number.attributes["min"], number.attributes["max"]) == expected

    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: number.entity_id, "value": expected[1]},
        blocking=True,
    )
    assert float(hass.states.get(number.entity_id).state) == expected[1]


@_ORDERS
async def test_preset_number_steps_on_the_device_grid(hass, fake_trv, order):
    """A preset number steps on the grid the thermostat's setpoint steps on."""
    _set_room_sensor(hass)
    bt = await _set_up(hass, _entry_with(make_entry(), target_temp_step="0.0"), order)
    assert bt.target_temperature_step == fake_trv.target_temperature_step

    number = _number_state(hass, bt, "preset_comfort")
    assert number.attributes["step"] == bt.target_temperature_step


@_ORDERS
async def test_cooling_preset_number_offers_the_room_range(hass, order):
    """A cooling preset number accepts exactly the range the room accepts.

    With a separate cooler the room range is where the heater's and the
    cooler's ranges overlap.
    """
    cooler = _NamedTrv(
        "fake cooler",
        hvac_modes=[HVACMode.COOL, HVACMode.OFF],
        min_temp=16.0,
        max_temp=32.0,
    )
    await _register(hass, _NamedTrv("fake trv"), cooler)
    _set_room_sensor(hass)
    entry = _entry_with(make_entry(), cooler=cooler.entity_id)
    bt = await _set_up(hass, entry, order)
    assert (bt.min_temp, bt.max_temp) == (16.0, 30.0)

    number = _number_state(hass, bt, "preset_comfort_cool")
    assert (number.attributes["min"], number.attributes["max"]) == (
        bt.min_temp,
        bt.max_temp,
    )


_PER_HEAD_SUFFIXES = (
    "pid_kp",
    "pid_ki",
    "pid_kd",
    "valve_max_opening",
    "pid_auto_tune",
    "child_lock",
)


@_ORDERS
@pytest.mark.parametrize("suffix", _PER_HEAD_SUFFIXES)
async def test_per_head_entity_is_named_after_its_trv(hass, suffix, order):
    """A per-head entity is named after the TRV it belongs to.

    On a boot the TRV's own integration may report its first state after
    this one's platforms are built; the name follows once it does.
    """
    heads = [_NamedTrv("fake trv"), _NamedTrv("second trv")]
    await _register(hass, *heads)
    _set_room_sensor(hass)
    template = make_entry().data["thermostat"][0]
    thermostats = [
        {
            **template,
            "trv": head.entity_id,
            "advanced": {
                **template["advanced"],
                "calibration": "direct_valve_based",
                "calibration_mode": "pid_calibration",
            },
        }
        for head in heads
    ]
    entry = _entry_with(make_entry(), thermostat=thermostats)
    if order == "boot":
        for head in heads:
            hass.states.async_remove(head.entity_id)
        await _set_up_during_boot(hass, entry)
        for head in heads:
            head.async_write_ha_state()
        await hass.async_block_till_done()
        bt = await _finish_boot(hass, entry)
    else:
        bt = await _set_up(hass, entry, order)

    platform = "switch" if suffix in ("pid_auto_tune", "child_lock") else "number"
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{bt.unique_id}_{TRV_ID}_{suffix}"
    )
    assert entity_id is not None
    friendly_name = hass.states.get(entity_id).attributes["friendly_name"]
    assert "fake trv" in friendly_name
    assert TRV_ID not in friendly_name
