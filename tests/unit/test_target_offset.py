"""Tests for the internal heating target offset."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.number import (
    BetterThermostatTargetOffsetNumber,
)
from custom_components.better_thermostat.utils.helpers import regulation_target
from tests.factories import ThermostatStandIn


@pytest.fixture
def thermostat():
    """Build a thermostat with an initially empty control queue."""
    bt = ThermostatStandIn()
    bt.unique_id = "test"
    bt.device_name = "Test"
    bt.target_offset = 0.0
    bt.heat_target_temperature = 19.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 24.0
    bt.cooler_entity_id = None
    bt.startup_running = False
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    return bt


@pytest.mark.parametrize(
    ("target", "calibration_offset", "expected"),
    [
        (5.0, -10.0, 5.0),
        (24.0, 10.0, 24.0),
        (20.0, -1.0, 19.0),
        (20.0, 2.0, 22.0),
        (5.0, 2.0, 7.0),
        (24.0, -2.0, 22.0),
        (20.0, -2.0, 18.0),
    ],
)
def test_regulation_target_clamps_offset_to_room_range(
    thermostat, target, calibration_offset, expected
):
    """An offset cannot move regulation outside the heating range."""
    thermostat.heat_target_temperature = target
    thermostat.target_offset = calibration_offset

    assert regulation_target(thermostat) == pytest.approx(expected)


@pytest.mark.parametrize("hvac_mode", [HVACMode.HEAT, HVACMode.HEAT_COOL])
@pytest.mark.parametrize(
    ("target_offset", "reported", "expected"),
    [(-2.0, -5.0, 5.0), (-2.0, 35.0, 21.5), (2.0, -5.0, 5.0), (2.0, 35.0, 19.5)],
)
def test_inbound_heat_clamp_handles_offset_sign_and_range_edges(
    thermostat, hvac_mode, target_offset, reported, expected
):
    """A TRV report is bounded and ordered for either offset sign and mode."""
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.hvac_mode = hvac_mode
    thermostat.heat_target_temperature = 19.0
    thermostat.target_offset = target_offset
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cool_target_temperature = 22.0

    adopted = BetterThermostat._clamp_inbound_heat_target(thermostat, reported)

    assert adopted == expected
    assert thermostat.bt_min_temp <= adopted <= thermostat.bt_max_temp
    assert max(
        adopted, regulation_target(thermostat, heat_target_temperature=adopted)
    ) < (thermostat.cool_target_temperature)


@pytest.mark.parametrize("hvac_mode", [HVACMode.COOL, HVACMode.HEAT_COOL])
@pytest.mark.parametrize(
    ("target_offset", "heat_target", "expected"),
    [(-2.0, 5.0, 5.5), (2.0, 5.0, 7.5), (-2.0, 30.0, 30.0), (2.0, 30.0, 30.0)],
)
def test_inbound_cool_clamp_uses_effective_heat_at_range_edges(
    thermostat, hvac_mode, target_offset, heat_target, expected
):
    """A cooler report clears the base or effective heat target within bounds."""
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.hvac_mode = hvac_mode
    thermostat.heat_target_temperature = heat_target
    thermostat.target_offset = target_offset
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 30.0

    adopted = BetterThermostat._clamp_inbound_cool_target(thermostat, 0.0)

    assert adopted == expected
    assert adopted <= thermostat.cool_max_temperature
    assert adopted >= max(heat_target, regulation_target(thermostat))


@pytest.mark.parametrize(
    ("enforcer", "hvac_mode", "target_offset", "expected"),
    [
        ("cool", HVACMode.HEAT, -2.0, 20.0),
        ("cool", HVACMode.HEAT, 2.0, 20.0),
        ("cool", HVACMode.HEAT_COOL, -2.0, 20.5),
        ("cool", HVACMode.HEAT_COOL, 2.0, 22.5),
        ("heat", HVACMode.HEAT, -2.0, 20.0),
        ("heat", HVACMode.HEAT, 2.0, 20.0),
        ("heat", HVACMode.HEAT_COOL, -2.0, 19.5),
        ("heat", HVACMode.HEAT_COOL, 2.0, 17.5),
    ],
)
def test_ordering_enforcers_handle_both_offsets_and_modes(
    thermostat, enforcer, hvac_mode, target_offset, expected
):
    """Ordering uses the effective heat target only in HEAT_COOL mode."""
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.hvac_mode = hvac_mode
    thermostat.heat_target_temperature = 20.0
    thermostat.target_offset = target_offset
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cool_target_temperature = 20.0
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 35.0

    if enforcer == "cool":
        BetterThermostat._enforce_cool_above_heat(thermostat)
        assert thermostat.cool_target_temperature == expected
    else:
        BetterThermostat._enforce_heat_below_cool(thermostat)
        assert thermostat.heat_target_temperature == expected


@pytest.mark.parametrize(("target_offset", "expected_cool"), [(-2.0, 5.5), (2.0, 7.5)])
def test_heat_minimum_boundary_resolves_cooling_order_for_both_offsets(
    thermostat, target_offset, expected_cool
):
    """When the heat minimum pins adoption, enforcement lifts cooling as needed."""
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.hvac_mode = HVACMode.HEAT_COOL
    thermostat.heat_target_temperature = 5.0
    thermostat.bt_min_temp = 5.0
    thermostat.target_offset = target_offset
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cool_target_temperature = 5.0
    thermostat.cool_min_temperature = 5.0
    thermostat.cool_max_temperature = 35.0

    adopted = BetterThermostat._clamp_inbound_heat_target(thermostat, 35.0)
    thermostat.heat_target_temperature = adopted
    BetterThermostat._enforce_cool_above_heat(thermostat)

    assert adopted == thermostat.bt_min_temp
    assert thermostat.cool_target_temperature == expected_cool
    assert thermostat.cool_target_temperature > adopted


@pytest.mark.parametrize(
    ("target_offset", "expected_heat"), [(-2.0, 29.5), (2.0, 27.5)]
)
def test_cool_maximum_boundary_resolves_heating_order_for_both_offsets(
    thermostat, target_offset, expected_heat
):
    """At the cooling maximum, heating yields using the signed offset rule."""
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.hvac_mode = HVACMode.HEAT_COOL
    thermostat.heat_target_temperature = 30.0
    thermostat.bt_max_temp = 30.0
    thermostat.target_offset = target_offset
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 30.0

    adopted = BetterThermostat._clamp_inbound_cool_target(thermostat, 0.0)
    thermostat.cool_target_temperature = adopted
    BetterThermostat._enforce_heat_below_cool(thermostat)

    assert adopted == thermostat.cool_max_temperature
    assert thermostat.heat_target_temperature == expected_heat
    assert thermostat.heat_target_temperature < thermostat.cool_target_temperature


async def test_offset_reversal_restores_cooling_target(thermostat):
    """Removing an offset undoes the cooling-target lift it caused."""
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.cool_target_temperature = 22.0
    thermostat.heat_target_temperature = 21.0
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 35.0
    thermostat._offset_ordered_cool_target = None
    thermostat.hvac_mode = HVACMode.HEAT_COOL
    thermostat._bound_cool_target_to_range = lambda value: (
        BetterThermostat._bound_cool_target_to_range(thermostat, value)
    )
    thermostat._bound_target_to_range = lambda value: (
        BetterThermostat._bound_target_to_range(thermostat, value)
    )

    def collect_no_trv_states() -> list[State]:
        return []

    thermostat._collect_trv_states = collect_no_trv_states
    thermostat._resolve_temperature_range = lambda _states: None
    thermostat._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(thermostat, **kwargs)
    )
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(2.0)
    assert thermostat.cool_target_temperature == 23.5

    BetterThermostat._refresh_temperature_range(thermostat)
    assert thermostat._offset_ordered_cool_target == (22.0, 23.5)

    thermostat.cool_max_temperature = 21.5
    await entity.async_set_native_value(0.0)

    assert thermostat.cool_target_temperature == 21.5


async def test_manual_cool_target_change_clears_offset_rollback(
    thermostat, monkeypatch
):
    """An unchanged range refresh preserves rollback; a manual change clears it."""
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.cool_target_temperature = 22.0
    thermostat.heat_target_temperature = 21.0
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 35.0
    thermostat._offset_ordered_cool_target = None
    thermostat.hvac_mode = HVACMode.HEAT_COOL
    thermostat._bound_cool_target_to_range = lambda value: (
        BetterThermostat._bound_cool_target_to_range(thermostat, value)
    )
    thermostat._bound_target_to_range = lambda value: (
        BetterThermostat._bound_target_to_range(thermostat, value)
    )

    def collect_no_trv_states() -> list[State]:
        return []

    thermostat._collect_trv_states = collect_no_trv_states
    thermostat._resolve_temperature_range = lambda _states: None
    thermostat._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(thermostat, **kwargs)
    )
    monkeypatch.setattr(
        type(thermostat),
        "cool_target_temperature",
        BetterThermostat.cool_target_temperature,
        raising=False,
    )
    thermostat.runtime = SimpleNamespace(cool_target_temperature=22.0)
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(2.0)
    assert thermostat.cool_target_temperature == 23.5
    assert thermostat._offset_ordered_cool_target == (22.0, 23.5)

    BetterThermostat._refresh_temperature_range(thermostat)
    assert thermostat._offset_ordered_cool_target == (22.0, 23.5)

    thermostat.cool_target_temperature = 24.0
    assert thermostat._offset_ordered_cool_target is None
    await entity.async_set_native_value(0.0)

    assert thermostat.cool_target_temperature == 24.0


async def test_offset_decrease_replaces_cooling_target_rollback(thermostat):
    """An offset decrease that reorders cooling retains the original target."""
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.cool_target_temperature = 22.0
    thermostat.heat_target_temperature = 21.0
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 35.0
    thermostat._offset_ordered_cool_target = None
    thermostat.hvac_mode = HVACMode.HEAT_COOL
    thermostat._bound_cool_target_to_range = lambda value: (
        BetterThermostat._bound_cool_target_to_range(thermostat, value)
    )
    thermostat._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(thermostat, **kwargs)
    )
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(2.0)
    assert thermostat.cool_target_temperature == 23.5
    await entity.async_set_native_value(1.0)
    assert thermostat.cool_target_temperature == 22.5

    await entity.async_set_native_value(0.0)

    assert thermostat.cool_target_temperature == 22.0


@pytest.mark.parametrize(
    ("current", "saved", "expected"),
    [
        (23.5, [22.0, 23.5], (22.0, 23.5)),
        (24.0, [22.0, 23.5], None),
        (23.5, [float("nan"), 23.5], None),
    ],
)
def test_restore_offset_cooling_target_pair(thermostat, current, saved, expected):
    """Restore only a finite rollback pair that still matches the cool target."""
    thermostat.cool_target_temperature = current
    thermostat._offset_ordered_cool_target = None
    state = State(
        "climate.test_thermostat",
        "heat",
        attributes={"offset_ordered_cool_target": saved},
    )

    BetterThermostat._restore_offset_ordered_cool_target(thermostat, state)

    assert thermostat._offset_ordered_cool_target == expected


def test_restore_offset_pair_accepts_fahrenheit_rounding(thermostat):
    """A displayed Fahrenheit rounding delta preserves a consistent rollback."""
    restored = 23.61111111111111
    thermostat._unit = UnitOfTemperature.FAHRENHEIT
    thermostat.heat_target_temperature = 21.0
    thermostat.target_offset = 2.0
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 35.0
    thermostat.cool_target_temperature = restored
    state = State(
        "climate.test_thermostat",
        "heat",
        attributes={"offset_ordered_cool_target": [22.0, 23.6]},
    )

    BetterThermostat._restore_offset_ordered_cool_target(thermostat, state)

    assert thermostat._offset_ordered_cool_target == (22.0, restored)


def test_restore_offset_pair_accepts_same_fahrenheit_publication(thermostat):
    """Values that publish to the same Fahrenheit tenth keep the rollback."""
    thermostat._unit = UnitOfTemperature.FAHRENHEIT
    thermostat.heat_target_temperature = 21.0
    thermostat.target_offset = 2.0
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 35.0
    thermostat._offset_ordered_cool_target = None
    thermostat.cool_target_temperature = 23.58
    state = State(
        "climate.test_thermostat",
        "heat",
        attributes={"offset_ordered_cool_target": [22.0, 23.55]},
    )

    BetterThermostat._restore_offset_ordered_cool_target(thermostat, state)

    assert thermostat._offset_ordered_cool_target == (22.0, 23.58)


def test_restore_offset_pair_rejects_rounded_but_inconsistent_target(thermostat):
    """A near match is rejected if ordering would move the restored target."""
    thermostat._unit = UnitOfTemperature.FAHRENHEIT
    thermostat.heat_target_temperature = 21.0
    thermostat.target_offset = 2.0
    thermostat.bt_target_temperature_step = 0.5
    thermostat.cooler_entity_id = "climate.cooler"
    thermostat.cool_min_temperature = 16.0
    thermostat.cool_max_temperature = 35.0
    thermostat.cool_target_temperature = 22.99
    thermostat._offset_ordered_cool_target = None
    state = State(
        "climate.test_thermostat",
        "heat",
        attributes={"offset_ordered_cool_target": [22.0, 23.0]},
    )

    BetterThermostat._restore_offset_ordered_cool_target(thermostat, state)

    assert thermostat._offset_ordered_cool_target is None


@pytest.mark.parametrize(
    ("unit", "value", "expected"),
    [(UnitOfTemperature.CELSIUS, 0.5, 0.5), (UnitOfTemperature.FAHRENHEIT, 0.9, 0.5)],
)
async def test_set_offset_in_system_unit(thermostat, unit, value, expected):
    """Temperature differences convert without an absolute-temperature shift."""
    entity = BetterThermostatTargetOffsetNumber(thermostat, unit)
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(value)

    assert thermostat.target_offset == pytest.approx(expected)
    assert entity.native_value == pytest.approx(value)
    assert thermostat.heat_target_temperature == 19.0
    assert thermostat.control_queue_task.get_nowait() is thermostat


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -10.1, 10.1])
async def test_invalid_offset_is_rejected(thermostat, value):
    """Invalid settings leave the regulation state and queue unchanged."""
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    await entity.async_set_native_value(value)
    assert thermostat.target_offset == 0.0
    assert thermostat.control_queue_task.empty()


@pytest.mark.parametrize(
    ("saved", "unit", "expected"),
    [
        ("-0.5", UnitOfTemperature.CELSIUS, -0.5),
        ("0.9", UnitOfTemperature.FAHRENHEIT, 0.5),
    ],
)
async def test_restore_offset(thermostat, saved, unit, expected):
    """Restored states retain their saved temperature difference unit."""
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    entity.async_write_ha_state = MagicMock()
    state = State(
        "number.test_target_offset", saved, attributes={"unit_of_measurement": unit}
    )
    entity.async_get_last_state = AsyncMock(return_value=state)
    with patch.object(entity, "_follow_thermostat"):
        await entity.async_added_to_hass()
    assert thermostat.target_offset == pytest.approx(expected)
    assert thermostat.control_queue_task.get_nowait() is thermostat


async def test_pending_cycle_is_coalesced(thermostat):
    """Changing the offset never waits on an already full queue."""
    thermostat.control_queue_task.put_nowait(thermostat)
    entity = BetterThermostatTargetOffsetNumber(thermostat, UnitOfTemperature.CELSIUS)
    entity.async_write_ha_state = MagicMock()
    await asyncio.wait_for(entity.async_set_native_value(0.5), timeout=1)
    assert thermostat.target_offset == 0.5
    assert thermostat.control_queue_task.qsize() == 1
