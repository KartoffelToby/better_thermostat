"""The entity registry is stood in for by its production types.

A spec-less ``MagicMock`` in place of a registry entry answers every field
it was not told about with a truthy mock. ``disabled_by`` then reads as
disabled on every entry and ``translation_key`` as a key that matches
nothing, so a finder that skips disabled entries skips every candidate in
the suite, and one that adopts them is never shown a disabled entry at all.
A spec-less registry answers ``entities.values()`` with an empty iterator
and ``async_get`` with a truthy mock, whatever the test meant it to hold.

``tests.factories.make_registry_entry`` builds the real entry and
``make_entity_registry`` a registry specced on the real class around HA's
own entry container. The rules here keep the hand-rolled shapes out:

* An entry stand-in is a spec-less mock or namespace whose fields are all
  ``RegistryEntry`` fields and include at least one field only an entity
  registry entry carries. The field set is derived from the class, so a
  field HA adds is covered the day it lands.
* A registry stand-in is a spec-less mock that is either handed to a patch
  of an entity registry getter in the same scope, or used through a member
  only the entity registry has (derived against the device registry, which
  shares ``async_get`` and ``async_get_or_create`` with it).

A registry built in one helper and patched in from another is seen by
neither half of the second rule; its entries are still seen by the first.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import attr
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

TESTS_ROOT = Path(__file__).resolve().parent.parent

STAND_IN_CONSTRUCTORS = frozenset(
    {
        "MagicMock",
        "Mock",
        "NonCallableMagicMock",
        "NonCallableMock",
        "AsyncMock",
        "SimpleNamespace",
    }
)

# Every attribute a real entry answers: its fields and the derived domain.
ENTRY_FIELDS = frozenset(
    {field.name for field in attr.fields(er.RegistryEntry)} | {"domain", "disabled"}
)

# Fields that make a stand-in an entity registry entry rather than a state,
# a config entry or a Better Thermostat instance. ``entity_id``,
# ``unique_id`` and ``name`` are shared with those and prove nothing alone.
ENTRY_MARKERS = frozenset(
    {
        "device_id",
        "config_entry_id",
        "translation_key",
        "original_name",
        "original_device_class",
        "disabled_by",
        "platform",
    }
)

# Members only the entity registry has. ``entities`` is an instance
# attribute, so the class namespace does not list it.
REGISTRY_MARKERS = frozenset(
    (
        {name for name in dir(er.EntityRegistry) if not name.startswith("_")}
        | {"entities"}
    )
    - set(dir(dr.DeviceRegistry))
)

# Dotted patch targets that hand out the entity registry.
REGISTRY_GETTERS = (
    "er.async_get",
    "entity_registry.async_get",
    "async_get_entity_registry",
)


def _call_name(node: ast.Call) -> str | None:
    """The bare name a call is made through, ``MagicMock`` for ``mock.MagicMock``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_spec_less_stand_in(node: ast.AST) -> bool:
    """Whether ``node`` builds a mock or namespace that follows no spec."""
    if not isinstance(node, ast.Call) or _call_name(node) not in STAND_IN_CONSTRUCTORS:
        return False
    if _call_name(node) == "SimpleNamespace":
        return True
    keywords = {kw.arg for kw in node.keywords}
    # ``MagicMock(SomeClass)`` passes the spec positionally.
    return not node.args and not ({"spec", "spec_set"} & keywords)


def _scopes(tree: ast.Module) -> dict[int, int]:
    """Map every node to the ``id`` of the function that encloses it (0: module)."""
    owner: dict[int, int] = {}

    def descend(node: ast.AST, current: int) -> None:
        for child in ast.iter_child_nodes(node):
            owner[id(child)] = current
            inner = (
                id(child)
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                )
                else current
            )
            descend(child, inner)

    owner[id(tree)] = 0
    descend(tree, 0)
    return owner


def _root_name(node: ast.expr) -> tuple[str, list[str]] | None:
    """``registry.entities.values`` -> (``registry``, [``entities``, ``values``])."""
    chain: list[str] = []
    while isinstance(node, ast.Attribute):
        chain.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return node.id, list(reversed(chain))
    return None


