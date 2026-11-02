"""The notifications of the bundled blueprints name the selected thermostat."""

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.template import Template
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.utils.const import DOMAIN
from tests.unit.test_blueprints import (
    BLUEPRINTS_WITH_DEVICE_TRIGGERS,
    _is_template,
    _load,
    _resolve_inputs,
    _substitute,
)


def _templates_naming_the_device(node) -> list[str]:
    """Collect every template string that looks up a device name."""
    if isinstance(node, dict):
        return [t for v in node.values() for t in _templates_naming_the_device(v)]
    if isinstance(node, list):
        return [t for v in node for t in _templates_naming_the_device(v)]
    if _is_template(node) and "device_attr(" in node:
        return [node]
    return []


@pytest.mark.parametrize("path", BLUEPRINTS_WITH_DEVICE_TRIGGERS, ids=lambda p: p.name)
async def test_notifications_name_the_selected_thermostat(hass, path):
    """A notification names the thermostat the blueprint was set up for.

    The trigger variables of a Better Thermostat device trigger are those of
    the state or numeric_state trigger it delegates to: they carry the
    entity, not the device. The device name therefore has to come from the
    blueprint's own device input, which is what this renders against.
    """
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "bt_living_room")},
        name="Living room",
    )

    blueprint = _load(path)
    inputs = _resolve_inputs(blueprint)
    inputs["thermostat_device"] = device.id
    variables = _substitute(blueprint.get("variables", {}), inputs)
    assert isinstance(variables, dict)
    variables["trigger"] = {
        "platform": "device",
        "entity_id": "climate.living_room",
        "from_state": None,
        "to_state": None,
    }

    templates = _templates_naming_the_device(
        _substitute(blueprint.get("action", blueprint.get("actions")), inputs)
    )
    assert templates, f"{path.name}: no message names the device"
    for text in templates:
        rendered = Template(text, hass).async_render(variables, parse_result=False)
        assert "Living room" in rendered, f"{path.name}: {rendered!r}"
