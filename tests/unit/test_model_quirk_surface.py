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
import functools
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
    "trv_state_unknown_as_available",
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
    "trv_state_unknown_as_available": ((), bool),
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
    """Each parameter's name, kind and default, and the coroutine flag.

    Names alone would let a model change a parameter's kind or drop a
    default and still match, neither of which the positional dispatch
    would survive. Annotations are deliberately left out: the dispatch
    never reads them, and the modules carry them unevenly, so comparing
    them would report typing coverage as a signature mismatch.
    """
    return (
        tuple(
            (parameter.name, parameter.kind, parameter.default)
            for parameter in inspect.signature(func).parameters.values()
        ),
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


QUIRK_ATTRIBUTE = "model_quirks"
QUIRK_LOADER = "load_model_quirks"
QUIRK_PACKAGE = "model_fixes"
# Past every source position, for asking what a scope ends up holding.
END_OF_SCOPE = (float("inf"), 0)


def _is_a_quirk_module(node, holders=frozenset()):
    """Whether an expression evaluates to a quirk module itself.

    Only the wrappers the shell actually puts around one are unwrapped:
    an ``await``, a guard against a missing TRV, a fallback chain. What
    a quirk function *returns* is not a quirk module, so a call only
    counts when it is the loader, and a bare name only when it was bound
    from one of these in the first place.
    """
    if isinstance(node, ast.Await):
        return _is_a_quirk_module(node.value, holders)
    if isinstance(node, ast.IfExp):
        return _is_a_quirk_module(node.body, holders) or _is_a_quirk_module(
            node.orelse, holders
        )
    if isinstance(node, ast.BoolOp):
        return any(_is_a_quirk_module(value, holders) for value in node.values)
    if isinstance(node, ast.Attribute):
        return node.attr == QUIRK_ATTRIBUTE
    if isinstance(node, ast.Name):
        return node.id in holders
    if isinstance(node, ast.Call):
        called = node.func
        name = (
            called.attr
            if isinstance(called, ast.Attribute)
            else getattr(called, "id", None)
        )
        return name == QUIRK_LOADER
    return False


NESTS_A_SCOPE = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    # A comprehension's target is local to it, whether or not the
    # comprehension itself is inlined into the enclosing frame.
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _within_scope(node):
    """Every node belonging to one scope.

    A nested scope is yielded but not entered: its own name binds out
    here, while everything inside it belongs to that scope. The one
    exception is the leftmost iterable of a comprehension, which Python
    evaluates in the enclosing scope before the comprehension runs.
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if isinstance(child, NESTS_A_SCOPE):
            if isinstance(child, COMPREHENSIONS) and child.generators:
                outermost = child.generators[0].iter
                yield outermost
                yield from _within_scope(outermost)
            continue
        yield from _within_scope(child)


def _scopes_inside(node):
    """The scopes opened directly within one scope."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, NESTS_A_SCOPE):
            yield child
        else:
            yield from _scopes_inside(child)


def _names_bound_by(node):
    """Names a statement binds without an ``ast.Name`` store node.

    An import, an ``except ... as``, a ``match`` capture and a nested
    ``def`` or ``class`` all put a name in the scope, and any of them can
    shadow a quirk holder.
    """
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(".", 1)[0] for alias in node.names]
    if isinstance(node, ast.ExceptHandler):
        return [node.name] if node.name else []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _quirk_holding_names(scope):
    """Names bound to a quirk module in one scope, and where.

    The shell rarely dispatches on the module expression directly: it
    binds ``<trv>.model_quirks`` to a local first. A local bound from
    another local is not followed, so nothing derived from a dispatch's
    result is mistaken for the module.
    """
    bound_to_a_quirk = {}
    for node in _within_scope(scope):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        # An assignment takes effect only once its right-hand side has been
        # read, so ``quirks = quirks.real()`` still reaches the old binding.
        takes_effect = (
            getattr(node.value, "end_lineno", node.value.lineno),
            getattr(node.value, "end_col_offset", node.value.col_offset),
        )
        holds_one = _is_a_quirk_module(node.value)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for inner in ast.walk(target):
                if isinstance(inner, ast.Name):
                    bound_to_a_quirk[id(inner)] = (takes_effect, holds_one)

    events = []
    for node in _within_scope(scope):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            position, holds_one = bound_to_a_quirk.get(
                id(node), (_position(node), False)
            )
            events.append((position, node.id, holds_one))
    events += [
        (_position(node), name, False)
        for node in _within_scope(scope)
        for name in _names_bound_by(node)
    ]
    events += [(_position(scope), name, False) for name in _parameter_names(scope)]
    # A comprehension's targets are bound inside it, before anything is read.
    if isinstance(scope, COMPREHENSIONS):
        events += [
            (_position(scope), inner.id, False)
            for generator in scope.generators
            for inner in ast.walk(generator.target)
            if isinstance(inner, ast.Name)
        ]
    events.sort()
    return events


