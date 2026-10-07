"""Quirks and helpers for Aqara SRTS-A01 (Zigbee TRV) devices.

Provides Aqara SRTS-A01 specific helper functions such as mirroring the
external temperature into the TRV and keeping its sensor selector on the
external input, which the device requires before it reads that value.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    """Return unchanged local calibration for SRTS-A01 by default.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        entity_id of the TRV
    offset : float
        the calculated local calibration offset

    Returns
    -------
    float
        The offset, unchanged.
    """
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return unchanged setpoint temperature for SRTS-A01 by default.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        entity_id of the TRV
    temperature : float
        the calculated target temperature

    Returns
    -------
    float
        The temperature, unchanged.
    """
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """No special HVAC mode handling for SRTS-A01; the generic adapter performs the write.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id : str
            entity_id of the TRV
    hvac_mode : str
            the HVAC mode to set

    Returns
    -------
    bool
            False, always: the generic adapter fallback performs the
            service call, including its retry handling
    """
    return False


async def override_set_temperature(self, entity_id, temperature):
    """No special setpoint handling for SRTS-A01; the generic adapter performs the write.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id : str
            entity_id of the TRV
    temperature : float
            the target temperature to set

    Returns
    -------
    bool
            False, always: the generic adapter fallback performs the
            service call, including step rounding and system-unit
            conversion
    """
    return False


# How long to wait for the sensor selector to report the external option.
EXTERNAL_SENSOR_SWITCH_TIMEOUT = 10.0


async def async_wait_for_external_sensor(hass, entity_id: str) -> bool:
    """Wait until a sensor selector reports an external option.

    Parameters
    ----------
    hass :
        The Home Assistant instance whose state machine is watched.
    entity_id : str
        The sensor selector to watch.

    Returns
    -------
    bool
        True when the selector is, or becomes, an option naming an external
        sensor within ``EXTERNAL_SENSOR_SWITCH_TIMEOUT`` seconds, False when
        it does not.
    """
    selected = asyncio.Event()

    @callback
    def _handle_state_change(event) -> None:
        new_state = event.data.get("new_state")
        if new_state is not None and str(new_state.state).startswith("external"):
            selected.set()

    unsubscribe = async_track_state_change_event(
        hass, [entity_id], _handle_state_change
    )
    try:
        state = hass.states.get(entity_id)
        if state is not None and str(state.state).startswith("external"):
            return True
        await asyncio.wait_for(selected.wait(), EXTERNAL_SENSOR_SWITCH_TIMEOUT)
        return True
    except TimeoutError:
        return False
    finally:
        unsubscribe()


# Translation keys Zigbee2MQTT uses for the input the room temperature is
# mirrored into.
_TK_EXTERNAL_TEMP = frozenset({"external_temperature_input", "external_temperature"})

# Translation keys Zigbee2MQTT uses for the selector that decides which sensor
# the SRTS-A01 regulates on.
_TK_SENSOR_SELECT = frozenset({"sensor"})

# The option that hands regulation to the value BT writes. Devices offer more
# than one option naming an external sensor, so the ones already on such an
# option are left as their owner set them.
_EXTERNAL_SENSOR_OPTION = "external"

# The option the device reports its own reading on. While the selector is on
# the external input the device echoes back what BT last wrote, so the TRV
# temperature is no fallback for a failed room sensor until it is selected.
_INTERNAL_SENSOR_OPTION = "internal"


