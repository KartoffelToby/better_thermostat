"""Guard: a thermostat stand-in only sets attributes production has.

A test builds its thermostat as a plain ``MagicMock`` named ``bt`` or
``mock_bt``, and such a mock accepts any attribute. When production renames
or drops an attribute, the fixtures keep setting the old name: the test stays
green while the code under test reads a default the fixture never meant. A
later assertion on that name checks nothing production writes.
"""

import ast
from pathlib import Path

from custom_components.better_thermostat.climate import BetterThermostat

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


def _inherited_names() -> set[str]:
    """Return the attribute names the thermostat inherits from Home Assistant.

    The class bodies and annotations of every base class outside this
    integration: ``ClimateEntity``, ``RestoreEntity``, ``Entity`` and what
    they build on.
    """
    names: set[str] = set()
    for base in BetterThermostat.__mro__:
        if base.__module__.startswith("custom_components."):
            continue
        names |= set(vars(base))
        names |= set(getattr(base, "__annotations__", {}))
    return names


def _ghosts(paths: list[Path], known: set[str], root: Path = _REPO_ROOT) -> list[str]:
    """Return ``file:line: attribute`` for each stand-in attribute not in ``known``."""
    return [
        f"{path.relative_to(root).as_posix()}:{line}: {attribute}"
        for path in paths
        for line, attribute in _stand_in_assignments(path)
        if attribute not in known
        and _TEST_ONLY.get(attribute) != path.relative_to(root).as_posix()
    ]


def test_an_inherited_attribute_passes_and_a_ghost_does_not(tmp_path):
    """A name the thermostat inherits from Home Assistant is a real attribute.

    The stand-in imitates the whole entity, so ``bt._attr_available`` or
    ``bt.registry_entry`` set on it is what production reads through its
    base classes; a name neither production nor those classes carry is not.
    """
    fixture = tmp_path / "tests" / "unit" / "test_probe.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "bt._attr_available = True\n"
        "bt.registry_entry = None\n"
        "mock_bt._attr_hvac_action = None\n"
        "bt._no_such_attribute_anywhere = 1\n",
        encoding="utf-8",
    )

    ghosts = _ghosts([fixture], _production_names() | _inherited_names(), tmp_path)

    assert ghosts == ["tests/unit/test_probe.py:4: _no_such_attribute_anywhere"]


def test_a_stand_in_sets_only_attributes_production_names():
    """Every attribute a fixture sets on its thermostat is one production uses."""
    ghosts = _ghosts(
        sorted(_TESTS.rglob("*.py")), _production_names() | _inherited_names()
    )

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
