"""Watcher helpers to verify the presence and state of configured entities.

This module contains utility functions to verify entities, check batteries,
and raise Home Assistant issues if an entity is missing or unavailable.

Supports degraded mode operation where optional sensors (window, humidity,
outdoor, weather) can be unavailable without blocking thermostat operation.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Window after startup during which a transition into degraded mode is logged
# at DEBUG and does not raise a Home Assistant repair. Slow integrations
# (cloud weather, Ecowitt, etc.) often need several minutes to publish their
# first state.
STARTUP_DEGRADED_GRACE_PERIOD = timedelta(minutes=5)

# Grace window after startup during which a temporarily unavailable TRV does
# not raise a ``missing_entity`` repair issue. Cloud-backed integrations (e.g.
# Tado) can take a while to reconnect after a HA reboot or network outage; the
# startup waiting loop already covers the initial load, but this protects
# against brief post-startup instability before surfacing a repair.
STARTUP_CRITICAL_GRACE_PERIOD = timedelta(minutes=2)

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
    self.hass.async_create_background_task(
        get_battery_status(self, entity), name=f"bt_battery_status_{entity}"
    )
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
                learn_more_url="https://better-thermostat.org/faq/missing-entity",
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

    During a startup grace period (``_critical_grace_until``), unavailable
    TRVs do not raise a Home Assistant repair issue — slow integrations
    (e.g. cloud-backed Tado valves) are given time to come online. The issue
    is created once the grace window elapses if the entity is still missing.

    When an entity becomes available again, any previously raised
    ``missing_entity_*`` issue is cleared automatically (and idempotently,
    so stale issues from a previous run are also removed).

    Returns
    -------
    bool
        True if all critical entities are available
    """
    critical = get_critical_entities(self)
    grace_until = getattr(self, "_critical_grace_until", None)
    in_grace = grace_until is not None and dt_util.now() < grace_until

    all_available = True
    for entity in critical:
        if not is_entity_available(self.hass, entity):
            if in_grace:
                _LOGGER.debug(
                    "better_thermostat %s: Critical entity %s is unavailable "
                    "during startup grace period; deferring repair issue",
                    self.device_name,
                    entity,
                )
            else:
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
                        learn_more_url="https://better-thermostat.org/faq/missing-entity",
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="missing_entity",
                        translation_placeholders={
                            "entity": str(entity),
                            "name": str(self.device_name),
                        },
                    )
            all_available = False
        else:
            # Clear error if entity is now available (covers recovery after an
            # outage and stale issues from a previous run).
            if entity in self.devices_errors:
                self.devices_errors.remove(entity)
                self.async_write_ha_state()
            ir.async_delete_issue(self.hass, DOMAIN, f"missing_entity_{entity}")
            # Update battery status for available entities
            self.hass.async_create_background_task(
                get_battery_status(self, entity), name=f"bt_battery_status_{entity}"
            )
    return all_available


# Default delays for the optional-sensor startup retry loop.
# Short initial interval catches fast local sensors (Zigbee), longer
# intervals give cloud / weather integrations time.  Total ≈ 60 s.
DEFAULT_OPTIONAL_SENSOR_DELAYS: tuple[int, ...] = (3, 5, 10, 15, 25)


async def await_optional_sensors(
    self,
    delays: tuple[int, ...] | list[int] = DEFAULT_OPTIONAL_SENSOR_DELAYS,
    _sleep=None,
) -> list[str]:
    """Wait for optional sensors to become available with increasing delays.

    After a reboot, optional sensors (outdoor, weather, window, humidity)
    frequently need a few seconds to initialise.  This helper retries with
    increasing intervals so that ``check_and_update_degraded_mode`` is not
    called while sensors are still starting up.

    Parameters
    ----------
    self :
        BetterThermostat instance (must expose ``.hass`` and
        ``.device_name``).
    delays :
        Sequence of sleep durations in seconds between retries.
        Defaults to ``DEFAULT_OPTIONAL_SENSOR_DELAYS`` (3/5/10/15/25 s).
    _sleep :
        Injectable sleep coroutine for testing.  Defaults to
        ``asyncio.sleep``.

    Returns
    -------
    list[str]
        Entity IDs of optional sensors that are still unavailable after
        all retries have been exhausted (empty if all came online).
    """
    if _sleep is None:
        _sleep = asyncio.sleep

    elapsed = 0
    pending: list[str] = []

    for idx, delay in enumerate(delays):
        pending = [
            eid
            for eid in get_optional_sensors(self)
            if not is_entity_available(self.hass, eid)
        ]
        if not pending:
            _LOGGER.debug(
                "better_thermostat %s: all optional sensors available (after %d s)",
                self.device_name,
                elapsed,
            )
            return []
        _LOGGER.debug(
            "better_thermostat %s: waiting for optional sensors "
            "(attempt %d/%d, next check in %d s, pending: %s)",
            self.device_name,
            idx + 1,
            len(delays),
            delay,
            ", ".join(pending),
        )
        await _sleep(delay)
        elapsed += delay

    # Final check after the last sleep
    pending = [
        eid
        for eid in get_optional_sensors(self)
        if not is_entity_available(self.hass, eid)
    ]
    if not pending:
        _LOGGER.debug(
            "better_thermostat %s: all optional sensors available (after %d s)",
            self.device_name,
            elapsed,
        )
    return pending