def _room_sensor_unavailable(self) -> bool:
    """Return True while the configured room temperature sensor is not reporting."""
    sensor_id = getattr(self, "sensor_entity_id", None)
    if not sensor_id:
        return False
    state = self.hass.states.get(sensor_id)
    return state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _find_device_entity(
    entity_registry: er.EntityRegistry,
    device_id: str | None,
    domain: str,
    translation_keys: frozenset[str],
    id_fragment: str,
) -> str | None:
    """Return a sibling entity of ``device_id`` in ``domain``, or ``None``.

    The translation key is the stable, language-independent handle and is
    tried first; the id fragment is the fallback for a registry entry that
    carries none.

    Parameters
    ----------
    entity_registry : er.EntityRegistry
        The registry to search.
    device_id : str | None
        The device the sibling has to belong to. ``None`` is no device and
        matches nothing: every entity that belongs to no device would
        otherwise be a candidate.
    domain : str
        The entity domain to search, ``number`` or ``select`` here.
    translation_keys : frozenset[str]
        The translation keys that name the wanted entity.
    id_fragment : str
        Matched against the entity id, unique id and original name of a
        registry entry that carries no translation key.

    Returns
    -------
    str | None
        The entity id of the translation key match, the first id fragment
        match when no entry carries one of the keys, or ``None`` when the
        device has no such entity.
    """
    if device_id is None:
        return None
    siblings = [
        ent
        for ent in entity_registry.entities.values()
        if ent.device_id == device_id and ent.domain == domain
    ]
    for ent in siblings:
        if getattr(ent, "translation_key", None) in translation_keys:
            return ent.entity_id
    # Only now, and only for entries that name themselves nothing: the
    # registry hands its entities out in insertion order, so a fragment match
    # tried per entry would beat the canonical key of an entry behind it.
    for ent in siblings:
        if getattr(ent, "translation_key", None) is not None:
            continue
        haystacks = (
            (ent.entity_id or "").lower(),
            (ent.unique_id or "").lower(),
            (getattr(ent, "original_name", None) or "").lower().replace(" ", "_"),
        )
        if any(id_fragment in haystack for haystack in haystacks):
            return ent.entity_id
    return None


def _find_sensor_selector(self, entity_id: str) -> str | None:
    """Return the sensor selector belonging to a TRV device.

    Parameters
    ----------
    self :
        The Better Thermostat instance, supplying ``hass``.
    entity_id : str
        The TRV whose device carries the selector.

    Returns
    -------
    str | None
        The entity id of the selector, or ``None`` when the TRV has no
        registry entry or its device has no such selector.
    """
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        return None
    return _find_device_entity(
        entity_registry, reg_entity.device_id, "select", _TK_SENSOR_SELECT, "sensor"
    )


def register_external_sensor_watch(self, entity_id: str):
    """Watch a TRV's sensor selector and put it back on the external input.

    The device falls back to its internal sensor on its own, for instance when
    it is re-paired. When the selector reports an option that names no external
    sensor, the room temperature is written again, which selects the external
    sensor as well. At most one such repair runs per TRV at a time, and it is
    cancelled when the watch is removed.

    While the room sensor is unavailable the selector is deliberately moved to
    the internal sensor, so the TRV reports its real reading for the fallback,
    and no repair runs. When the room sensor reports again, the room
    temperature is written, which selects the external sensor once more.

    Parameters
    ----------
    self :
        The Better Thermostat instance, supplying ``hass``, the TRV registry,
        the current temperature and the removal flag.
    entity_id : str
        The TRV whose sensor selector is watched.

    Returns
    -------
    Callable[[], None] | None
        A callable that stops the watch and cancels a pending repair, or
        ``None`` when no external temperature sensor is configured or the
        TRV has no sensor selector to watch.
    """
    if not self.sensor_entity_id:
        return None
    selector_id = _find_sensor_selector(self, entity_id)
    trv = self.real_trvs.get(entity_id)
    if selector_id is None or trv is None:
        return None

    task_key = "_external_sensor_repair_task"
    factory_key = "_external_sensor_repair_factory"

    @callback
    def _handle_selector_change(event) -> None:
        new_state = event.data.get("new_state")
        if (
            new_state is None
            or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            or str(new_state.state).startswith(_EXTERNAL_SENSOR_OPTION)
            or self.is_removed
            or _room_sensor_unavailable(self)
        ):
            return
        _schedule(_repair_external_temperature)

    def _schedule(factory) -> None:
        pending = trv.extra.get(task_key)
        if pending is not None and not pending.done():
            if trv.extra.get(factory_key) is factory:
                return
            pending.cancel()

        async def _run() -> None:
            try:
                await factory()
            finally:
                # A cancelled task must not clear the task that replaced it.
                if trv.extra.get(task_key) is task:
                    trv.extra.pop(task_key, None)
                    trv.extra.pop(factory_key, None)

        task = self.hass.async_create_background_task(
            _run(), name=f"bt_external_sensor_repair_{entity_id}"
        )
        trv.extra[task_key] = task
        trv.extra[factory_key] = factory

    async def _repair_external_temperature() -> None:
        temperature = self.cur_temp
        if temperature is not None:
            await maybe_set_external_temperature(self, entity_id, temperature)

    async def _select_internal() -> None:
        await maybe_select_internal_sensor(self, entity_id)

    @callback
    def _handle_room_sensor_change(event) -> None:
        new_state = event.data.get("new_state")
        if self.is_removed:
            return
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _schedule(_select_internal)
        else:
            _schedule(_repair_external_temperature)

    unsub_selector = async_track_state_change_event(
        self.hass, [selector_id], _handle_selector_change
    )
    unsub_room = async_track_state_change_event(
        self.hass, [self.sensor_entity_id], _handle_room_sensor_change
    )

    if _room_sensor_unavailable(self):
        _schedule(_select_internal)

    def _unsubscribe() -> None:
        unsub_selector()
        unsub_room()
        pending = trv.extra.pop(task_key, None)
        trv.extra.pop(factory_key, None)
        if pending is not None and not pending.done():
            pending.cancel()

    return _unsubscribe


