"""The adapter seam is mocked under the contract it replaces.

Two independent hazards live at this seam, and they need two different
answers:

* A ``patch()`` without ``autospec`` accepts any call signature, so a
  shell call that drifts out of shape stays green. ``autospec=True``
  makes the mock reject it.
* An autospec mock still answers with a truthy mock. Where the shell
  reads the answer — "the write went out" against "this device has no
  such channel" — that turns a refused write into a completed one. Only
  an explicit ``return_value`` or ``side_effect`` fixes it.

The seam is derived from the production modules rather than listed here,
so a new delegate or quirk function is covered the day it is written.
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


def _iter_patch_sites() -> Iterator[PatchSite]:
    """Every ``patch()`` under tests/ that aims at the seam."""
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        source = path.read_text()
        if "patch(" not in source:
            continue
        tree = ast.parse(source)
        constants = _dict_constants(tree)
        async_defs: dict[str, list[ast.AsyncFunctionDef]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                async_defs.setdefault(node.name, []).append(node)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if name != "patch":
                continue
            symbol = _patched_symbol(_patch_target(node.args[0], constants))
            if symbol not in SEAM:
                continue
            yield PatchSite(
                path=path,
                lineno=node.lineno,
                symbol=symbol,
                keywords={kw.arg: kw.value for kw in node.keywords if kw.arg},
                positional_new=len(node.args) > 1,
                async_defs=async_defs,
            )


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
