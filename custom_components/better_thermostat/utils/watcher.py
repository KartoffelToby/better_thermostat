from __future__ import annotations
from homeassistant.helpers import issue_registry as ir
from .. import DOMAIN


async def check_entity(self, entity) -> bool:
    if entity is None:
        return False
    entity_states = self.hass.states.get(entity)
    state = (
        "missing"
        if not entity_states
        else str(entity_states.state).replace("unavailable", "unavail")
    )
    if entity_states is None or state in ["missing", "unknown", "unavail"]:
        return False
    return True


async def check_all_entities(self) -> bool:
    entities = self.all_entities
    for entity in entities:
        if not await check_entity(self, entity):
            name = entity
            ir.async_create_issue(
                hass=self.hass,
                domain=DOMAIN,
                issue_id=f"missing_entity_{name}",
                is_fixable=True,
                is_persistent=True,
                learn_more_url="https://better-thermostat.org/qanda/missing_entity",
                severity=ir.IssueSeverity.WARNING,
                translation_key="missing_entity",
                translation_placeholders={"entity": str(name), "name": str(self.name)},
            )
            return False
    return True
