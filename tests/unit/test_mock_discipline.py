"""The adapter seam is mocked under the contract it replaces.

Independent hazards live at this seam, and they need different answers:

* A ``patch()`` without ``autospec`` accepts any call signature, so a
  shell call that drifts out of shape stays green. ``autospec=True``
  makes the mock reject it.
* An autospec mock still answers with a truthy mock. Where the shell
  reads the answer — "the write went out" against "this device has no
  such channel" — that turns a refused write into a completed one. Only
  an explicit ``return_value`` or ``side_effect`` fixes it.
* A ``side_effect`` assigned onto the mock afterwards wins over the
  ``return_value=`` given at the call, so a callback that falls off its
  end takes the stated answer back and hands the shell ``None``.

Both halves are derived rather than listed. The seam comes from the
production modules, so a new delegate or quirk function is covered the
day it is written. The patch spellings are matched on the dotted call
itself and on the module's own import names, so neither an alias nor a
``mock.patch.object`` can slip past. The one spelling this cannot read,
``patch.multiple``, is rejected outright instead of being passed over.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
import inspect
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

from custom_components.better_thermostat.adapters import delegate
from custom_components.better_thermostat.model_fixes import default as default_quirk

TESTS_ROOT = Path(__file__).resolve().parent.parent

# Functions whose answer the shell branches on. A truthy mock here does
# not merely blur a value, it selects the wrong path: the offset and
# valve channels take an accepted write to mean a command is in flight,
# and a quirk override reporting True suppresses the direct write that
# would otherwise follow.
ANSWER_IS_READ = frozenset(
    {
        "set_offset",
        "set_valve",
        "override_set_hvac_mode",
        "override_set_temperature",
        "override_set_valve",
        "get_current_offset",
    }
)


def _async_surface(module: ModuleType) -> set[str]:
    """Public coroutine functions the module itself defines."""
    return {
        name
        for name, obj in vars(module).items()
        if not name.startswith("_")
        and inspect.iscoroutinefunction(obj)
        and getattr(obj, "__module__", None) == module.__name__
    }


SEAM = _async_surface(delegate) | _async_surface(default_quirk)


class PatchSite(NamedTuple):
    """One ``patch()`` call aimed at the adapter seam."""

    path: Path
    lineno: int
    symbol: str
    keywords: dict[str, ast.expr]
    positional_new: bool
    async_defs: dict[str, list[ast.AsyncFunctionDef]]
    scoped_async_defs: dict[tuple[int, str], list[ast.AsyncFunctionDef]]
    bound_name: str | None
    late_side_effects: dict[tuple[int, str], list[str]]
    scope: int

    def __str__(self) -> str:
        rel = self.path.relative_to(TESTS_ROOT.parent)
        return f"{rel}:{self.lineno} patches {self.symbol}"


def _dict_constants(tree: ast.Module) -> dict[tuple[str, str], str]:
    """Module-level ``{"name": "target"}`` tables, keyed by (dict, key).

    The controlling tests keep their patch targets in such a table and
    subscript it at the call, so the target is not a literal there.
    """
    found: dict[tuple[str, str], str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found[(target.id, key.value)] = ast.unparse(value)
    return found


def _patch_target(arg: ast.expr, constants: dict[tuple[str, str], str]) -> str:
    """Source form of a patch target, resolved through a lookup table."""
    if isinstance(arg, ast.Constant):
        return str(arg.value)
    if (
        isinstance(arg, ast.Subscript)
        and isinstance(arg.value, ast.Name)
        and isinstance(arg.slice, ast.Constant)
        and isinstance(arg.slice.value, str)
    ):
        return constants.get((arg.value.id, arg.slice.value), ast.unparse(arg))
    return ast.unparse(arg)


def _patched_symbol(target: str) -> str:
    """Last dotted segment of a target, past any f-string prefix."""
    return target.rstrip("'\"").split(".")[-1].split("}")[-1]


def _patch_aliases(tree: ast.Module) -> set[str]:
    """Names under which ``unittest.mock.patch`` is callable in a module.

    A rule keyed on the spelling ``patch`` would step aside for
    ``from unittest.mock import patch as mock_patch``, so the names are
    read off the imports instead of assumed.
    """
    aliases = {"patch"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (
            "unittest.mock",
            "mock",
        ):
            for imported in node.names:
                if imported.name == "patch":
                    aliases.add(imported.asname or imported.name)
    return aliases


def _names_patch(expression: str, aliases: set[str]) -> bool:
    """Whether a dotted expression names ``unittest.mock.patch``.

    Either it is one of the module's own names for it, or it ends in
    ``.patch`` — which covers ``mock.patch``, ``unittest.mock.patch``
    and any other spelling of the module, without the rule having to
    resolve every import form.
    """
    return expression in aliases or expression.rpartition(".")[2] == "patch"


def _patch_form(node: ast.Call, aliases: set[str]) -> str | None:
    """Which ``patch`` spelling a call uses, or ``None`` if it is not one.

    ``plain`` covers ``patch("a.b.c")`` and ``mock.patch("a.b.c")``,
    where the target is the first argument. ``object`` covers
    ``patch.object(obj, "name")``, where it is the second. ``multiple``
    covers ``patch.multiple(obj, name=…)``, where the targets are the
    keywords. Each is recognised however the module reaches ``patch``,
    so ``mock.patch.object`` counts the same as ``patch.object``.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        tail = func.attr
    elif isinstance(func, ast.Name):
        tail = func.id
    else:
        return None
    # Cheap gate: every spelling ends in one of these, and unparsing the
    # rest of a large test file's call nodes is not worth the certainty.
    if tail not in ("patch", "object", "multiple") and tail not in aliases:
        return None
    source = ast.unparse(func)
    head, _, last = source.rpartition(".")
    if last in ("object", "multiple") and head and _names_patch(head, aliases):
        return last
    return "plain" if _names_patch(source, aliases) else None


