"""Guard: production code accesses TRV state via Trv attributes only.

The entries of ``real_trvs`` are typed Trv objects; this scan rejects
dict-style access to them in production code.
"""

from pathlib import Path
import re

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO_ROOT / "custom_components" / "better_thermostat"

_FORBIDDEN = (
    # real_trvs[x]["key"] subscripting of an entry
    re.compile(r"real_trvs\[[^\]]+\]\s*\["),
    # real_trvs[x].get("key") dict-style reads of an entry
    re.compile(r"real_trvs\[[^\]]+\]\.get\("),
    # empty-dict fallbacks that imply dict-shaped entries
    re.compile(r"real_trvs\.get\([^)]*,\s*\{\}\)"),
    # "key" in real_trvs[x] membership tests on an entry
    re.compile(
        r"\bin\s+(?:\w+(?:\.\w+)*\.)?real_trvs\[[^\]]+\]"
        r"(?=\s*(?::|$|\)|,|\.|\band\b|\bor\b))",
        re.MULTILINE,
    ),
    # real_trvs[x].pop()/.keys()/... dict methods on an entry
    re.compile(r"real_trvs\[[^\]]+\]\.(pop|keys|values|items|setdefault|update)\("),
)


def test_no_raw_dict_access_to_real_trvs_entries():
    """No production file subscripts or dict-reads a real_trvs entry."""
    offenders: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        source = path.read_text()
        lines = source.splitlines()
        for pattern in _FORBIDDEN:
            for match in pattern.finditer(source):
                lineno = source.count("\n", 0, match.start()) + 1
                rel = path.relative_to(_REPO_ROOT).as_posix()
                offenders.append(f"{rel}:{lineno}: {lines[lineno - 1].strip()}")
    assert offenders == [], "raw dict access to real_trvs entries:\n" + "\n".join(
        offenders
    )
