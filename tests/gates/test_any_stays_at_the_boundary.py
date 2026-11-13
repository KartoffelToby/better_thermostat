"""``typing.Any`` appears in the integration only where a signature it does not own puts it.

``Any`` switches the type checker off for every value that passes through it,
and nothing at the use site says so. The integration types what it reads at
its boundaries instead: settings and stores as ``Mapping[str, object]``
narrowed by parsers, adapters and model quirks through module protocols.
What is left are two places where the shape is not the integration's to
choose:

- ``**kwargs: Any`` on a method marked ``@override``. Home Assistant declares
  the service handlers that way, and an override has to accept what the base
  passes.
- The yield and send arguments of ``Coroutine[Any, Any, T]``. A coroutine
  handed to a task is only ever awaited, so those two slots carry nothing the
  integration reads; the result type ``T`` stays checked.

Every other ``Any`` fails this test. A merge that brings an untyped version
of a file back fails it too, which a passing type check does not catch: the
untyped code was type-correct.
"""

import ast
from pathlib import Path

PACKAGE = (
    Path(__file__).resolve().parents[2] / "custom_components" / "better_thermostat"
)


def _is_any(node: ast.AST, any_names: set[str], typing_modules: set[str]) -> bool:
    """Whether a node spells ``typing.Any``, under any name it was imported as."""
    if isinstance(node, ast.Name):
        return node.id in any_names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "Any"
        and isinstance(node.value, ast.Name)
        and node.value.id in typing_modules
    )


def _typing_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Answer the names ``Any`` and the ``typing`` module are bound to in a file."""
    any_names: set[str] = set()
    typing_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            any_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "Any"
            )
        elif isinstance(node, ast.Import):
            typing_modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "typing"
            )
    return any_names, typing_modules


def _is_override(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        (isinstance(decorator, ast.Name) and decorator.id == "override")
        or (isinstance(decorator, ast.Attribute) and decorator.attr == "override")
        for decorator in function.decorator_list
    )


def _allowed_nodes(tree: ast.Module) -> set[int]:
    """Answer the ids of the annotation nodes allowed to be ``Any``."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and _is_override(node)
            and node.args.kwarg is not None
            and node.args.kwarg.annotation is not None
        ):
            allowed.add(id(node.args.kwarg.annotation))
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name | ast.Attribute)
            and (node.value.id if isinstance(node.value, ast.Name) else node.value.attr)
            == "Coroutine"
            and isinstance(node.slice, ast.Tuple)
            and len(node.slice.elts) == 3
        ):
            allowed.update(id(element) for element in node.slice.elts[:2])
    return allowed


def _unexplained_any(source: str) -> list[int]:
    """Answer the lines of every ``Any`` in a module outside the two allowed places."""
    tree = ast.parse(source)
    any_names, typing_modules = _typing_aliases(tree)
    allowed = _allowed_nodes(tree)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
        and id(node) not in allowed
        and _is_any(node, any_names, typing_modules)
    )


def test_any_appears_only_where_a_foreign_signature_puts_it():
    offenders = [
        f"{path.relative_to(PACKAGE.parents[1])}:{line}"
        for path in sorted(PACKAGE.rglob("*.py"))
        for line in _unexplained_any(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, (
        "typing.Any outside an @override **kwargs or a Coroutine's yield/send "
        "slots:\n  " + "\n  ".join(offenders)
    )


def test_the_two_foreign_signatures_pass():
    source = (
        "from collections.abc import Coroutine\n"
        "from typing import Any, override\n"
        "class Entity:\n"
        "    @override\n"
        "    async def async_turn_on(self, **kwargs: Any) -> None: ...\n"
        "def spawn[T](coro: Coroutine[Any, Any, T]) -> T: ...\n"
    )

    assert _unexplained_any(source) == []


def test_every_other_any_is_reported_under_any_spelling():
    source = (
        "import typing\n"
        "import typing as t\n"
        "from collections.abc import Coroutine\n"
        "from typing import Any, Any as Anything\n"
        "def read(raw: dict[str, Any]) -> Anything: ...\n"
        "def own(**kwargs: Any) -> None: ...\n"
        "def run(coro: Coroutine[Any, Any, Any]) -> typing.Any: ...\n"
        "value: t.Any = None\n"
    )

    assert _unexplained_any(source) == [5, 5, 6, 7, 7, 8]
