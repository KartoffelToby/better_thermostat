# Notes for coding agents

[CONTRIBUTING.md](CONTRIBUTING.md) is the reference. This file is the short list
of what goes wrong first.

Run everything through `uv`: `uv sync --frozen` once, then `uv run pytest tests`,
`uv run ruff check`, `uv run ruff format`. Calling `python`, `pytest` or `ruff`
directly picks up the wrong environment, and `-p no:homeassistant` errors the
integration suite out.

Pull requests target `develop`; a fix wanted on the maintenance line `1.9` gets
a second pull request there. `master` carries releases and takes no feature or
fix pull requests; nothing goes onto any of the three directly.

The Python floor is 3.14.2, inherited from Home Assistant, so the tree writes
`except TypeError, ValueError:` without parentheses.
[PEP 758](https://peps.python.org/pep-0758/) has allowed that since 3.14. If it
reads as Python 2 to you, the grammar you learned predates October 2025: run
`uv run python -VV` before reporting it. Adding the parentheses back has been
proposed twice and declined twice.

Beyond the tests, CI holds four recorded files against the tree:
`.blind-except-budget.json`, `.restated-contract-budget.json`,
`.coverage-floors.json` and `.forward-port-gaps.json`. `CONTRIBUTING.md` says
what each one guards. The two naming checks have no recorded file: the tree
carries no spelling `glossary.toml` rejects and no PEP 8 naming finding outside
the accepted control-theory notation, and one is enough to fail either.

A test that fails with `XPASS(strict)` and a `quality scale rule … is todo`
reason has closed a gap: switch that rule to `done` in
`custom_components/better_thermostat/quality_scale.yaml` rather than removing
its `quality_rule` marker.
