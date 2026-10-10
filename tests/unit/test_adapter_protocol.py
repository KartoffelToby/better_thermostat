"""Which modules the delegate accepts as an ecosystem adapter.

An adapter is loaded by name at runtime, so nothing at the call site says
what the loaded module offers. The delegate holds every module it loads
against the adapter protocol and serves an integration whose module falls
short through the generic adapter, the way it serves one with no module
at all. The type checker holds the signatures through the conformance
module, which only helps for an adapter that module names.
"""

import importlib
import logging
import pkgutil
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.better_thermostat import adapters
from custom_components.better_thermostat.adapters import delegate, generic, mqtt
from custom_components.better_thermostat.adapters.conformance import ECOSYSTEM_ADAPTERS
from custom_components.better_thermostat.adapters.types import TrvAdapter
from tests.factories import ThermostatStandIn

ENTITY_ID = "climate.trv"

_DELEGATE = "custom_components.better_thermostat.adapters.delegate"


def _ecosystem_modules() -> dict[str, ModuleType]:
    """Every module of the adapter package that declares its capabilities."""
    modules = {}
    for info in pkgutil.iter_modules(adapters.__path__):
        module = importlib.import_module(f"{adapters.__name__}.{info.name}")
        if hasattr(module, "CAPABILITIES"):
            modules[info.name] = module
    return modules


def _without(module: ModuleType, member: str) -> ModuleType:
    """A copy of an adapter module that lacks one member."""
    copy = ModuleType(f"{module.__name__}_without_{member}")
    copy.__dict__.update(
        (name, value) for name, value in vars(module).items() if name != member
    )
    return copy


def _thermostat() -> ThermostatStandIn:
    thermostat = ThermostatStandIn()
    thermostat.device_name = "Test BT"
    return thermostat


def test_the_type_checker_sees_every_ecosystem_adapter():
    """An adapter left out of the conformance module escapes the signature check."""
    assert set(_ecosystem_modules()) == set(ECOSYSTEM_ADAPTERS)


@pytest.mark.parametrize("name", sorted(ECOSYSTEM_ADAPTERS))
def test_every_ecosystem_adapter_passes_the_runtime_check(name):
    """The delegate's check accepts each adapter that ships."""
    assert isinstance(ECOSYSTEM_ADAPTERS[name], TrvAdapter)


@pytest.mark.parametrize("member", ["CAPABILITIES", "set_valve", "get_info"])
def test_a_module_lacking_a_member_fails_the_runtime_check(member):
    """The check reads every member of the protocol, constants included."""
    module: object = _without(mqtt, member)
    assert not isinstance(module, TrvAdapter)


def test_helper_modules_are_no_adapters():
    """The delegate and the shared helpers do not pass for an ecosystem."""
    for name in ("base", "delegate", "types", "valve_entity"):
        module: object = importlib.import_module(f"{adapters.__name__}.{name}")
        assert not isinstance(module, TrvAdapter), name


@pytest.mark.asyncio
async def test_a_conforming_adapter_is_served_as_loaded():
    """An ecosystem whose module offers the whole protocol gets that module."""
    imports = AsyncMock(return_value=mqtt)

    with patch(f"{_DELEGATE}.async_import_module", imports):
        adapter = await delegate.load_adapter(_thermostat(), "mqtt", ENTITY_ID)

    assert adapter is mqtt
    imports.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_adapter_lacking_a_member_is_served_by_the_generic_one(caplog):
    """A module that falls short reads like an unsupported ecosystem.

    The traceback names the missing member's protocol, so the log tells
    a broken module apart from an integration that has none.
    """
    imports = AsyncMock(side_effect=[_without(mqtt, "set_valve"), generic])

    with (
        caplog.at_level(logging.DEBUG),
        patch(f"{_DELEGATE}.async_import_module", imports),
    ):
        adapter = await delegate.load_adapter(_thermostat(), "mqtt", ENTITY_ID)

    assert adapter is generic
    assert "AdapterContractError" in caplog.text


@pytest.mark.asyncio
async def test_a_generic_adapter_lacking_a_member_is_refused():
    """Nothing stands behind the generic adapter, so its shortfall surfaces."""
    imports = AsyncMock(
        side_effect=[ImportError("no such adapter"), _without(generic, "init")]
    )

    with (
        patch(f"{_DELEGATE}.async_import_module", imports),
        pytest.raises(delegate.AdapterContractError),
    ):
        await delegate.load_adapter(_thermostat(), "unknown", ENTITY_ID)