def _position(node):
    """Where a node sits in the source, ordered as it is written.

    A line is not a position: ``quirks = trv.model_quirks; quirks = other``
    binds the same name twice on one, and only the column separates the
    binding that holds a module from the one that replaces it.
    """
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _parameter_names(scope):
    """Every name a callable scope binds through its own parameters."""
    arguments = getattr(scope, "args", None)
    if not isinstance(arguments, ast.arguments):
        return []
    slots = (
        arguments.posonlyargs
        + arguments.args
        + arguments.kwonlyargs
        + [arguments.vararg, arguments.kwarg]
    )
    return [slot.arg for slot in slots if slot is not None]


def _names_reached_for_in(scope, inherited, names):
    """Collect one scope's quirk-module lookups, then its inner scopes.

    Holders are resolved per scope rather than per file: ``quirks`` is an
    ordinary local name, and one function's binding says nothing about
    another's. Within a scope the latest binding at or above the
    reference decides, so rebinding the name to something else revokes
    it, and a parameter of that name never held a module to begin with.
    An enclosing scope's binding stays visible, since a nested function
    runs after it — except across a class body, whose locals methods do
    not close over.
    """
    events = _quirk_holding_names(scope)
    bound_here = {name for _, name, _ in events}
    # Anything bound in this scope shadows the enclosing binding entirely,
    # whatever that one held.
    holders = {name for name in inherited if name not in bound_here}

    def holds_a_quirk(name, position):
        if name in bound_here:
            reached = [
                event for event in events if event[1] == name and event[0] <= position
            ]
            return bool(reached) and reached[-1][2]
        return name in holders

    def reaches(node):
        line = _position(node)
        visible = frozenset(
            name for name in bound_here | holders if holds_a_quirk(name, line)
        )
        return _is_a_quirk_module(node, visible)

    for node in _within_scope(scope):
        if isinstance(node, ast.Attribute):
            if reaches(node.value):
                names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            if QUIRK_PACKAGE in (node.module or "").split("."):
                names.update(alias.asname or alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("hasattr", "getattr")
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and reaches(node.args[0])
        ):
            names.add(node.args[1].value)

    # What a nested scope closes over is the state at the end of this one,
    # since it runs after the bindings here have executed.
    at_the_end = holders | {
        name for name in bound_here if holds_a_quirk(name, END_OF_SCOPE)
    }
    for inner in _scopes_inside(scope):
        # A method does not see the class body's locals, but does still see
        # whatever the class was defined inside.
        _names_reached_for_in(
            inner, holders if isinstance(scope, ast.ClassDef) else at_the_end, names
        )


def _names_reached_for(path):
    """Every quirk-module name a file's code reaches for.

    Read as source text a name also "appears" in the comment that
    explains a dispatch, in the docstring above it and in an unrelated
    string literal — so removing the dispatch itself would go unnoticed.
    Read as any AST name it also matches an unrelated symbol that happens
    to share the spelling. What is left is a name reached for *on a quirk
    module*: an attribute of one, the string a ``hasattr``/``getattr``
    looks up on one, or a name imported straight out of the package.
    """
    names = set()
    _names_reached_for_in(ast.parse(path.read_text()), {}, names)
    return names


@functools.cache
def _shell_dispatch_surface():
    """Every name the production code outside the quirk modules reaches."""
    names = set()
    for path in PRODUCTION_ROOT.rglob("*.py"):
        if path.parent == QUIRKS_DIR:
            continue
        names |= _names_reached_for(path)
    return names


def _dispatched_from_the_shell(name):
    """Whether production code outside the quirk modules reaches for it."""
    return name in _shell_dispatch_surface()


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


