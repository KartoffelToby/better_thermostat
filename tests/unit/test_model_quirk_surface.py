"""What a quirk module may define, and what it must.

Twelve modules extend Better Thermostat for one device family each, and
the shell reaches them through a duck-typed dispatch: an attribute lookup
on whichever module ``load_model_quirks`` imported. Nothing checks the
result. A module that spells a name wrong either crashes the calibration
path or, where the dispatch guards with ``hasattr``, silently does
nothing at all — and the quiet half is the dangerous one.

So the surface is written down here, in both directions:

* everything the dispatch reaches *unguarded* must exist in every module,
  because an absent name is an ``AttributeError`` mid-cycle;
* everything a module defines must be something somebody calls, or it is
  a quirk that was written and never wired up;
* an implementation must match ``default.py`` in signature and in whether
  it is a coroutine, because the dispatch awaits some of these and not
  others;
* and a quirk may only read host attributes that ``ModelFixHost``
  promises, since that Protocol is the only written record of what the
  host owes it.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.model_fixes import default as default_quirk
from custom_components.better_thermostat.model_fixes.types import ModelFixHost
from custom_components.better_thermostat.trv import Trv

QUIRKS_DIR = Path(default_quirk.__file__).parent
PRODUCTION_ROOT = QUIRKS_DIR.parent
NOT_A_MODEL = {"__init__", "model_quirks", "types", "default"}

ENTITY_ID = "climate.trv"

# Reached as a plain attribute by the shims in model_quirks.py, so a module
# that omits one raises AttributeError in the middle of a control cycle.
REQUIRED = (
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "override_set_hvac_mode",
    "override_set_temperature",
)
# Reached through hasattr/getattr, so a module may leave these out — and a
# typo in one of them is invisible rather than loud.
OPTIONAL = (
    "fix_valve_calibration",
    "override_set_valve",
    "initial_tweak",
    "maybe_set_external_temperature",
)
SURFACE = REQUIRED + OPTIONAL

# One representative argument tuple per surface function, and what the
# function's own contract says it hands back.
CALL_CONTRACT = {
    "fix_local_calibration": ((0.5,), float),
    "fix_valve_calibration": ((50,), (int, float)),
    "fix_target_temperature_calibration": ((21.0,), float),
    "override_set_hvac_mode": (("heat",), bool),
    "override_set_temperature": ((21.0,), bool),
    "override_set_valve": ((50,), bool),
    "initial_tweak": ((), type(None)),
}


def _model_modules():
    """Import every per-model quirk module, keyed by its file stem."""
    return {
        path.stem: importlib.import_module(
            f"custom_components.better_thermostat.model_fixes.{path.stem}"
        )
        for path in sorted(QUIRKS_DIR.glob("*.py"))
        if path.stem not in NOT_A_MODEL
    }


MODEL_MODULES = _model_modules()
MODEL_IDS = sorted(MODEL_MODULES)


def _defined_functions(module):
    """Public functions the module defines itself, by name."""
    return {
        name: obj
        for name, obj in vars(module).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == module.__name__
    }


def _host():
    """A host whose contracted attributes carry values, not mocks.

    Every attribute ``ModelFixHost`` promises is filled with something of
    the promised shape, so a quirk that trips over one is tripping over
    the contract and not over the fixture.
    """
    host = MagicMock()
    host.device_name = "Test BT"
    host.context = None
    host.cur_temp = 19.5
    host.bt_target_temp = 21.0
    host.hass = MagicMock()
    host.hass.services.async_call = AsyncMock()
    host.hass.states.get = lambda requested: State(
        requested,
        "heat",
        {
            "temperature": 20.0,
            "current_temperature": 19.5,
            "min": 0,
            "max": 100,
            "step": 1,
            "local_temperature_calibration": 0.0,
        },
    )
    trv = Trv(entity_id=ENTITY_ID)
    trv.model = "generic"
    trv.local_temperature_calibration_entity = "number.trv_calibration"
    trv.valve_position_entity = "number.trv_valve"
    trv.valve_position_writable = True
    trv.local_calibration_min = -10.0
    trv.local_calibration_max = 10.0
    trv.last_hvac_mode = "heat"
    trv.last_temperature = 20.0
    host.real_trvs = {ENTITY_ID: trv}
    return host


def _signature(func):
    """Parameter names and whether the function is a coroutine."""
    return (
        tuple(inspect.signature(func).parameters),
        inspect.iscoroutinefunction(func),
    )


def _self_attributes_read(path):
    """Every ``self.<name>`` a module's top-level functions reach for."""
    tree = ast.parse(path.read_text())
    names = set()
    for func in tree.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = func.args.args
        if not arguments or arguments[0].arg != "self":
            continue
        names.update(
            node.attr
            for node in ast.walk(func)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )
    return names


