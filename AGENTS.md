# Notes for coding agents

[CONTRIBUTING.md](CONTRIBUTING.md) is the reference. This file is the short list
of what goes wrong first.

Run everything through `uv`: `uv sync --frozen` once, then `uv run pytest tests`,
`uv run ruff check`, `uv run ruff format`. Calling `python`, `pytest` or `ruff`
directly picks up the wrong environment, and `-p no:homeassistant` errors the
integration suite out.

Pull requests target `develop`. `1.9` is the maintenance line and `master`
carries releases; nothing goes onto any of the three directly.

The Python floor is 3.14.2, inherited from Home Assistant, so the tree writes
`except TypeError, ValueError:` without parentheses.
[PEP 758](https://peps.python.org/pep-0758/) has allowed that since 3.14. If it
reads as Python 2 to you, the grammar you learned predates October 2025: run
`uv run python -VV` before reporting it. Adding the parentheses back has been
proposed twice and declined twice.

Beyond the tests, CI holds five recorded budgets against the tree.
`CONTRIBUTING.md` names them and says what re-recording one costs.
