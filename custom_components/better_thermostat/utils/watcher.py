"""Watcher helpers to verify the presence and state of configured entities.

This module contains utility functions to verify entities, check batteries,
and raise Home Assistant issues if an entity is missing or unavailable.

Supports degraded mode operation where optional sensors (window, humidity,
outdoor, weather) can be unavailable without blocking thermostat operation.
"""

from __future__ import annotations

import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import issue_registry as ir

DOMAIN = "better_thermostat"
_LOGGER = logging.getLogger(__name__)

# States considered unavailable
UNAVAILABLE_STATES = (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    None,
    "missing",
    "unknown",
    "unavail",
    "unavailable",
)


def is_entity_available(hass, entity) -> bool:
    """Check if an entity is available without side effects.

    Parameters
    ----------
    hass :
        Home Assistant instance
    entity : str
        Entity ID to check

    Returns
    -------
    bool
        True if entity exists and is in a valid state
    """
    if entity is None:
        return False
    entity_states = hass.states.get(entity)
    if entity_states is None:
        return False
    return entity_states.state not in UNAVAILABLE_STATES


async def check_entity(self, entity) -> bool:
    """Check if a specific entity is present and available.

    Returns True if the entity is available and known to Home Assistant,
    otherwise raises an issue and returns False.
    """
    if entity is None:
        return False
    entity_states = self.hass.states.get(entity)
    if entity_states is None:
        return False
    state = entity_states.state
    if state in UNAVAILABLE_STATES:
        _LOGGER.debug(
            "better_thermostat %s: %s is unavailable. with state %s",
            self.device_name,
            entity,
            state,
        )
        return False
    if entity in self.devices_errors:
        self.devices_errors.remove(entity)
        self.async_write_ha_state()
        ir.async_delete_issue(self.hass, DOMAIN, f"missing_entity_{entity}")
    self.hass.async_create_task(get_battery_status(self, entity))
    return True


async def get_battery_status(self, entity):
    """Read a battery entity for a device and update internal state.

    Uses the provided mapping stored in `self.devices_states`.
    """
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
    """Verify all configured entities and report missing ones as issues.

    Returns True if all entities are available.
    """
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
                translation_placeholders={
                    "entity": str(name),
                    "name": str(self.device_name),
                },
            )
            return False
    return True


def get_optional_sensors(self) -> list:
    """Return list of optional sensor entity IDs.

    Optional sensors are those that can be unavailable without
    blocking thermostat operation (degraded mode).

    Returns
    -------
    list
        List of optional sensor entity IDs
    """
    optional = []
    if getattr(self, "window_id", None):
        optional.append(self.window_id)
    if getattr(self, "humidity_sensor_entity_id", None):
        optional.append(self.humidity_sensor_entity_id)
    if getattr(self, "outdoor_sensor", None):
        optional.append(self.outdoor_sensor)
    if getattr(self, "weather_entity", None):
        optional.append(self.weather_entity)
    return optional


def get_critical_entities(self) -> list:
    """Return list of critical entity IDs.

    Critical entities are TRVs - without them the thermostat cannot function.
    The room temperature sensor is semi-critical (can fall back to TRV temp).

    Returns
    -------
    list
        List of critical entity IDs (TRVs)
    """
    critical = []
    if hasattr(self, "real_trvs") and self.real_trvs:
        critical.extend(list(self.real_trvs.keys()))
    return critical


async def check_critical_entities(self) -> bool:
    """Check only critical entities (TRVs).

    Returns True if all TRVs are available. Does not block on optional sensors.

    Returns
    -------
    bool
        True if all critical entities are available
    """
    critical = get_critical_entities(self)
    for entity in critical:
        if not is_entity_available(self.hass, entity):
            _LOGGER.warning(
                "better_thermostat %s: Critical entity %s is unavailable",
                self.device_name,
                entity,
            )
            if entity not in self.devices_errors:
                self.devices_errors.append(entity)
                self.async_write_ha_state()
                ir.async_create_issue(
                    hass=self.hass,
                    domain=DOMAIN,
                    issue_id=f"missing_entity_{entity}",
                    is_fixable=True,
                    is_persistent=False,
                    learn_more_url="https://better-thermostat.org/qanda/missing_entity",
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="missing_entity",
                    translation_placeholders={
                        "entity": str(entity),
                        "name": str(self.device_name),
                    },
                )
            return False
        else:
            # Clear error if entity is now available
            if entity in self.devices_errors:
                self.devices_errors.remove(entity)
                ir.async_delete_issue(self.hass, DOMAIN, f"missing_entity_{entity}")
            # Update battery status for available entities
            self.hass.async_create_task(get_battery_status(self, entity))
    return True


async def check_and_update_degraded_mode(self) -> bool:
    """Check optional sensors and update degraded mode status.

    Sets self.degraded_mode to True if any optional sensor is unavailable.
    Updates self.unavailable_sensors with list of unavailable optional sensors.

    Returns
    -------
    bool
        True if operating in degraded mode (some optional sensors unavailable)
    """
    optional = get_optional_sensors(self)
    unavailable = []

    for entity in optional:
        if not is_entity_available(self.hass, entity):
            unavailable.append(entity)
            _LOGGER.debug(
                "better_thermostat %s: Optional sensor %s is unavailable (degraded mode)",
                self.device_name,
                entity,
            )
        else:
            # Update battery status for available optional sensors
            self.hass.async_create_task(get_battery_status(self, entity))

    # Check room temperature sensor - special case with TRV fallback
    sensor_available = is_entity_available(self.hass, self.sensor_entity_id)
    if not sensor_available:
        unavailable.append(self.sensor_entity_id)
        _LOGGER.warning(
            "better_thermostat %s: Room temperature sensor %s unavailable, "
            "falling back to TRV internal temperature",
            self.device_name,
            self.sensor_entity_id,
        )
    else:
        # Update battery status for room temperature sensor
        self.hass.async_create_task(get_battery_status(self, self.sensor_entity_id))

    # Update instance state
    old_degraded = getattr(self, "degraded_mode", False)
    self.degraded_mode = len(unavailable) > 0
    self.unavailable_sensors = unavailable

    if self.degraded_mode and not old_degraded:
        _LOGGER.warning(
            "better_thermostat %s: Entering degraded mode. Unavailable sensors: %s",
            self.device_name,
            ", ".join(unavailable),
        )
        # Create a single issue for degraded mode
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=f"degraded_mode_{self.device_name}",
            is_fixable=False,
            is_persistent=False,
            learn_more_url="https://better-thermostat.org/qanda/degraded_mode",
            severity=ir.IssueSeverity.WARNING,
            translation_key="degraded_mode",
            translation_placeholders={
                "name": str(self.device_name),
                "sensors": ", ".join(unavailable),
            },
        )
    elif not self.degraded_mode and old_degraded:
        _LOGGER.info(
            "better_thermostat %s: Exiting degraded mode. All sensors available.",
            self.device_name,
        )
        ir.async_delete_issue(self.hass, DOMAIN, f"degraded_mode_{self.device_name}")

    self.async_write_ha_state()
    return self.degraded_mode
