# Contributing to Better Thermostat

:+1::tada: First off, thanks for taking the time to contribute! :tada::+1:

The following is a set of guidelines for contributing to Better Thermostat. These are mostly guidelines, not rules. Use your best judgment, and feel free to propose changes to this
document in a pull request.

## Development

#### Requirements
- VSCode
- Docker
- Devcontainer Extension

#### Setup
1. Clone the repository
2. Open the repository in VSCode
3. Click on the green button in the bottom left corner and select "Reopen in Container"
4. Wait for the container to build
5. Open Task Runner and run "Run Home Assistant on port 9123"
6. Open the browser and go to http://localhost:9123 -> Inital DEV HA Setup


#### Nice to know

- Debugging is possible with the VSCode Debugger. Just run the HomeAssistant in Debugger and open your browser to http://localhost:9123 (No task run needed)
- Update your local in devcontainer configuration.yaml to the current version of the repository to get the latest changes. -> Run "Sync configuration.yaml (Override local)" in Task Runner
- Test BT in a specific HA version -> Run "Install a specific version of Home Assistant" in Task Runner and the version you want to test in the terminal prompt.
- Test BT with the latest HA version -> Run "upgrade Home Assistant to latest dev" in Task Runner

## How Can I Contribute?

## New Adapter

If you want to add a new adapter, please create a new Python file with the name of the adapter in the adapters folder. The file should contain all functions found in the generic.py. If your adapter needs special handling for one of the base functions, override it, if you can use generic functions, use them like:

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