async def maybe_select_internal_sensor(self, entity_id: str) -> bool:
    """Point the TRV's sensor selector at its own sensor.

    Used while the room sensor fails, so that the TRV reports its real
    temperature instead of the value BT last wrote.

    Parameters
    ----------
    self :
        The Better Thermostat instance, supplying ``hass`` and the context
        the service call is made under.
    entity_id : str
        The TRV whose device carries the selector.

    Returns
    -------
    bool
        True when the selector is on the internal option, whether this call
        put it there or found it there.
    """
    target = _find_sensor_selector(self, entity_id)
    if target is None:
        return False
    state = self.hass.states.get(target)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return False
    if state.state == _INTERNAL_SENSOR_OPTION:
        return True
    options = state.attributes.get("options")
    if not isinstance(options, (list, tuple)) or _INTERNAL_SENSOR_OPTION not in options:
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": target, "option": _INTERNAL_SENSOR_OPTION},
        blocking=True,
        context=self.context,
    )
    _LOGGER.debug(
        "better_thermostat %s: room sensor unavailable, set SRTS-A01 %s from '%s' to '%s' (for %s)",
        self.device_name,
        target,
        state.state,
        _INTERNAL_SENSOR_OPTION,
        entity_id,
    )
    return True


async def maybe_select_external_sensor(self, entity_id: str) -> bool:
    """Point the TRV's sensor selector at the value BT writes.

    Writing the external temperature input achieves nothing while the device
    regulates on its own sensor, and it lands there on its own: a SRTS-A01 that
    is re-paired comes back on the internal sensor. So the selector is checked
    alongside every write of the input it belongs to.

    A device already on an option naming an external sensor is left alone,
    whichever of them it is: the choice between them is its owner's.

    Parameters
    ----------
    self :
        The Better Thermostat instance, supplying ``hass`` and the context
        the service call is made under.
    entity_id : str
        The TRV whose device carries the selector.

    Returns
    -------
    bool
        True when the selector is on an external option, whether this call
        put it there or found it there.
    """

    if _room_sensor_unavailable(self):
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 maybe_select_external_sensor: room sensor unavailable for %s",
            self.device_name,
            entity_id,
        )
        return False

    _LOGGER.debug(
        "better_thermostat %s: SRTS-A01 maybe_select_external_sensor: setting external sensor",
        self.device_name,
    )

    target = _find_sensor_selector(self, entity_id)
    if target is None:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 maybe_select_external_sensor: no registry entity for %s",
            self.device_name,
            entity_id,
        )
        return False
    state = self.hass.states.get(target)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        # A selector that is not reporting names no option, and the device
        # behind it is in no state to take one either.
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 temperature sensor selector %s is unavailable or unknown for %s",
            self.device_name,
            target,
            entity_id,
        )
        return False
    if str(state.state).startswith(_EXTERNAL_SENSOR_OPTION):
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 temperature sensor selector %s already on '%s' for %s",
            self.device_name,
            target,
            state.state,
            entity_id,
        )
        return True
    options = state.attributes.get("options")
    if not isinstance(options, (list, tuple)) or _EXTERNAL_SENSOR_OPTION not in options:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 selector %s offers no '%s' option (%s)",
            self.device_name,
            target,
            _EXTERNAL_SENSOR_OPTION,
            options,
        )
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": target, "option": _EXTERNAL_SENSOR_OPTION},
        blocking=True,
        context=self.context,
    )
    if not await async_wait_for_external_sensor(self.hass, target):
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 selector %s did not report an external sensor in time",
            self.device_name,
            target,
        )
        return False
    _LOGGER.debug(
        "better_thermostat %s: set SRTS-A01 %s from '%s' to '%s' (for %s)",
        self.device_name,
        target,
        state.state,
        _EXTERNAL_SENSOR_OPTION,
        entity_id,
    )
    return True


