"""Which of several matching siblings on the TRV's device is taken up.

A TRV's device often exposes more than one entity whose name fits a lookup:
a valve number beside a valve sensor, a closing degree beside an opening
degree, a window lock beside the child lock. The pick must not depend on
the order in which the integration registered them, so every case runs
against both registry orders.

The registry and its entries are the production types from
``tests.factories``.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import STATE_ON
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from custom_components.better_thermostat.adapters import mqtt, valve_entity
from custom_components.better_thermostat.model_fixes import default as default_quirk
from custom_components.better_thermostat.switch import BetterThermostatChildLockSwitch
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils import helpers
from custom_components.better_thermostat.utils.const import CONF_CHILD_LOCK
from custom_components.better_thermostat.utils.entry_schema import TrvAdvanced
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

TRV_ID = "climate.trv"
TRV_DEVICE = "device_trv"

REGISTRY_GETTERS = (
    f"{helpers.__name__}.er.async_get",
    f"{default_quirk.__name__}.er.async_get",
    "custom_components.better_thermostat.switch.er.async_get",
)


def _entry(entity_id: str, **fields: Any) -> er.RegistryEntry:
    return make_registry_entry(entity_id, device_id=TRV_DEVICE, **fields)


def _orders(*entries: er.RegistryEntry) -> list[tuple[er.RegistryEntry, ...]]:
    return list(itertools.permutations(entries))


def _order_id(order: tuple[er.RegistryEntry, ...]) -> str:
    return ",".join(entry.entity_id for entry in order)


def _host(registry: Any, advanced: TrvAdvanced | None = None) -> MagicMock:
    """A Better Thermostat stand-in whose states follow the registry.

    Every enabled switch reports ``on``, every other enabled entity a
    neutral value; a disabled entity has no state.
    """

    def _state(entity_id: str) -> State | None:
        entry = registry.entities.get(entity_id)
        if entry is None or entry.disabled_by is not None:
            return None
        if entry.domain == "switch":
            return State(entity_id, STATE_ON)
        return State(entity_id, "50", {"min": 0, "max": 100, "step": 1})

    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.unique_id = "bt_test"
    host.model = None
    host.context = None
    host.hass.states.get = _state
    host.hass.services.async_call = AsyncMock(return_value=None)
    host.real_trvs = {
        TRV_ID: Trv(entity_id=TRV_ID, model="TRVZB", advanced=advanced or {})
    }
    return host


@contextlib.contextmanager
def _registry_in_place(registry: Any) -> Iterator[None]:
    """Hand ``registry`` to every module under test; no device is registered."""
    device_registry = MagicMock(spec=dr.DeviceRegistry)
    device_registry.async_get.return_value = None
    with contextlib.ExitStack() as stack:
        for getter in REGISTRY_GETTERS:
            stack.enter_context(patch(getter, return_value=registry))
        stack.enter_context(
            patch(f"{helpers.__name__}.dr.async_get", return_value=device_registry)
        )
        yield


def _registry(order: tuple[er.RegistryEntry, ...]) -> Any:
    trv = make_registry_entry(TRV_ID, device_id=TRV_DEVICE)
    return make_entity_registry(trv, *order)


async def _found_valve(order: tuple[er.RegistryEntry, ...]):
    registry = _registry(order)
    host = _host(registry)
    with _registry_in_place(registry):
        return await helpers.find_valve_entity(host, TRV_ID, trv=host.real_trvs[TRV_ID])


VALVE_SENSOR = _entry("sensor.trv_valve_position")
VALVE_NUMBER = _entry("number.trv_valve_opening")


@pytest.mark.parametrize("order", _orders(VALVE_SENSOR, VALVE_NUMBER), ids=_order_id)
@pytest.mark.asyncio
async def test_a_writable_valve_number_wins_over_a_better_named_sensor(order):
    """The number that moves the valve is the valve channel.

    A sensor named for the valve position only reports it. Taking it in
    place of a writable valve number on the same device leaves Better
    Thermostat without direct valve control the device offers.
    """
    found = await _found_valve(order)

    assert found is not None
    assert found["entity_id"] == "number.trv_valve_opening"
    assert found["writable"] is True


@pytest.mark.parametrize(
    "order",
    _orders(_entry("sensor.trv_position", original_name="Position"), VALVE_SENSOR),
    ids=_order_id,
)
@pytest.mark.asyncio
async def test_among_read_only_sensors_the_best_named_one_is_reported(order):
    """With nothing writable, the sensor named for the valve is the one read.

    A bare ``position`` sensor could report anything; the lookup returns
    the best match, not whichever sensor the registry happens to list
    first.
    """
    found = await _found_valve(order)

    assert found is not None
    assert found["entity_id"] == "sensor.trv_valve_position"
    assert found["writable"] is False


CLOSING_BY_KEY = _entry(
    "number.trv_valve_closing_degree", translation_key="valve_closing_degree"
)
CLOSING_BY_NAME = [
    CLOSING_BY_KEY,
    _entry("number.trv_closing", unique_id="0x01_valve_closing_degree"),
    _entry("number.trv_closing", original_name="Valve closing degree"),
]
OPENING = _entry(
    "number.trv_valve_opening_degree", translation_key="valve_opening_degree"
)


@pytest.mark.parametrize(
    "closing", CLOSING_BY_NAME, ids=["translation_key", "unique_id", "name"]
)
@pytest.mark.asyncio
async def test_a_closing_degree_is_never_the_valve_entity(closing):
    """The closing degree holds the complement of the opening.

    Written with the opening percentage it would close the valve by what
    Better Thermostat meant to open it, and read back it would never match
    what was sent.
    """
    registry = _registry((closing,))
    host = _host(registry)
    with _registry_in_place(registry):
        found = await helpers.find_valve_entity(
            host, TRV_ID, trv=host.real_trvs[TRV_ID]
        )
        offered = await mqtt.get_info(host, TRV_ID)
        await valve_entity.discover_valve_entity(host, TRV_ID)

    assert found is None
    assert offered["support_valve"] is False
    assert host.real_trvs[TRV_ID].valve_position_entity is None


@pytest.mark.parametrize(
    "order",
    _orders(
        _entry(
            OPENING.entity_id,
            translation_key="valve_opening_degree",
            disabled_by=er.RegistryEntryDisabler.USER,
        ),
        CLOSING_BY_KEY,
    ),
    ids=_order_id,
)
@pytest.mark.asyncio
async def test_a_disabled_opening_degree_is_named_rather_than_replaced(order, caplog):
    """With the opening degree disabled, the device offers no valve entity.

    The closing degree does not stand in for it, and the warning names the
    disabled opening degree so the user knows what to enable.
    """
    found = await _found_valve(order)

    assert found is None
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "is disabled" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert OPENING.entity_id in warnings[0]


@pytest.mark.parametrize("order", _orders(OPENING, CLOSING_BY_KEY), ids=_order_id)
@pytest.mark.asyncio
async def test_the_opening_degree_is_the_valve_entity_beside_a_closing_degree(order):
    """Both degrees enabled: the opening degree takes the valve position."""
    found = await _found_valve(order)

    assert found is not None
    assert found["entity_id"] == OPENING.entity_id
    assert found["writable"] is True


CHILD_LOCK = _entry("switch.trv_child_lock")
OTHER_LOCKS = [
    _entry("switch.trv_window_lock"),
    _entry("switch.trv_valve_lock"),
    _entry("lock.trv_door_lock"),
]


async def _child_lock_from_startup(host: MagicMock) -> None:
    await default_quirk.initial_tweak(host, TRV_ID)


async def _child_lock_from_switch(host: MagicMock) -> None:
    switch = BetterThermostatChildLockSwitch(host, TRV_ID, show_trv_name=False)
    await switch._set_child_lock(False)


CHILD_LOCK_WRITERS = {
    "startup": _child_lock_from_startup,
    "switch": _child_lock_from_switch,
}


def _switched(host: MagicMock) -> list[str]:
    return [
        call.args[2]["entity_id"]
        for call in host.hass.services.async_call.await_args_list
        if call.args[0] in {"switch", "lock"}
    ]


@pytest.mark.parametrize("writer", list(CHILD_LOCK_WRITERS))
@pytest.mark.parametrize(
    "other", OTHER_LOCKS, ids=[entry.entity_id for entry in OTHER_LOCKS]
)
@pytest.mark.parametrize("child_lock_first", [True, False], ids=["first", "last"])
@pytest.mark.asyncio
async def test_the_child_lock_wins_over_any_other_lock(writer, other, child_lock_first):
    """Startup and the switch both set the entity named for the child lock.

    Another switch or lock whose name merely contains "lock" stays
    untouched, wherever the registry lists it.
    """
    order = (CHILD_LOCK, other) if child_lock_first else (other, CHILD_LOCK)
    registry = _registry(order)
    host = _host(registry, advanced={CONF_CHILD_LOCK: False})

    with _registry_in_place(registry):
        await CHILD_LOCK_WRITERS[writer](host)

    assert _switched(host) == [CHILD_LOCK.entity_id]


@pytest.mark.parametrize("writer", list(CHILD_LOCK_WRITERS))
@pytest.mark.asyncio
async def test_a_bare_lock_serves_when_nothing_is_named_for_the_child_lock(writer):
    """A device whose only lock entity is a bare ``lock`` uses that one."""
    lone_lock = _entry("switch.trv_lock")
    registry = _registry((lone_lock,))
    host = _host(registry, advanced={CONF_CHILD_LOCK: False})

    with _registry_in_place(registry):
        await CHILD_LOCK_WRITERS[writer](host)

    assert _switched(host) == [lone_lock.entity_id]