def _dispatched_from_the_shell(name):
    """Whether production code outside the quirk modules reaches for it."""
    return any(
        name in path.read_text()
        for path in PRODUCTION_ROOT.rglob("*.py")
        if path.parent != QUIRKS_DIR
    )


def _called_within(path, name):
    """Whether the module calls its own function somewhere."""
    tree = ast.parse(path.read_text())
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
        for node in ast.walk(tree)
    )


def _implemented_pairs():
    """Every (model, surface function) pair that actually exists."""
    return [
        (model, name)
        for model in MODEL_IDS
        for name in SURFACE
        if hasattr(MODEL_MODULES[model], name)
    ]


IMPLEMENTED = _implemented_pairs()
IMPLEMENTED_IDS = [f"{model}-{name}" for model, name in IMPLEMENTED]
CALLABLE_PAIRS = [pair for pair in IMPLEMENTED if pair[1] in CALL_CONTRACT]
CALLABLE_IDS = [f"{model}-{name}" for model, name in CALLABLE_PAIRS]


class TestTheDispatchAlwaysFindsWhatItReachesFor:
    """The unguarded half of the surface exists in every module."""

    @pytest.mark.parametrize("model", MODEL_IDS)
    @pytest.mark.parametrize("name", REQUIRED)
    def test_a_required_function_is_defined(self, model, name):
        """An absent name is an AttributeError mid control cycle."""
        assert hasattr(MODEL_MODULES[model], name)

    @pytest.mark.parametrize("name", SURFACE)
    def test_the_surface_list_matches_what_the_shims_reach_for(self, name):
        """The list here is the one the dispatch actually uses.

        Every entry is either a name ``default.py`` defines or one the
        shell reaches for by ``hasattr``; a surface function nobody
        dispatches would make the rest of this file test nothing.
        """
        if hasattr(default_quirk, name):
            return
        assert _dispatched_from_the_shell(name), f"{name} is dispatched from nowhere"


class TestAnImplementationMatchesTheDefault:
    """A model's version is callable wherever the default one is."""

    @pytest.mark.parametrize(("model", "name"), IMPLEMENTED, ids=IMPLEMENTED_IDS)
    def test_the_signature_and_kind_match(self, model, name):
        """The dispatch awaits some of these and calls others plainly."""
        reference = getattr(default_quirk, name, None)
        if reference is None:
            pytest.skip(f"{name} has no counterpart in default.py")
        assert _signature(getattr(MODEL_MODULES[model], name)) == _signature(reference)


class TestNothingIsDefinedThatNobodyCalls:
    """The other direction: a quirk written but never wired up."""

    @pytest.mark.parametrize("model", MODEL_IDS)
    def test_every_public_function_is_reachable(self, model):
        """Surface, dispatched from the shell, or used inside the module.

        The third case is the model's own helper; the failure this guards
        against is a quirk that was written and never wired up at all.
        """
        path = QUIRKS_DIR / f"{model}.py"
        orphans = [
            name
            for name in _defined_functions(MODEL_MODULES[model])
            if name not in SURFACE
            and not _dispatched_from_the_shell(name)
            and not _called_within(path, name)
        ]
        assert not orphans, f"{model} defines unreachable {orphans}"


class TestAQuirkOnlyReadsWhatTheHostPromises:
    """``ModelFixHost`` is the only written record of that contract."""

    @pytest.mark.parametrize("model", MODEL_IDS)
    def test_no_undeclared_host_attribute_is_read(self, model):
        """A quirk reaching past the Protocol reaches for luck."""
        promised = set(ModelFixHost.__annotations__) | {
            name for name in vars(ModelFixHost) if not name.startswith("_")
        }
        read = _self_attributes_read(QUIRKS_DIR / f"{model}.py")

        assert not read - promised, (
            f"{model} reads {sorted(read - promised)}, which ModelFixHost "
            f"does not promise"
        )


class TestEveryImplementationSurvivesBeingCalled:
    """Six of the twelve modules have no test of their own."""

    @pytest.mark.parametrize(("model", "name"), CALLABLE_PAIRS, ids=CALLABLE_IDS)
    def test_it_returns_what_its_contract_declares(self, model, name):
        """Called against a contract-shaped host, it answers in kind."""
        arguments, expected = CALL_CONTRACT[name]

        answer = getattr(MODEL_MODULES[model], name)(_host(), ENTITY_ID, *arguments)
        if inspect.iscoroutine(answer):
            answer = asyncio.run(answer)

        if expected is type(None):
            assert answer is None
        else:
            assert isinstance(answer, expected)
            assert not isinstance(answer, bool) or expected is bool
