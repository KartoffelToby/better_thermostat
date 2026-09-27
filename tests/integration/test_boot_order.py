"""Entities built from the thermostat must survive the order of a real boot.

Every platform besides climate builds its entities from the climate entity,
and some of them copy a value into the entity when it is constructed. The
climate entity resolves the device range and the step in its startup, and on
a boot that startup waits until Home Assistant has started, long after the
platforms are built; a TRV's name is known once its own integration reports
a state, which on a boot can also come later. Each test runs twice: once on
a Home Assistant that is booting (``boot``), and once on one that is already
running (``running``), where the startup happens to finish first.

Covered copies, all made in a constructor:

- ``BetterThermostatPresetNumber``: ``native_min_value`` and
  ``native_max_value`` from ``min_temp`` and ``max_temp``, ``native_step``
  from ``target_temperature_step``;
- ``BetterThermostatPresetCoolNumber`` inherits all three;
- ``BetterThermostatPIDNumber``, ``BetterThermostatValveMaxOpeningNumber``,
  ``BetterThermostatPIDAutoTuneSwitch`` and ``BetterThermostatChildLockSwitch``:
  the TRV's friendly name as the placeholder of their names.

Not covered, because the copied value cannot differ between a boot and a
reload: every ``unique_id`` (the config entry id), the cooler entity id and
the preset list (config entry data), and the sensors' ``device_info``, which
reads the entity and device registries, both loaded from storage before any
integration is set up.
"""

from dataclasses import replace

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .boot_sequence import finish_boot, set_up_during_boot
from .conftest import (
    DOMAIN,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, GROUP_OF_THREE, SEPARATE_COOLER

NARROW_RANGE_TRV = replace(
    GENERIC_HEAT_TRV, name="narrow_range_trv", min_temp=7.0, max_temp=28.0
)
"""A head whose range differs from the placeholder range on both ends."""

OWN_GRID_TRV = replace(
    GENERIC_HEAT_TRV, name="own_grid_trv", configured_target_temp_step="0.0"
)
"""A head whose half-degree grid is not overridden by the config entry."""


async def _set_up(hass, entry, order):
    """Set ``entry`` up in ``order`` and return the started climate entity."""
    if order == "boot":
        await set_up_during_boot(hass, entry)
        return await finish_boot(hass, entry)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


def _entry_for(profile, **extra):
    """Return the entry for ``profile`` with ``extra`` merged into its data."""
    entry = make_entry(profile)
    return MockConfigEntry(
        domain=DOMAIN, version=18, data={**entry.data, **extra}, title=entry.title
    )


def _number_state(hass, bt, suffix):
    """Return the state of the number whose unique id ends in ``suffix``."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, f"{bt.unique_id}_{suffix}"
    )
    assert entity_id is not None, suffix
    return hass.states.get(entity_id)


_ORDERS = pytest.mark.parametrize("order", ["boot", "running"])
"""Every test runs on a booting and on a running Home Assistant."""


@_ORDERS
@pytest.mark.parametrize(
    ("fake_trv", "extra", "expected"),
    [
        pytest.param(NARROW_RANGE_TRV, {}, (7.0, 28.0), id="device_range"),
        pytest.param(
            GENERIC_HEAT_TRV,
            {"target_temp_min": 10.0, "target_temp_max": 35.0},
            (10.0, 35.0),
            id="configured_range",
        ),
    ],
    indirect=["fake_trv"],
)
async def test_preset_number_offers_the_thermostat_range(
    hass, fake_trv, extra, expected, order
):
    """A preset number accepts exactly the range the thermostat accepts.

    The range comes from the device or from the configured bounds, and a
    preset outside it is one the thermostat cannot run.
    """
    set_room_sensor(hass, 18.0)
    entry = _entry_for(fake_trv.profile, **extra)
    bt = await _set_up(hass, entry, order)
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
@pytest.mark.parametrize("fake_trv", [OWN_GRID_TRV], indirect=True, ids=profile_id)
async def test_preset_number_steps_on_the_device_grid(hass, fake_trv, order):
    """A preset number steps on the grid the thermostat's setpoint steps on."""
    set_room_sensor(hass, 18.0)
    bt = await _set_up(hass, make_entry(fake_trv.profile), order)
    assert bt.target_temperature_step == fake_trv.profile.target_temperature_step

    number = _number_state(hass, bt, "preset_comfort")
    assert number.attributes["step"] == bt.target_temperature_step


@_ORDERS
@pytest.mark.parametrize(
    "device_role", [SEPARATE_COOLER], indirect=True, ids=profile_id
)
async def test_cooling_preset_number_offers_the_room_range(hass, device_role, order):
    """A cooling preset number accepts exactly the range the room accepts.

    With a separate cooler the room range is where the heater's and the
    cooler's ranges overlap.
    """
    set_room_sensor(hass, 18.0)
    bt = await _set_up(hass, make_entry(device_role.scenario), order)
    cooler = device_role.scenario.cooler
    assert (bt.min_temp, bt.max_temp) == (cooler.min_temp, cooler.max_temp)

    number = _number_state(hass, bt, "preset_comfort_cool")
    assert (number.attributes["min"], number.attributes["max"]) == (
        bt.min_temp,
        bt.max_temp,
    )


def _group_entry():
    """Return a group entry whose heads get every per-head entity.

    PID calibration adds the PID numbers and the auto-tune switch, valve
    calibration the max-opening number; the child-lock switch is always
    there. The TRV name is part of these names only with several heads.
    """
    entry = make_entry(GROUP_OF_THREE)
    thermostats = [
        {
            **head,
            "advanced": {
                **head["advanced"],
                "calibration": "direct_valve_based",
                "calibration_mode": "pid_calibration",
            },
        }
        for head in entry.data["thermostat"]
    ]
    return MockConfigEntry(
        domain=DOMAIN,
        version=18,
        data={**entry.data, "thermostat": thermostats},
        title=entry.title,
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
@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
@pytest.mark.parametrize("suffix", _PER_HEAD_SUFFIXES)
async def test_per_head_entity_is_named_after_its_trv(hass, trv_group, suffix, order):
    """A per-head entity is named after the TRV it belongs to.

    On a boot the TRV's own integration may report its first state after
    this one's platforms are built; the name follows once it does.
    """
    set_room_sensor(hass, 18.0)
    entry = _group_entry()
    head = trv_group[0]
    if order == "boot":
        for trv in trv_group.entities:
            hass.states.async_remove(trv.entity_id)
        bt = await set_up_during_boot(hass, entry)
        for trv in trv_group.entities:
            trv.async_write_ha_state()
        await hass.async_block_till_done()
        bt = await finish_boot(hass, entry)
    else:
        bt = await _set_up(hass, entry, order)

    platform = "switch" if suffix in ("pid_auto_tune", "child_lock") else "number"
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{bt.unique_id}_{head.entity_id}_{suffix}"
    )
    assert entity_id is not None
    friendly_name = hass.states.get(entity_id).attributes["friendly_name"]
    assert head.profile.entity_name in friendly_name
    assert head.entity_id not in friendly_name
