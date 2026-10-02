"""Guard: a thermostat stand-in cannot invent the state it was never given.

A test that builds its BetterThermostat as a bare ``MagicMock`` gets a
truthy mock for every attribute it forgot. Production then reads a
maintenance run, an open window or a kernel region the test never set up,
and the test passes on a branch it did not mean to exercise.
``tests.factories.ThermostatStandIn`` raises for such a read, and
``make_bt`` builds on it. No test file builds a bare one.

A bare mock is a thermostat stand-in when it carries one of the names the
suite gives a thermostat, or when the scope that builds it sets at least two
attributes of thermostat state on it. A mock built in one helper and filled
in another is seen by neither rule.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from homeassistant.components.climate import ClimateEntity

from tests.factories import THERMOSTAT_STATE

_TESTS = Path(__file__).resolve().parents[1]

# The names a test gives its BetterThermostat stand-in.
_STAND_IN_NAMES = frozenset({"bt", "bt_climate", "mock_bt", "mock_self"})

_BARE_MOCKS = frozenset(
    {"MagicMock", "Mock", "NonCallableMagicMock", "NonCallableMock"}
)

# State attributes a scope has to set on a mock before it counts as a
# thermostat. A name every climate entity has, such as ``min_temp``, also
# fits a TRV stand-in and proves nothing.
_STATE_EVIDENCE = 2
_THERMOSTAT_ONLY = THERMOSTAT_STATE - set(dir(ClimateEntity))


def _dotted(node: ast.expr) -> str | None:
    """Return ``bt`` or ``self.bt`` for a name or attribute chain, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (owner := _dotted(node.value)):
        return f"{owner}.{node.attr}"
    return None


def _is_bare_mock(node: ast.expr | None) -> bool:
    """Return whether ``node`` calls a mock class directly."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return called in _BARE_MOCKS


def _bindings(node: ast.stmt) -> list[tuple[ast.expr, ast.expr | None]]:
    """Return ``(target, value)`` pairs an assignment statement binds."""
    if isinstance(node, ast.Assign):
        pairs = []
        for target in node.targets:
            if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                pairs.extend(zip(target.elts, node.value.elts, strict=False))
            else:
                pairs.append((target, node.value))
        return pairs
    if isinstance(node, ast.AnnAssign | ast.AugAssign):
        return [(node.target, node.value)]
    return []


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Yield the nodes of ``scope`` without the bodies of nested functions."""
    pending = [scope]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
        )


def _bare_stand_ins_in(scope: ast.AST) -> set[int]:
    """Return the lines in ``scope`` that build a bare thermostat stand-in."""
    built: dict[str, set[int]] = {}
    state_set: dict[str, set[str]] = {}
    for node in _own_nodes(scope):
        if isinstance(node, ast.NamedExpr) and _is_bare_mock(node.value):
            built.setdefault(node.target.id, set()).add(node.lineno)
        if not isinstance(node, ast.stmt):
            continue
        for target, value in _bindings(node):
            if _is_bare_mock(value) and (name := _dotted(target)):
                built.setdefault(name, set()).add(node.lineno)
            if (
                isinstance(target, ast.Attribute)
                and target.attr in _THERMOSTAT_ONLY
                and (owner := _dotted(target.value))
            ):
                state_set.setdefault(owner, set()).add(target.attr)
    return {
        line
        for name, lines in built.items()
        if name.rsplit(".", 1)[-1] in _STAND_IN_NAMES
        or len(state_set.get(name, ())) >= _STATE_EVIDENCE
        for line in lines
    }


def _bare_stand_ins(path: Path) -> list[int]:
    """Return the lines in one file that build a bare thermostat stand-in."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scopes = [
        tree,
        *(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ),
    ]
    return sorted(set().union(*(_bare_stand_ins_in(scope) for scope in scopes)))


def test_a_bare_stand_in_is_found_in_each_spelling(tmp_path):
    """A stand-in is found by its name or by the state set on it."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "bt = MagicMock()\n"
        "mock_bt = mock.Mock()\n"
        "self.bt = MagicMock()\n"
        "def fixture():\n"
        "    mock: MagicMock = MagicMock()\n"
        "    mock.kernel_state = None\n"
        "    mock.clock = None\n"
        "    host, trv = MagicMock(), MagicMock()\n"
        "    host.real_trvs = {}\n"
        "    host.in_maintenance = False\n"
        "    trv.min_temp = 5.0\n"
        "    trv.max_temp = 30.0\n"
        "    strict = ThermostatStandIn()\n"
        "    other = MagicMock()\n"
        "    other.kernel_state = None\n"
        "    mock = MagicMock()\n"
        "bt_climate: MagicMock = MagicMock()\n"
        "def builds():\n"
        "    shared = MagicMock()\n"
        "    def fills():\n"
        "        shared = object()\n"
        "        shared.kernel_state = None\n"
        "        shared.clock = None\n"
        "def fills_too():\n"
        "    shared.kernel_state = None\n"
        "    shared.clock = None\n"
        "shared = MagicMock()\n",
        encoding="utf-8",
    )

    assert _bare_stand_ins(probe) == [1, 2, 3, 5, 8, 16, 17]


def test_no_file_builds_a_bare_stand_in():
    """Every thermostat stand-in in the suite is the strict one."""
    bare = [
        f"{path.relative_to(_TESTS.parent).as_posix()}:{line}"
        for path in sorted(_TESTS.rglob("*.py"))
        for line in _bare_stand_ins(path)
    ]

    assert not bare, (
        "build these on tests.factories.ThermostatStandIn or make_bt:\n"
        + "\n".join(bare)
    )