def _bound_names(tree: ast.Module) -> dict[int, str]:
    """The ``as`` name of each ``with patch(…) as name`` call, by node id."""
    bound: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            if isinstance(item.optional_vars, ast.Name):
                bound[id(item.context_expr)] = item.optional_vars.id
    return bound


def _scopes(tree: ast.Module) -> dict[int, int]:
    """Map every node to the ``id`` of the function that encloses it.

    A module reuses one mock name across many tests, so a rule keyed on
    the name alone would report every test in the file for one offending
    assignment. The enclosing function is the scope that matters.
    """
    owner: dict[int, int] = {}

    def descend(node: ast.AST, current: int) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                id(child)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current
            )
            owner[id(child)] = current
            descend(child, inner)

    owner[id(tree)] = id(tree)
    descend(tree, id(tree))
    return owner


def _late_side_effects(
    tree: ast.Module, scopes: dict[int, int]
) -> dict[tuple[int, str], list[str]]:
    """``mock.side_effect = callback`` assignments, by (scope, mock name).

    An assignment made after the ``patch()`` call overrides whatever
    ``return_value=`` the call declared, so a rule reading only the call
    keywords would accept an answer the test then throws away. Every
    callback assigned to a name is kept: checking only the last would
    let the earlier ones through.
    """
    late: dict[tuple[int, str], list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Name):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "side_effect"
                and isinstance(target.value, ast.Name)
            ):
                key = (scopes.get(id(node), 0), target.value.id)
                late.setdefault(key, []).append(node.value.id)
    return late