async def maybe_set_external_temperature(self, entity_id, temperature: float) -> bool:
    """Set Aqara SRTS-A01 external temperature input via a number entity on the same device.

    Looks for number.* entity matching external_temperature_input and writes the
    given temperature (clamped to 0..55.0, rounded to one decimal). The sensor
    selector is pointed at that input alongside the write, because a device
    regulating on its own sensor never reads it.

    Parameters
    ----------
    self :
        The Better Thermostat instance, supplying ``hass``, the TRV registry
        and the context the service calls are made under.
    entity_id : str
        The TRV whose device carries the input.
    temperature : float
        The room temperature to mirror into the device, in degrees Celsius.

    Returns
    -------
    bool
        True when the input was written, False when the device is not a
        SRTS-A01, names no such input, the value is not a number, or its
        sensor cannot be set to external.
    """

    _LOGGER.debug(
        "better_thermostat %s: SRTS-A01 maybe_set_external_temperature: setting external temperature to %.1f for %s",
        self.device_name,
        temperature,
        entity_id,
    )
    try:
        model = str(self.real_trvs[entity_id].model or "")
        if "srts-a01" not in model.lower():
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 maybe_set_external_temperature skipped (model=%s)",
                self.device_name,
                model,
            )
            return False
        entity_registry = er.async_get(self.hass)
        reg_entity = entity_registry.async_get(entity_id)
        if reg_entity is None:
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 maybe_set_external_temperature: no registry entity for %s",
                self.device_name,
                entity_id,
            )
            return False
        target = _find_device_entity(
            entity_registry,
            reg_entity.device_id,
            "number",
            _TK_EXTERNAL_TEMP,
            "external_temperature_input",
        )
        if target is None:
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 external_temperature_input number entity not found for %s",
                self.device_name,
                entity_id,
            )
            return False

        # Clamp and round
        try:
            val = float(temperature)
        except TypeError, ValueError:
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 maybe_set_external_temperature got non-float: %s",
                self.device_name,
                temperature,
            )
            return False
        val = max(0.0, min(55.0, round(val, 1)))

        if not await maybe_select_external_sensor(self, entity_id):
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 maybe_set_external_temperature failed to select external sensor for %s",
                self.device_name,
                entity_id,
            )
            return False

        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": target, "value": val},
            blocking=True,
            context=self.context,
        )
        _LOGGER.debug(
            "better_thermostat %s: set SRTS-A01 external_temperature_input=%.1f on %s (for %s)",
            self.device_name,
            val,
            target,
            entity_id,
        )

        return True
    except (TypeError, ValueError, KeyError, AttributeError) as ex:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 maybe_set_external_temperature exception: %s",
            self.device_name,
            ex,
        )
        return False
