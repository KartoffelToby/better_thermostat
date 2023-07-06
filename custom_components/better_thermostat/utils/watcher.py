from __future__ import annotations
from homeassistant.helpers import issue_registry as ir

DOMAIN = "better_thermostat"


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
    if entity in self.devices_errors:
        self.devices_errors.remove(entity)
        self.async_write_ha_state()
        ir.async_delete_issue(self.hass, DOMAIN, f"missing_entity_{entity}")
    await get_battery_status(self, entity)
    return True


async def get_battery_status(self, entity):
    entity_states = self.hass.states.get(entity)
    if entity_states is None:
        return None
    battery = entity_states.attributes.get("battery")
    if battery is not None:
        self.devices_states[entity] = {"battery": battery}
        self.async_write_ha_state()


async def check_all_entities(self) -> bool:
    entities = self.all_entities
    for entity in entities:
        if not await check_entity(self, entity):
            name = entity
            self.devices_errors.append(name)
            self.async_write_ha_state()
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
