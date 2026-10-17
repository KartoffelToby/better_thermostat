"""Hold PEP 8 naming to the exceptions `pyproject.toml` declares.

Ruff's `N` rules read the case and shape of a name: a class that is not
CapWords (N801), a function or argument or local or class attribute that is not
lowercase (N802, N803, N806, N815), a module whose file name is not a valid
identifier (N999). A name either follows them or is an exception that
`pyproject.toml` declares with its reason. There is no third way: no `noqa`, no
budget, no per-file count.

`pyproject.toml` declares two exceptions:

- the control-theory notation (`A`, `kalman_P`, `T_room`, `error_K`), whose
  shapes `[tool.ruff.lint.pep8-naming] extend-ignore-names` lists and whose
  paths `[tool.better_thermostat.pep8-naming] notation-paths` lists;
- N999 for the device model strings that name the modules under
  `[tool.better_thermostat.pep8-naming] module-name-paths`.

Ruff can apply a name pattern only everywhere and a `per-file-ignores` glob
only to a whole rule, so neither says "these names in these paths". The scan
here does: the notation paths are scanned with the patterns, every other file
without them, so a `kalman_P` in `climate.py` is a finding although ruff itself
lets it through.

The scan runs with ruff's own configuration ignored, with inline `noqa`
directives overridden, and over the file list git reports rather than over a
directory walk. No `per-file-ignores` entry, no `exclude` entry, no `noqa`
comment and no `.gitignore` line hides a name from it; only the two exceptions
above do.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tomllib
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

RULE_FAMILY = "N"
MODULE_NAME_RULE = "N999"

# `--isolated` drops every lint setting the repository carries, which is the
# point: `per-file-ignores`, `extend-per-file-ignores`, `ignore`, `exclude` and
# `extend-exclude` each silence a naming rule, and the scan has to see past
# every one of them. `--ignore-noqa` does the same for the inline directives.
# The files are passed explicitly, so no directory walk and no `.gitignore` line
# decides what is looked at.
RUFF_SCAN = (
    sys.executable,
    "-m",
    "ruff",
    "check",
    "--isolated",
    "--select",
    RULE_FAMILY,
    "--ignore-noqa",
    "--output-format",
    "json",
)


def _normalise(name: str) -> str:
    """Return a ruff filename as a repository-relative POSIX path."""
    path = Path(name)
    if path.is_absolute():
        try:
            path = path.relative_to(REPO_ROOT)
        except ValueError:
            pass
    return path.as_posix()


def _config() -> dict[str, Any]:
    """Return `pyproject.toml` as parsed TOML."""
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _string_list(table: dict[str, Any], key: str, where: str) -> list[str]:
    """Return ``table[key]`` as a list of strings, or exit naming the setting."""
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        sys.exit(f"pyproject.toml: {where} {key} is no list of strings")
    return value


def _target_version(config: dict[str, Any]) -> str | None:
    """Return the Python version ruff parses against, or None when unset.

    An isolated scan has no `target-version`, and a grammar older than the
    sources turns a file into a syntax error, which reports no findings at all.
    The setting selects a grammar and cannot silence a rule, so reading this one
    value back out of `pyproject.toml` does not reopen what `--isolated` closes.
    """
    version = config.get("tool", {}).get("ruff", {}).get("target-version")
    return str(version) if version else None


def _notation_patterns(config: dict[str, Any]) -> list[str]:
    """Return the name patterns ruff reads as the control-theory notation."""
    lint = config.get("tool", {}).get("ruff", {}).get("lint", {})
    return _string_list(
        lint.get("pep8-naming", {}),
        "extend-ignore-names",
        "[tool.ruff.lint.pep8-naming]",
    )


def _exception_paths(config: dict[str, Any], key: str) -> list[str]:
    """Return the path globs `[tool.better_thermostat.pep8-naming]` lists."""
    table = config.get("tool", {}).get("better_thermostat", {}).get("pep8-naming", {})
    return _string_list(table, key, "[tool.better_thermostat.pep8-naming]")


def _in_any(name: str, globs: list[str]) -> bool:
    """Return whether a repository-relative path matches one of *globs*."""
    path = PurePosixPath(name)
    return any(path.full_match(glob) for glob in globs)


def _python_files() -> list[str]:
    """Return the repository's Python files, added to git or not yet.

    A file nobody has run ``git add`` on is part of the next commit all the
    same, so it counts here as it will in CI. The ignore rules decide what
    belongs to the repository.
    """
    listing = subprocess.run(
        (
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
        ),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        sys.exit(f"git could not list the Python files:\n{listing.stderr.strip()}")
    return [name for name in listing.stdout.split("\0") if name]


def _scan(files: list[str], target: str | None, *extra: str) -> list[dict[str, Any]]:
    """Return ruff's naming findings for *files*, exiting when it cannot scan."""
    if not files:
        return []
    command = [*RUFF_SCAN, *extra]
    if target is not None:
        command += ["--target-version", target]
    scan = subprocess.run(
        [*command, *files], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    # Ruff exits 1 when it found something and 2 or above when it could not
    # scan, which is the only case that says nothing about the code.
    if scan.returncode > 1:
        sys.exit(f"ruff could not scan the repository:\n{scan.stderr.strip()}")
    try:
        return json.loads(scan.stdout)
    except json.JSONDecodeError as err:
        sys.exit(f"ruff did not report JSON: {err}\n{scan.stderr.strip()}")


def findings() -> list[str]:
    """Return every naming finding no declared exception covers, one per line.

    The notation paths are scanned with the notation patterns, every other
    file without them, and N999 is accepted under the module-name paths alone.
    """
    config = _config()
    target = _target_version(config)
    notation_paths = _exception_paths(config, "notation-paths")
    module_name_paths = _exception_paths(config, "module-name-paths")
    patterns = json.dumps(_notation_patterns(config))

    files = _python_files()
    zoned = [name for name in files if _in_any(name, notation_paths)]
    plain = [name for name in files if not _in_any(name, notation_paths)]
    scanned = [
        *_scan(plain, target),
        *_scan(
            zoned,
            target,
            "--config",
            f"lint.pep8-naming.extend-ignore-names={patterns}",
        ),
    ]

    # Only the naming rules were selected, so anything else is a file ruff could
    # not parse. Such a file reports no naming findings, which would read as a
    # clean file.
    unparsed = sorted(
        {
            _normalise(finding["filename"])
            for finding in scanned
            if not (finding["code"] or "").startswith(RULE_FAMILY)
        }
    )
    if unparsed:
        sys.exit("ruff could not parse, so it read no names in: " + ", ".join(unparsed))

    reported: list[str] = []
    for finding in scanned:
        name = _normalise(finding["filename"])
        if finding["code"] == MODULE_NAME_RULE and _in_any(name, module_name_paths):
            continue
        row = finding["location"]["row"]
        reported.append(f"{name}:{row}: {finding['code']} {finding['message']}")
    return sorted(reported)


def check() -> int:
    """Report the naming findings no exception covers. Return an exit code."""
    reported = findings()
    if not reported:
        print("no naming finding outside the exceptions pyproject.toml declares")
        return 0
    print("naming findings no exception in pyproject.toml covers:")
    for line in reported:
        print(f"  {line}")
    print(
        "\nSpell the name the way PEP 8 does. Where the spelling is the domain's "
        "own, declare it as an exception in pyproject.toml, with its reason."
    )
    return 1


def main() -> int:
    """Parse arguments and run the check."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("check",))
    parser.parse_args()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
