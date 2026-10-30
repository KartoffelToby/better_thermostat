"""Guard: a per-TRV boolean option is read through ``advanced_flag``.

An entry written by an older version can hold these options as strings such
as ``"false"``, which a raw ``.get`` hands on as a truthy value. The scan
rejects every ``.get`` of one of them on an ``advanced`` mapping in
production code, so a stored value means the same thing to every reader.
"""

import ast
from pathlib import Path

from custom_components.better_thermostat.utils import const

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO_ROOT / "custom_components" / "better_thermostat"

_FLAG_CONSTANTS = (
    "CONF_PROTECT_OVERHEATING",
    "CONF_NO_OFF_SYSTEM_MODE",
    "CONF_HEAT_AUTO_SWAPPED",
    "CONF_VALVE_MAINTENANCE",
    "CONF_CHILD_LOCK",
    "CONF_HOMEMATICIP",
)
_FLAG_VALUES = frozenset(getattr(const, name) for name in _FLAG_CONSTANTS)

# Reads that hand the raw value on to ``as_bool`` themselves, keyed by file
# and the reason the raw value is needed.
_ALLOWED = {
    # ``None`` means the option was never set, which skips the child-lock
    # sync; a set value goes through ``as_bool``.
    "model_fixes/default.py": 1,
    # The options flow normalises what it is about to save.
    "config_flow.py": None,
    # The setup parser normalises the flags with ``as_bool``.
    "utils/entry_schema.py": None,
    "utils/advanced_flags.py": None,
}


def _is_flag_key(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _FLAG_CONSTANTS
    if isinstance(node, ast.Constant):
        return node.value in _FLAG_VALUES
    return False


def _raw_flag_reads(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and _is_flag_key(node.args[0])
        ):
            continue
        if "advanced" in ast.unparse(node.func.value):
            lines.append(node.lineno)
    return lines


def test_no_raw_read_of_an_advanced_flag():
    """Only the named sites read an advanced flag without ``advanced_flag``."""
    offenders: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        rel = path.relative_to(_PACKAGE).as_posix()
        allowed = _ALLOWED.get(rel, 0)
        if allowed is None:
            continue
        reads = _raw_flag_reads(ast.parse(path.read_text()))
        if len(reads) != allowed:
            offenders.append(f"{rel}: raw reads on lines {reads}, expected {allowed}")
    assert offenders == [], "advanced flags read raw:\n" + "\n".join(offenders)
