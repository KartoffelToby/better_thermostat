"""Guard: every ``CONF_*`` constant is named after the key it holds.

The key is persisted in the config entry, so it cannot move; the constant
can, and a name that says something else than the key sends a reader to the
wrong setting. The scan covers every module-level ``CONF_*`` string constant
in the package.
"""

import ast
from pathlib import Path

_PACKAGE = (
    Path(__file__).resolve().parents[2] / "custom_components" / "better_thermostat"
)


def _config_constants() -> list[tuple[str, str, str]]:
    """Return (module, constant, key) for each ``CONF_*`` string constant."""
    found = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            else:
                continue
            if (
                isinstance(target, ast.Name)
                and target.id.startswith("CONF_")
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                found.append((path.name, target.id, value.value))
    return found


def test_the_scan_finds_the_config_constants():
    """A scan that finds nothing would pass for the wrong reason."""
    names = {name for _, name, _ in _config_constants()}
    assert {"CONF_THERMOSTAT", "CONF_WINDOW_OFF_DELAY", "CONF_CALIBRATION"} <= names


def test_every_config_constant_is_named_after_its_key():
    """``CONF_X`` holds ``"x"``: the constant follows the stored key."""
    mismatched = [
        f"{module}: {name} = {key!r}"
        for module, name, key in _config_constants()
        if name != f"CONF_{key.upper()}"
    ]
    assert mismatched == []