def _names_a_registry_getter(node: ast.Call) -> bool:
    """Whether a ``patch``/``patch.object``/``setattr`` call replaces a registry getter."""
    texts = [
        arg.value
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]
    for arg in node.args:
        if isinstance(arg, ast.JoinedStr):
            texts.append(ast.unparse(arg))
    if any(text.rstrip("'\"").endswith(REGISTRY_GETTERS) for text in texts):
        return True
    # patch.object(module.er, "async_get") / monkeypatch.setattr(module.er, "async_get", ...)
    if len(node.args) >= 2:
        owner, member = node.args[0], node.args[1]
        owner_text = ast.unparse(owner)
        if (
            isinstance(member, ast.Constant)
            and member.value == "async_get"
            and (owner_text == "er" or owner_text.endswith((".er", "entity_registry")))
        ):
            return True
    return False


def _handed_out(value: ast.expr) -> str | None:
    """The name a registry getter replacement answers with, if it is one."""
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Lambda) and isinstance(value.body, ast.Name):
        return value.body.id
    return None


def offenders_in(source: str, label: str) -> list[str]:
    """Every spec-less registry entry or registry stand-in in ``source``."""
    tree = ast.parse(source)
    scopes = _scopes(tree)

    bound: dict[tuple[int, str], list[ast.Call]] = {}
    fields: dict[tuple[int, str], set[str]] = {}
    members: dict[tuple[int, str], set[str]] = {}
    patched_as: dict[tuple[int, str], bool] = {}
    handed: set[tuple[int, str]] = set()

    for node in ast.walk(tree):
        scope = scopes.get(id(node), 0)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and isinstance(node.value, ast.Call)
                    and _is_spec_less_stand_in(node.value)
                ):
                    bound.setdefault((scope, target.id), []).append(node.value)
                if isinstance(target, ast.Attribute) and isinstance(
                    target.value, ast.Name
                ):
                    key = (scope, target.value.id)
                    fields.setdefault(key, set()).add(target.attr)
                    if target.attr == "return_value" and patched_as.get(key):
                        name = _handed_out(node.value)
                        if name:
                            handed.add((scope, name))
        if isinstance(node, ast.Attribute):
            root = _root_name(node)
            if root is not None and root[1]:
                members.setdefault((scope, root[0]), set()).add(root[1][0])
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if (
                    isinstance(item.context_expr, ast.Call)
                    and _names_a_registry_getter(item.context_expr)
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    patched_as[(scope, item.optional_vars.id)] = True
        if isinstance(node, ast.Call) and _names_a_registry_getter(node):
            for kw in node.keywords:
                if kw.arg in ("return_value", "new") and (
                    name := _handed_out(kw.value)
                ):
                    handed.add((scope, name))
            for arg in node.args[2:]:
                if name := _handed_out(arg):
                    handed.add((scope, name))

    found: list[str] = []

    def report(node: ast.Call, what: str) -> None:
        found.append(f"{label}:{node.lineno} {what}")

    # Entries built inline: MagicMock(device_id=...), SimpleNamespace(device_id=...).
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_spec_less_stand_in(node):
            keywords = {kw.arg for kw in node.keywords if kw.arg}
            if keywords & ENTRY_MARKERS and keywords <= ENTRY_FIELDS:
                report(node, f"builds a registry entry from {sorted(keywords)}")

    for key, calls in bound.items():
        assigned = fields.get(key, set())
        entry_like = bool(assigned & ENTRY_MARKERS) and assigned <= ENTRY_FIELDS
        registry_like = key in handed or bool(
            members.get(key, set()) & REGISTRY_MARKERS
        )
        for call in calls:
            if entry_like:
                report(
                    call, f"'{key[1]}' is a registry entry carrying {sorted(assigned)}"
                )
            elif registry_like:
                report(call, f"'{key[1]}' stands in for the entity registry")
    return sorted(
        set(found),
        key=lambda line: (line.split(":")[0], int(line.split(":")[1].split()[0])),
    )


def _test_modules() -> Iterator[Path]:
    """Every Python module under tests/, this one included."""
    yield from sorted(TESTS_ROOT.rglob("*.py"))


def test_the_derived_field_sets_are_populated():
    """The derivations find the fields and members the rules rest on.

    An import that resolved to the wrong class would empty them and pass the
    suite-wide rule without checking anything.
    """
    assert {"device_id", "disabled_by", "translation_key"} <= ENTRY_FIELDS
    assert ENTRY_MARKERS <= ENTRY_FIELDS
    assert {
        "entities",
        "async_get_entity_id",
        "async_update_entity",
    } <= REGISTRY_MARKERS
    assert "async_get" not in REGISTRY_MARKERS


@pytest.mark.parametrize(
    "source",
    [
        # An entry built field by field.
        "def f():\n    e = MagicMock()\n    e.device_id = 'd'\n    e.entity_id = 'x.y'\n",
        # An entry built from keywords, bound or inline.
        "def f():\n    e = MagicMock(device_id='d', entity_id='x.y')\n",
        "def f(reg):\n    reg.async_get.return_value = SimpleNamespace(device_id='d')\n",
        "def f(reg):\n    reg.async_get.return_value = MagicMock(device_id=None)\n",
        # A registry used through a member only the entity registry has.
        "def f():\n    reg = MagicMock()\n    reg.entities.values.return_value = []\n",
        "def f():\n    reg = MagicMock()\n    reg.async_get_entity_id = MagicMock()\n",
        # A registry handed to a patched getter, in each spelling.
        "def f():\n    reg = MagicMock()\n    with patch('m.er.async_get', return_value=reg):\n        pass\n",
        "def f():\n    reg = MagicMock()\n    with patch('m.er.async_get') as g:\n        g.return_value = reg\n",
        "def f():\n    reg = MagicMock()\n    patch.object(m.er, 'async_get', lambda hass: reg)\n",
        "def f(mp):\n    reg = MagicMock()\n    mp.setattr(quirk.er, 'async_get', lambda hass: reg, raising=True)\n",
        "def f():\n    reg = MagicMock()\n    patch('m.sensor.async_get_entity_registry', return_value=reg)\n",
    ],
    ids=[
        "entry-by-fields",
        "entry-by-keywords",
        "inline-namespace-entry",
        "inline-mock-entry",
        "registry-entities",
        "registry-only-member",
        "patch-return-value",
        "patch-as-return-value",
        "patch-object-lambda",
        "monkeypatch-setattr",
        "sensor-getter",
    ],
)
def test_the_rule_reports_each_spec_less_shape(source):
    """Each shape a registry stand-in has taken in this suite is reported."""
    assert offenders_in(source, "sample"), source


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    e = make_registry_entry('x.y', device_id='d')\n",
        "def f():\n    reg = make_entity_registry()\n    patch('m.er.async_get', return_value=reg)\n",
        "def f():\n    reg = MagicMock(spec=er.EntityRegistry)\n    reg.entities = x\n",
        "def f():\n    e = MagicMock(spec=er.RegistryEntry)\n    e.device_id = 'd'\n",
        # A device registry shares async_get with the entity registry.
        "def f():\n    dev_reg = MagicMock()\n    dev_reg.async_get.return_value = None\n",
        # A Better Thermostat stand-in carries a device id among other things.
        "def f():\n    bt = SimpleNamespace(real_trvs={}, device_id='d')\n",
        # A state or config entry carries an entity id, which proves nothing.
        "def f():\n    s = MagicMock()\n    s.entity_id = 'x.y'\n    s.state = 'on'\n",
    ],
    ids=[
        "factory-entry",
        "factory-registry",
        "specced-registry",
        "specced-entry",
        "device-registry",
        "bt-namespace",
        "state",
    ],
)
def test_the_rule_passes_the_production_types(source):
    """The factories, a specced mock and the look-alikes are not reported."""
    assert offenders_in(source, "sample") == [], source


def test_no_spec_less_registry_stand_in_in_the_suite():
    """Every registry entry and entity registry in tests/ has the production type."""
    offenders = [
        line
        for path in _test_modules()
        for line in offenders_in(
            path.read_text(), str(path.relative_to(TESTS_ROOT.parent))
        )
    ]
    assert not offenders, (
        "spec-less entity registry stand-ins; build them with "
        "tests.factories.make_registry_entry / make_entity_registry:\n"
        + "\n".join(offenders)
    )
