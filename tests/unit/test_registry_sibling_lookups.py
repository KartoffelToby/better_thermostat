"""Which registry entries count as a sibling of the TRV.

Every lookup here walks the entity registry for an entity that sits on the
same device as the TRV (a calibration number, a valve number, a battery
sensor, a mode select) and then reads it, writes it or reports a capability
from it. Two kinds of entry are no such sibling:

* A disabled entry. Home Assistant does not load it, so it has no state and
  a service call aimed at it is dropped with a warning. Adopting one makes
  Better Thermostat offer a capability the device cannot deliver and report
  every write to it as done.
* An entry of another device-less entity. A TRV without a device has no
  siblings; ``None == None`` is no shared device, and the entry that wins is
  whatever else the same integration registered without a device.

The registry and its entries are the production types from
``tests.factories``, and Home Assistant's states hold a state for every
enabled entry and none for a disabled one, as a running instance does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
import contextlib
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from custom_components.better_thermostat.adapters import (
    generic,
    mqtt,
    valve_entity,
    zwave_js,
)
from custom_components.better_thermostat.model_fixes import (
    SPZB0001,
    TRVZB,
    default as default_quirk,
)
from custom_components.better_thermostat.switch import BetterThermostatChildLockSwitch
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils import helpers
from tests.factories import make_entity_registry, make_registry_entry

TRV_ID = "climate.trv"
TRV_DEVICE = "device_trv"

# Where each module under test reaches the entity registry.
REGISTRY_GETTERS = (
    f"{helpers.__name__}.er.async_get",
    f"{TRVZB.__name__}.er.async_get",
    f"{SPZB0001.__name__}.er.async_get",
    f"{default_quirk.__name__}.er.async_get",
    "custom_components.better_thermostat.switch.er.async_get",
)

# The state an enabled entry reports, per domain.
ENABLED_STATE = {
    "climate": ("heat", {}),
    "number": ("0", {}),
    "select": ("2", {"options": ["1", "2"]}),
    "switch": ("off", {}),
    "sensor": ("50", {}),
}

DISABLED_BY = [er.RegistryEntryDisabler.USER, er.RegistryEntryDisabler.INTEGRATION]


@dataclass(frozen=True)
class Lookup:
    """One place that picks a sibling out of the registry.

    ``adopts`` runs it against the host and registry and answers whether
    the candidate was taken up: returned, written to, or reported as a
    capability.
    """

    name: str
    candidate: str
    adopts: Callable[[Any, Any, str], Awaitable[bool]]
    fields: dict[str, Any] = field(default_factory=dict)
    state: tuple[str, dict[str, Any]] | None = None


def _written(host: Any) -> set[str]:
    """Every entity a service call went out for."""
    return {
        call.args[2]["entity_id"]
        for call in host.hass.services.async_call.await_args_list
        if len(call.args) > 2 and "entity_id" in call.args[2]
    }


async def _calibration_found(host, _registry, candidate):
    return await helpers.find_local_calibration_entity(host, TRV_ID) == candidate


async def _valve_found(host, _registry, candidate):
    found = await helpers.find_valve_entity(host, TRV_ID)
    return found is not None and found["entity_id"] == candidate


async def _generic_offers_offset(host, _registry, _candidate):
    return (await generic.get_info(host, TRV_ID))["support_offset"]


async def _mqtt_offers_offset(host, _registry, _candidate):
    return (await mqtt.get_info(host, TRV_ID))["support_offset"]


async def _mqtt_offers_valve(host, _registry, _candidate):
    return (await mqtt.get_info(host, TRV_ID))["support_valve"]


async def _zwave_offers_offset(host, _registry, _candidate):
    return (await zwave_js.get_info(host, TRV_ID))["support_offset"]


async def _zwave_offers_valve(host, _registry, _candidate):
    return (await zwave_js.get_info(host, TRV_ID))["support_valve"]


async def _valve_discovered(host, _registry, candidate):
    await valve_entity.discover_valve_entity(host, TRV_ID)
    return host.real_trvs[TRV_ID].valve_position_entity == candidate


async def _device_entity_found(_host, registry, candidate):
    device_id = registry.async_get(TRV_ID).device_id
    found = helpers.find_device_entity(
        registry, device_id, ["switch", "lock"], ["child_lock"]
    )
    return found == candidate


async def _calibration_reset_on_startup(host, _registry, candidate):
    await default_quirk.initial_tweak(host, TRV_ID)
    return candidate in _written(host)


async def _child_lock_written(host, _registry, candidate):
    switch = BetterThermostatChildLockSwitch(host, TRV_ID, show_trv_name=False)
    await switch._set_child_lock(True)
    return candidate in _written(host)


async def _battery_found(host, _registry, candidate):
    return await helpers.find_battery_entity(host, TRV_ID) == candidate


async def _trvzb_sibling_found(_host, registry, candidate):
    device_id = registry.async_get(TRV_ID).device_id
    found = TRVZB._find_device_entity(
        registry,
        device_id,
        "select",
        TRVZB._TK_SENSOR_SELECT,
        "temperature_sensor_select",
    )
    return found == candidate


async def _trvzb_sensor_selected(host, _registry, candidate):
    await TRVZB.maybe_select_external_sensor(host, TRV_ID)
    return candidate in _written(host)


async def _trvzb_external_temperature_written(host, _registry, candidate):
    await TRVZB.maybe_set_external_temperature(host, TRV_ID, 20.5)
    return candidate in _written(host)


async def _trvzb_valve_written(host, _registry, candidate):
    await TRVZB.maybe_set_sonoff_valve_percent(host, TRV_ID, 40)
    return candidate in _written(host)


async def _spzb_mode_written(host, _registry, candidate):
    await SPZB0001.check_operation_mode(host, TRV_ID, "1")
    return candidate in _written(host)


CALIBRATION = {"translation_key": "local_temperature_calibration"}
VALVE = {"translation_key": "valve_opening_degree"}

LOOKUPS = {
    lookup.name: lookup
    for lookup in (
        Lookup(
            "find_local_calibration_entity",
            "number.trv_local_temperature_calibration",
            _calibration_found,
            CALIBRATION,
        ),
        Lookup(
            "find_valve_entity", "number.trv_valve_opening_degree", _valve_found, VALVE
        ),
        Lookup(
            "generic.get_info offset",
            "number.trv_local_temperature_calibration",
            _generic_offers_offset,
            CALIBRATION,
        ),
        Lookup(
            "mqtt.get_info offset",
            "number.trv_local_temperature_calibration",
            _mqtt_offers_offset,
            CALIBRATION,
        ),
        Lookup(
            "mqtt.get_info valve",
            "number.trv_valve_opening_degree",
            _mqtt_offers_valve,
            VALVE,
        ),
        Lookup(
            "zwave_js.get_info offset",
            "number.trv_local_temperature_calibration",
            _zwave_offers_offset,
            CALIBRATION,
        ),
        Lookup(
            "zwave_js.get_info valve",
            "number.trv_valve_opening_degree",
            _zwave_offers_valve,
            VALVE,
        ),
        Lookup(
            "valve_entity.discover_valve_entity",
            "number.trv_valve_opening_degree",
            _valve_discovered,
            VALVE,
        ),
        Lookup("find_device_entity", "switch.trv_child_lock", _device_entity_found),
        Lookup(
            "default.initial_tweak calibration reset",
            "number.trv_local_temperature_calibration",
            _calibration_reset_on_startup,
        ),
        Lookup("switch child lock", "switch.trv_child_lock", _child_lock_written),
        Lookup(
            "find_battery_entity",
            "sensor.trv_battery",
            _battery_found,
            {"original_device_class": "battery"},
        ),
        Lookup(
            "TRVZB._find_device_entity",
            "select.trv_temperature_sensor_select",
            _trvzb_sibling_found,
            {"translation_key": "temperature_sensor_select"},
        ),
        Lookup(
            "TRVZB.maybe_select_external_sensor",
            "select.trv_temperature_sensor_select",
            _trvzb_sensor_selected,
            {"translation_key": "temperature_sensor_select"},
            ("internal", {"options": ["internal", "external"]}),
        ),
        Lookup(
            "TRVZB.maybe_set_external_temperature",
            "number.trv_external_temperature_input",
            _trvzb_external_temperature_written,
            {"translation_key": "external_temperature_input"},
        ),
        Lookup(
            "TRVZB.maybe_set_sonoff_valve_percent",
            "number.trv_valve_opening_degree",
            _trvzb_valve_written,
            VALVE,
        ),
        Lookup(
            "SPZB0001.check_operation_mode", "select.trv_trv_mode", _spzb_mode_written
        ),
    )
}

ADOPTS_A_DISABLED_ENTRY = pytest.mark.xfail(
    strict=True,
    reason="the lookup takes a disabled registry entry for the TRV's sibling",
)
MATCHES_ANY_DEVICE_LESS_ENTRY = pytest.mark.xfail(
    strict=True,
    reason="a TRV without a device takes any device-less entry of its config "
    "entry for its sibling",
)

# A disabled entry has no state, so a lookup that reads the state before it
# writes passes over it on its own; the others take it up.
DISABLED_ENTRY_ADOPTED_BY = frozenset(
    {
        "find_local_calibration_entity",
        "find_valve_entity",
        "generic.get_info offset",
        "mqtt.get_info offset",
        "mqtt.get_info valve",
        "zwave_js.get_info offset",
        "zwave_js.get_info valve",
        "valve_entity.discover_valve_entity",
        "find_device_entity",
        "default.initial_tweak calibration reset",
        "find_battery_entity",
        "TRVZB._find_device_entity",
        "TRVZB.maybe_set_external_temperature",
        "TRVZB.maybe_set_sonoff_valve_percent",
    }
)

# The lookups that compare device ids without first asking whether the TRV
# has a device at all.
DEVICE_LESS_ENTRY_ADOPTED_BY = frozenset(
    {
        "find_local_calibration_entity",
        "find_valve_entity",
        "generic.get_info offset",
        "mqtt.get_info offset",
        "mqtt.get_info valve",
        "zwave_js.get_info offset",
        "zwave_js.get_info valve",
        "valve_entity.discover_valve_entity",
        "TRVZB.maybe_set_sonoff_valve_percent",
        "SPZB0001.check_operation_mode",
    }
)

# ``find_device_entity`` takes the device id from its caller, and both
# callers stop at a TRV without one; they carry that case below.
TAKES_ITS_DEVICE_FROM_THE_CALLER = frozenset({"find_device_entity"})


def _host(registry: Any, lookup: Lookup, candidate: str) -> MagicMock:
    """A Better Thermostat stand-in whose states follow the registry."""

    def _state(entity_id: str) -> State | None:
        entry = registry.entities.get(entity_id)
        if entry is None or entry.disabled_by is not None:
            return None
        value, attributes = ENABLED_STATE[entry.domain]
        if entity_id == candidate and lookup.state is not None:
            value, attributes = lookup.state
        return State(entity_id, value, attributes)

    host = MagicMock()
    host.device_name = "Test BT"
    host.unique_id = "bt_test"
    host.model = None
    host.context = None
    host.hass.states.get = _state
    host.hass.services.async_call = AsyncMock(return_value=None)
    host.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID, model="TRVZB", advanced={})}
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


async def _adopts(lookup: Lookup, candidate: Any) -> bool:
    """Run ``lookup`` against a TRV and ``candidate`` in one config entry."""
    trv = make_registry_entry(TRV_ID, device_id=candidate.device_id)
    registry = make_entity_registry(trv, candidate)
    host = _host(registry, lookup, candidate.entity_id)
    with _registry_in_place(registry):
        return await lookup.adopts(host, registry, candidate.entity_id)


def _params(adopted_by: frozenset[str], mark: Any, skip=frozenset()) -> list[Any]:
    return [
        pytest.param(name, id=name, marks=[mark] if name in adopted_by else [])
        for name in LOOKUPS
        if name not in skip
    ]


@pytest.mark.parametrize("name", list(LOOKUPS))
@pytest.mark.asyncio
async def test_an_enabled_sibling_is_taken_up(name):
    """The TRV's enabled sibling on its device is found.

    This is the control for the two requirements below: each lookup is live
    against the registry it is handed, so a lookup that passes over the
    candidate there does so because of the candidate.
    """
    lookup = LOOKUPS[name]
    candidate = make_registry_entry(
        lookup.candidate, device_id=TRV_DEVICE, **lookup.fields
    )

    assert await _adopts(lookup, candidate), f"{name} passed over {candidate.entity_id}"


@pytest.mark.parametrize("disabled_by", DISABLED_BY, ids=lambda d: d.value)
@pytest.mark.parametrize(
    "name", _params(DISABLED_ENTRY_ADOPTED_BY, ADOPTS_A_DISABLED_ENTRY)
)
@pytest.mark.asyncio
async def test_a_disabled_sibling_is_not_taken_up(name, disabled_by):
    """A disabled entry on the TRV's device is no sibling.

    Home Assistant does not load a disabled entity: it has no state, and a
    service call aimed at it is dropped. It must not be offered as a
    capability, returned as the TRV's helper entity, or written to.
    """
    lookup = LOOKUPS[name]
    candidate = make_registry_entry(
        lookup.candidate, device_id=TRV_DEVICE, disabled_by=disabled_by, **lookup.fields
    )

    assert not await _adopts(lookup, candidate), (
        f"{name} took the disabled {candidate.entity_id}"
    )


@pytest.mark.parametrize(
    "name",
    _params(
        DEVICE_LESS_ENTRY_ADOPTED_BY,
        MATCHES_ANY_DEVICE_LESS_ENTRY,
        skip=TAKES_ITS_DEVICE_FROM_THE_CALLER,
    ),
)
@pytest.mark.asyncio
async def test_a_trv_without_a_device_has_no_siblings(name):
    """A TRV that belongs to no device shares a device with nothing.

    Another entity the same integration registered without a device is a
    stranger, however well its name matches: writing a calibration or a
    valve position there moves someone else's hardware.
    """
    lookup = LOOKUPS[name]
    stranger = make_registry_entry(
        lookup.candidate.replace(".trv_", ".greenhouse_", 1),
        device_id=None,
        **lookup.fields,
    )

    assert not await _adopts(lookup, stranger), (
        f"{name} took the device-less {stranger.entity_id}"
    )


def test_every_lookup_the_tables_name_exists():
    """The expectation tables name only lookups this module runs.

    A name that matches no lookup marks nothing, and the table would then
    claim a defect that no test pins.
    """
    named = (
        DISABLED_ENTRY_ADOPTED_BY
        | DEVICE_LESS_ENTRY_ADOPTED_BY
        | TAKES_ITS_DEVICE_FROM_THE_CALLER
    )
    assert named <= LOOKUPS.keys()