def _iter_patch_sites() -> Iterator[PatchSite]:
    """Every ``patch()`` or ``patch.object()`` under tests/ that aims at the seam.

    ``patch.multiple`` is deliberately absent: its replacements are the
    keyword values, so neither rule below can read them.
    ``test_no_seam_patch_hides_in_an_unsupported_form`` rejects that
    spelling outright rather than letting it pass unexamined.
    """
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        source = path.read_text()
        if "patch" not in source:
            continue
        tree = ast.parse(source)
        aliases = _patch_aliases(tree)
        constants = _dict_constants(tree)
        bound = _bound_names(tree)
        scopes = _scopes(tree)
        late = _late_side_effects(tree, scopes)
        async_defs: dict[str, list[ast.AsyncFunctionDef]] = {}
        scoped_async_defs: dict[tuple[int, str], list[ast.AsyncFunctionDef]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                async_defs.setdefault(node.name, []).append(node)
                key = (scopes.get(id(node), 0), node.name)
                scoped_async_defs.setdefault(key, []).append(node)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            form = _patch_form(node, aliases)
            if form == "plain":
                target_index = 0
            elif form == "object":
                target_index = 1
            else:
                continue
            if len(node.args) <= target_index:
                continue
            symbol = _patched_symbol(_patch_target(node.args[target_index], constants))
            if symbol not in SEAM:
                continue
            yield PatchSite(
                path=path,
                lineno=node.lineno,
                symbol=symbol,
                keywords={kw.arg: kw.value for kw in node.keywords if kw.arg},
                positional_new=len(node.args) > target_index + 1,
                async_defs=async_defs,
                scoped_async_defs=scoped_async_defs,
                bound_name=bound.get(id(node)),
                late_side_effects=late,
                scope=scopes.get(id(node), 0),
            )


def _iter_multiple_sites() -> Iterator[str]:
    """Every ``patch.multiple`` under tests/ that names a seam symbol."""
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        source = path.read_text()
        if "multiple" not in source:
            continue
        tree = ast.parse(source)
        aliases = _patch_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _patch_form(node, aliases) != "multiple":
                continue
            for keyword in node.keywords:
                if keyword.arg in SEAM:
                    rel = path.relative_to(TESTS_ROOT.parent)
                    yield f"{rel}:{node.lineno} patches {keyword.arg}"


def _substitute_enforces_the_signature(site: PatchSite) -> bool:
    """Whether the replacement rejects a call of the wrong shape.

    ``autospec=True`` does it by copying the signature. A hand-written
    ``async def`` standing in for the function does it too — Python
    itself binds the arguments — as long as it spells its parameters out
    instead of swallowing them into ``*args``.
    """
    autospec = site.keywords.get("autospec")
    if isinstance(autospec, ast.Constant) and autospec.value is True:
        return True
    new = site.keywords.get("new")
    if not isinstance(new, ast.Name):
        return False
    definitions = site.async_defs.get(new.id)
    if not definitions:
        return False
    return all(
        d.args.vararg is None and d.args.kwarg is None and d.args.args
        for d in definitions
    )


def _substitute_states_its_answer(site: PatchSite) -> bool:
    """Whether the replacement answers something other than a mock."""
    if {"return_value", "side_effect"} & site.keywords.keys():
        return True
    new = site.keywords.get("new")
    return isinstance(new, ast.Name) and new.id in site.async_defs


def _returns_a_value(definition: ast.AsyncFunctionDef) -> bool:
    """Whether a coroutine ever hands back something other than ``None``.

    Returns inside a nested function belong to that function, not to
    this one, so they do not count.
    """
    nested = {
        id(inner)
        for node in ast.walk(definition)
        if node is not definition
        and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for inner in ast.walk(node)
    }
    return any(
        isinstance(node, ast.Return)
        and node.value is not None
        and id(node) not in nested
        for node in ast.walk(definition)
    )


def _late_side_effects_without_an_answer(site: PatchSite) -> list[str]:
    """Callbacks assigned onto this mock that fall off their end.

    ``mock.side_effect = callback`` wins over the ``return_value=`` given
    at the patch call, and a coroutine without a ``return`` answers
    ``None`` — which the shell reads as a refused write.
    """
    if site.bound_name is None:
        return []
    silent: list[str] = []
    for callback in site.late_side_effects.get((site.scope, site.bound_name), []):
        # The callback belonging to *this* test, not a same-named one from
        # a neighbouring test that happens to answer.
        definitions = site.scoped_async_defs.get(
            (site.scope, callback)
        ) or site.async_defs.get(callback)
        if definitions and not any(_returns_a_value(d) for d in definitions):
            silent.append(callback)
    return silent


def test_the_seam_is_reachable_from_here():
    """The derivation finds both halves of the seam and its patch sites.

    A typo in a module path would empty ``SEAM`` and pass every other
    test in this file without checking anything.
    """
    assert {"set_offset", "set_valve", "get_current_offset"} <= SEAM
    assert {"override_set_hvac_mode", "override_set_temperature"} <= SEAM
    assert len(list(_iter_patch_sites())) > 100


def test_every_symbol_whose_answer_is_read_is_part_of_the_seam():
    """The branching list names functions that still exist.

    Renaming one in production would otherwise drop it out of the rule
    silently instead of failing here.
    """
    assert ANSWER_IS_READ <= SEAM


def test_no_seam_patch_leaves_the_signature_unchecked():
    """Every stand-in rejects a call the real function would reject."""
    offenders = [
        f"{site} without autospec=True"
        for site in _iter_patch_sites()
        if site.positional_new or not _substitute_enforces_the_signature(site)
    ]
    assert not offenders, "unspecced patches of the adapter seam:\n" + "\n".join(
        offenders
    )


def test_no_seam_patch_answers_with_a_bare_mock_where_the_shell_reads_it():
    """Every stand-in the shell interrogates states its answer.

    Without it the mock answers with a truthy mock, and the path taken
    for "the write went out" also runs for a device that has no such
    channel.
    """
    offenders = [
        f"{site} without return_value= or side_effect="
        for site in _iter_patch_sites()
        if site.symbol in ANSWER_IS_READ and not _substitute_states_its_answer(site)
    ]
    assert not offenders, "adapter-seam patches with an unstated answer:\n" + "\n".join(
        offenders
    )


def test_no_late_side_effect_takes_the_answer_back():
    """A callback assigned after the patch call still has to answer.

    ``patch(…, return_value=True)`` followed by ``mock.side_effect = cb``
    reads as if the answer were stated, but ``side_effect`` wins and a
    coroutine that falls off its end answers ``None``. The shell then
    runs its refused-write path while the test believes it is watching
    the accepted one.
    """
    offenders = [
        f"{site} then {callback}() overrides the answer with None"
        for site in _iter_patch_sites()
        if site.symbol in ANSWER_IS_READ
        for callback in _late_side_effects_without_an_answer(site)
    ]
    assert not offenders, (
        "adapter-seam mocks whose answer is taken back:\n" + "\n".join(offenders)
    )


def test_no_seam_patch_hides_in_an_unsupported_form():
    """``patch.multiple`` may not stand in for a seam function.

    Its replacements are the keyword values, so the two rules above
    cannot read a signature or an answer off them. Rejecting the
    spelling keeps the guard sound; the alternative — passing it over
    quietly — is what a rule aimed only at ``patch(…)`` already did.
    """
    offenders = list(_iter_multiple_sites())
    assert not offenders, (
        "seam functions patched through patch.multiple, which this rule "
        "cannot inspect — use patch() or patch.object() instead:\n"
        + "\n".join(offenders)
    )