# What the scan must make of each way a name can or cannot hold a quirk
# module. ``real`` is a name it has to find; ``leaked`` is one it must
# not, because at that point the name holds something else or nothing.
SCOPE_CASES = [
    (
        "a sibling function's local of the same name",
        """
def dispatches(trv):
    quirks = trv.model_quirks
    return quirks.real()

def does_not(other):
    quirks = other.unrelated
    return quirks.leaked()
""",
        {"real"},
    ),
    (
        "a reference above the binding",
        """
def reads_too_early(trv):
    answer = quirks.leaked()
    quirks = trv.model_quirks
    return answer
""",
        set(),
    ),
    (
        "a name rebound to something else",
        """
def rebinds(trv, other):
    quirks = trv.model_quirks
    answer = quirks.real()
    quirks = other.unrelated
    return answer, quirks.leaked()
""",
        {"real"},
    ),
    (
        "a name rebound from a dispatch on itself",
        """
def rebinds_from_itself(trv):
    quirks = trv.model_quirks
    quirks = quirks.real()
    return quirks
""",
        {"real"},
    ),
    (
        "a name bound twice on one line",
        """
def rebinds_on_one_line(trv, other):
    quirks = trv.model_quirks; quirks = other.unrelated
    return quirks.leaked()
""",
        set(),
    ),
    (
        "a parameter shadowing the enclosing binding",
        """
def shadows(trv):
    quirks = trv.model_quirks
    def inner(quirks):
        return quirks.leaked()
    return inner
""",
        set(),
    ),
    (
        "a comprehension target of the same name",
        """
def iterates(trv, values):
    quirks = trv.model_quirks
    return [quirks.leaked() for quirks in values]
""",
        set(),
    ),
    (
        "a comprehension that only reads the binding",
        """
def iterates(trv, values):
    quirks = trv.model_quirks
    return [quirks.real() for value in values]
""",
        {"real"},
    ),
    (
        "a comprehension whose leftmost iterable reads the shadowed binding",
        """
def iterates(trv):
    quirks = trv.model_quirks
    return [value for quirks in quirks.real()]
""",
        {"real"},
    ),
    (
        "an import shadowing the binding",
        """
def imports(trv):
    quirks = trv.model_quirks
    import quirks
    return quirks.leaked()
""",
        set(),
    ),
    (
        "an except alias shadowing the binding",
        """
def catches(trv):
    quirks = trv.model_quirks
    try:
        pass
    except ValueError as quirks:
        return quirks.leaked()
""",
        set(),
    ),
    (
        "a nested def shadowing the binding",
        """
def redefines(trv):
    quirks = trv.model_quirks
    def quirks():
        pass
    return quirks.leaked()
""",
        set(),
    ),
    (
        "a match capture shadowing the binding",
        """
def matches(trv, value):
    quirks = trv.model_quirks
    match value:
        case {"key": quirks}:
            return quirks.leaked()
""",
        set(),
    ),
    (
        "a class body's local read in its method",
        """
def encloses(trv):
    class Holder:
        quirks = trv.model_quirks
        def method(self):
            return quirks.leaked()
    return Holder
""",
        set(),
    ),
    (
        "a closure over the enclosing binding",
        """
def encloses(trv):
    quirks = trv.model_quirks
    def inner():
        return quirks.real()
    return inner
""",
        {"real"},
    ),
    (
        "a method closing over the function the class sits in",
        """
def encloses(trv):
    quirks = trv.model_quirks
    class Holder:
        def method(self):
            return quirks.real()
    return Holder
""",
        {"real"},
    ),
    (
        "a nested function defined above the binding it closes over",
        """
def encloses(trv):
    def inner():
        return quirks.real()
    quirks = trv.model_quirks
    return inner
""",
        {"real"},
    ),
]


class TestTheScanReadsPythonsOwnScoping:
    """``quirks`` is an ordinary local name, so the scan has to scope it.

    Enough of Python's binding rules live in ``_names_reached_for`` now
    that they are worth pinning here directly: every case below is one
    the shell could grow, and getting any of them wrong makes the two
    tests above pass without a dispatch behind them.
    """

    @pytest.mark.parametrize(
        ("source", "expected"),
        [(source, expected) for _label, source, expected in SCOPE_CASES],
        ids=[label for label, _source, _expected in SCOPE_CASES],
    )
    def test_only_a_lookup_on_a_quirk_module_counts(self, tmp_path, source, expected):
        """A name reached where it holds no module is not a dispatch."""
        probe = tmp_path / "probe.py"
        probe.write_text(source)

        assert _names_reached_for(probe) == expected