# Default delays for the critical-entity startup retry loop.  Critical entities
# are TRVs, which on cloud-backed integrations (e.g. Tado) can take noticeably
# longer to initialise than local Zigbee valves, so the schedule is slightly
# longer than the optional-sensor one.  Total ≈ 90 s.
DEFAULT_CRITICAL_ENTITY_DELAYS: tuple[int, ...] = (3, 5, 10, 15, 25, 30)


async def await_critical_entities(
    self,
    delays: tuple[int, ...] | list[int] = DEFAULT_CRITICAL_ENTITY_DELAYS,
    _sleep=None,
) -> list[str]:
    """Wait for critical entities (TRVs) to become available with retry delays.

    After a reboot, the underlying TRVs frequently need a few seconds to
    initialise.  Cloud-backed integrations (e.g. Tado) load even later than
    Home Assistant itself, so a single availability check at startup raises a
    false-positive ``missing_entity`` repair issue before the valve is ready.
    This helper retries with increasing intervals so that
    ``check_critical_entities`` is not called while TRVs are still starting up.

    Parameters
    ----------
    self :
        BetterThermostat instance (must expose ``.hass`` and
        ``.device_name``).
    delays :
        Sequence of sleep durations in seconds between retries.
        Defaults to ``DEFAULT_CRITICAL_ENTITY_DELAYS`` (3/5/10/15/25/30 s).
    _sleep :
        Injectable sleep coroutine for testing.  Defaults to
        ``asyncio.sleep``.

    Returns
    -------
    list[str]
        Entity IDs of critical entities that are still unavailable after all
        retries have been exhausted (empty if all came online).
    """
    if _sleep is None:
        _sleep = asyncio.sleep

    elapsed = 0
    pending: list[str] = []

    for idx, delay in enumerate(delays):
        # The entity may be torn down mid-wait; stop retrying immediately
        # instead of running out the (up to ~90 s) schedule against a
        # being-removed instance.
        if getattr(self, "is_removed", False):
            return pending
        pending = [
            eid
            for eid in get_critical_entities(self)
            if not is_entity_available(self.hass, eid)
        ]
        if not pending:
            _LOGGER.debug(
                "better_thermostat %s: all critical entities available (after %d s)",
                self.device_name,
                elapsed,
            )
            return []
        _LOGGER.debug(
            "better_thermostat %s: waiting for critical entities "
            "(attempt %d/%d, next check in %d s, pending: %s)",
            self.device_name,
            idx + 1,
            len(delays),
            delay,
            ", ".join(pending),
        )
        await _sleep(delay)
        elapsed += delay
        if getattr(self, "is_removed", False):
            return pending

    # Final check after the last sleep
    pending = [
        eid
        for eid in get_critical_entities(self)
        if not is_entity_available(self.hass, eid)
    ]
    if not pending:
        _LOGGER.debug(
            "better_thermostat %s: all critical entities available (after %d s)",
            self.device_name,
            elapsed,
        )
    return pending


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
            self.hass.async_create_background_task(
                get_battery_status(self, entity), name=f"bt_battery_status_{entity}"
            )

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
        self.hass.async_create_background_task(
            get_battery_status(self, self.sensor_entity_id),
            name=f"bt_battery_status_{self.sensor_entity_id}",
        )

    # Update instance state
    old_degraded = getattr(self, "degraded_mode", False)
    self.degraded_mode = len(unavailable) > 0
    self.unavailable_sensors = unavailable

    grace_until = getattr(self, "_degraded_grace_until", None)
    in_grace = grace_until is not None and dt_util.now() < grace_until
    has_warned = getattr(self, "_degraded_warning_emitted", False)

    if self.degraded_mode and not has_warned and not in_grace:
        _LOGGER.warning(
            "better_thermostat %s: Entering degraded mode. Unavailable sensors: %s",
            self.device_name,
            ", ".join(unavailable),
        )
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=f"degraded_mode_{self.device_name}",
            is_fixable=False,
            is_persistent=False,
            learn_more_url="https://better-thermostat.org/faq/degraded-mode",
            severity=ir.IssueSeverity.WARNING,
            translation_key="degraded_mode",
            translation_placeholders={
                "name": str(self.device_name),
                "sensors": ", ".join(unavailable),
            },
        )
        self._degraded_warning_emitted = True
    elif self.degraded_mode and in_grace and not old_degraded:
        _LOGGER.debug(
            "better_thermostat %s: degraded mode during startup grace period "
            "(unavailable: %s); waiting for sensors before warning",
            self.device_name,
            ", ".join(unavailable),
        )
    elif not self.degraded_mode and has_warned:
        _LOGGER.info(
            "better_thermostat %s: Exiting degraded mode. All sensors available.",
            self.device_name,
        )
        ir.async_delete_issue(self.hass, DOMAIN, f"degraded_mode_{self.device_name}")
        self._degraded_warning_emitted = False

    self.async_write_ha_state()
    return self.degraded_mode
