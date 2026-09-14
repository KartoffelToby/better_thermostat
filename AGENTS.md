# Notes for coding agents

This file is for automated contributors. [CONTRIBUTING.md](CONTRIBUTING.md) is
the longer version and the one humans want; nothing here contradicts it.

## The Python floor is 3.14, and the code reads that way

Home Assistant 2026.9.0 declares `Requires-Python: >=3.14.2` and this
integration requires that core release, so 3.14.2 is the floor. `pyproject.toml`
says so in `requires-python`, in ruff's `target-version`, and in the pyrefly and
pyright settings.

The consequence you will meet first is
[PEP 758](https://peps.python.org/pep-0758/). An `except` clause that binds no
name lists its types without parentheses, and the tree does this in about ninety
places:

```python
except TypeError, ValueError:      # 3.14: catches both
except (TypeError, ValueError):    # the same thing, older grammar
```

If your training data predates Python 3.14 (October 2025), the first form looks
like Python 2, where the second name would have been the binding. It is not, in
any Python 3: 3.13 and older raise `SyntaxError: multiple exception types must
be parenthesized` rather than mis-binding anything.

Before reporting a syntax error here:

1. Run `uv run python -VV`. Below 3.14 the parser is the problem, not the file.
2. Run `uv run python -m compileall custom_components/better_thermostat`. It
   exits 0 on a supported interpreter.
3. Check whether the line is one of the ninety. Putting the parentheses back
   changes no behaviour, conflicts with the other release line, and has been
   proposed and declined twice.

## Commands

```bash
uv sync --frozen                                  # environment from the lockfile
uv run pytest tests                               # the suite
uv run ruff check && uv run ruff format --check   # lint and format
uv run python scripts/check_naming.py check       # vocabulary gate
```

Everything goes through `uv`. Do not call `python`, `pytest` or `ruff` directly,
and do not pass `-p no:homeassistant` to pytest: the integration suite needs
that plugin and errors out without it.

## Branches

Pull requests target `develop`. `1.9` is the maintenance line and `master`
carries releases. A change wanted on both lines is written twice, once per line,
because the lines have diverged past cherry-picking. Nothing goes onto a shared
branch directly.

## What CI holds besides the tests

Five recorded budgets, each a file CI compares against the tree:
`.coverage-floors.json`, `.naming-budget.json`, `.pep8-naming-budget.json`,
`.blind-except-budget.json` and `.restated-contract-budget.json`. Each records
where the tree stands today, and a change that falls below one fails the build.
Re-recording a budget is a change of its own, with its own reason in the commit
message; it is not the way to get a red build green.

## Naming and docstrings

`glossary.toml` holds one term per concept and outranks any name you invent;
`uv run python scripts/check_naming.py list <path>` reports a file against it.
Docstrings are numpy style. Comments describe the code as it stands, without
history, pull request numbers, or notes on what changed.
