"""Guard: a thermostat stand-in only sets attributes production has.

A test builds its thermostat as a plain ``MagicMock`` named ``bt`` or
``mock_bt``, and such a mock accepts any attribute. When production renames
or drops an attribute, the fixtures keep setting the old name: the test stays
green while the code under test reads a default the fixture never meant. A
later assertion on that name checks nothing production writes.
"""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO_ROOT / "custom_components" / "better_thermostat"
_TESTS = _REPO_ROOT / "tests"

# The names a test gives a BetterThermostat stand-in.
_STAND_IN_NAMES = frozenset({"bt", "mock_bt"})

# Attributes a test puts on its stand-in for its own bookkeeping. They carry
# no production meaning, and each is read back only by the test that sets it.
_TEST_ONLY = {
    "_call_for_heat_after": "tests/unit/test_outdoor_sensor_listener.py",
    "_ambient_mock": "tests/unit/test_outdoor_sensor_listener.py",
}


def _production_names() -> set[str]:
    """Return every name production spells: attributes, names, strings, defs."""
    names: set[str] = set()
    for path in _PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                names.add(node.name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # setattr/getattr with a literal name
                names.add(node.value)
    return names


def _stand_in_assignments(path: Path) -> list[tuple[int, str]]:
    """Return ``(line, attribute)`` for each ``bt.<attribute> = ...`` in a file."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in _STAND_IN_NAMES
            ):
                found.append((target.lineno, target.attr))
    return found


def test_a_stand_in_sets_only_attributes_production_names():
    """Every attribute a fixture sets on its thermostat is one production uses."""
    production = _production_names()
    ghosts = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{line}: {attribute}"
        for path in sorted(_TESTS.rglob("*.py"))
        for line, attribute in _stand_in_assignments(path)
        if attribute not in production
        and _TEST_ONLY.get(attribute) != path.relative_to(_REPO_ROOT).as_posix()
    ]

    assert not ghosts, "stand-in attributes production does not have:\n" + "\n".join(
        ghosts
    )


def test_the_test_only_attributes_are_still_test_only():
    """An allowed test attribute that production adopts leaves the allowance."""
    production = _production_names()

    adopted = sorted(name for name in _TEST_ONLY if name in production)

    assert not adopted, (
        f"now production attributes, drop them from _TEST_ONLY: {adopted}"
    )
