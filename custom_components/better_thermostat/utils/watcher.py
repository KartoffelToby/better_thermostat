from __future__ import annotations
from homeassistant.helpers import issue_registry as ir
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
import logging

DOMAIN = "better_thermostat"
_LOGGER = logging.getLogger(__name__)


async def check_entity(self, entity) -> bool:
    if entity is None:
        return False
    entity_states = self.hass.states.get(entity)
    if entity_states is None:
        return False
    state = entity_states.state
    if state in (
        STATE_UNAVAILABLE,
        STATE_UNKNOWN,
        None,
        "missing",
        "unknown",
        "unavail",
        "unavailable",
    ):
        _LOGGER.debug(
            f"better_thermostat {self.name}: {entity} is unavailable. with state {state}"
        )
        return False
    if entity in self.devices_errors:
        self.devices_errors.remove(entity)
        self.async_write_ha_state()
        ir.async_delete_issue(self.hass, DOMAIN, f"missing_entity_{entity}")
    self.hass.async_create_task(get_battery_status(self, entity))
    return True


async def get_battery_status(self, entity):
    if entity in self.devices_states:
        battery_id = self.devices_states[entity].get("battery_id")
        if battery_id is not None:
            new_battery = self.hass.states.get(battery_id)
            if new_battery is not None:
                battery = new_battery.state
                self.devices_states[entity] = {
                    "battery": battery,
                    "battery_id": battery_id,
                }
                self.async_write_ha_state()
                return


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
                is_persistent=False,
                learn_more_url="https://better-thermostat.org/qanda/missing_entity",
                severity=ir.IssueSeverity.WARNING,
                translation_key="missing_entity",
                translation_placeholders={"entity": str(name), "name": str(self.name)},
            )
            return False
    return True
