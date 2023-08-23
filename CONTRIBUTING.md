# Contributing to Better Thermostat

:+1::tada: First off, thanks for taking the time to contribute! :tada::+1:

The following is a set of guidelines for contributing to Better Thermostat. These are mostly guidelines, not rules. Use your best judgment, and feel free to propose changes to this
document in a pull request.

## How Can I Contribute?

## New Adapter

If you want to add a new adapter, please create a new python file with the name of the adapter in the adapters folder. The file should contain all functions find in the generic.py. The if you adapter needs a special handling for one of the base functions, override it, if you can use generic functions, use them like:

```python
async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)
```

## Translations

[INLANG Editor](https://inlang.com/editor/github.com/KartoffelToby/better_thermostat)

### Reporting Bugs

You can create an issue if you have any kind of bug or error but please use the issue template.
## Docstring type

We use numpy type docstrings. Documentation can be found here:

https://sphinxcontrib-napoleon.readthedocs.io/en/latest/example_numpy.html

## Setup

Install the pip install pre-commit  used for pre-commit hooks.