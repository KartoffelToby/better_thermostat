"""Quirks for the Aqara SRTS-A01 (Smart radiator thermostat E1) Zigbee TRV.

The SRTS-A01 regulates on an external temperature once its ``sensor``
selector is on ``external``, and reads that temperature from the
``external_temperature_input`` number (0 to 55 °C) nobody else keeps
current. The room temperature is mirrored into that input, with the
selector pointed at it first.

Unlike the Sonoff TRVZB, the SRTS-A01 does not fall back to its own sensor
when the input goes quiet: it keeps regulating on the last value written,
and reports that value as its ``local_temperature``. Once the room sensor
has lost its reading, Better Thermostat steps down its control-mode ladder
to ``SENSOR_FALLBACK`` and controls on the TRV temperature, which on this
device would be its own stale input read back.

So the quirk keeps the selector in line with the thermostat's control mode:

* ``OPTIMAL``, controlling on the room sensor: ``external``;
* any rung below it: ``internal``. Under ``SENSOR_FALLBACK`` the device then
  reports its real reading for the core to control on; under ``HOLD`` the
  input it would read has gone stale as well.

The selector is brought in line before every write of the input, whenever
the thermostat entity changes state, which it does when the ladder commits
a new rung, and whenever the selector itself changes, so an option set by
hand or by another automation is put back. A selector already in line is
left alone. When the selector goes back to ``external`` outside a write, the
room temperature is written into the input with it.

Everything else stays as the default quirks have it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
import logging
import time

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util.unit_conversion import TemperatureConverter

from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.model_fixes.default import (
    initial_tweak as default_initial_tweak,
)
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    ModelFixHost,
    ModelQuirks,
)
from custom_components.better_thermostat.utils.helpers import (
    device_released,
    is_sibling_entry,
)

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Return unchanged local calibration for SRTS-A01 by default."""
    return calibration_offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Return unchanged setpoint temperature for SRTS-A01 by default."""
    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """No special HVAC mode handling for SRTS-A01; the generic adapter performs the write.

    Parameters
    ----------
    self : ModelFixHost
            Better Thermostat host providing device state and HA access
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


async def override_set_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """No special setpoint handling for SRTS-A01; the generic adapter performs the write.

    Parameters
    ----------
    self : ModelFixHost
            Better Thermostat host providing device state and HA access
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


# Translation keys Zigbee2MQTT uses for the input the room temperature is
# mirrored into.
_TK_EXTERNAL_TEMP = frozenset({"external_temperature_input", "external_temperature"})

# Translation keys Zigbee2MQTT uses for the selector that decides which sensor
# the SRTS-A01 regulates on.
_TK_SENSOR_SELECT = frozenset({"sensor_select", "sensor"})

# Zigbee2MQTT exposes the SRTS-A01 ``sensor`` enum with exactly these two
# values.
_EXTERNAL_SENSOR_OPTION = "external"
_INTERNAL_SENSOR_OPTION = "internal"

# Range of the external temperature input, in degrees Celsius, for an input
# that publishes no bounds of its own.
_EXTERNAL_TEMPERATURE_MIN_C = 0.0
_EXTERNAL_TEMPERATURE_MAX_C = 55.0

# The listener per TRV, keyed by the TRV's climate entity id: the thermostat
# it was registered for, and how to unsubscribe it. A reloaded thermostat is
# a new host and registers anew.
_SELECTOR_WATCHES: dict[str, tuple[ModelFixHost, Callable[[], None]]] = {}

# TRVs whose selector is being brought in line. A switch changes the selector,
# which is itself a change the watch answers, and states can arrive in a row;
# a change arriving meanwhile marks the TRV in ``_KEEP_AGAIN`` instead of
# starting a second run, and the run checks once more before it ends.
_KEEPING: set[str] = set()
_KEEP_AGAIN: set[str] = set()

# One check at a time per TRV. A write by Better Thermostat and the state
# change of its entity arrive together when the ladder commits a rung, and two
# checks side by side would both find the old option and both switch.
_SELECTOR_LOCKS: dict[str, asyncio.Lock] = {}

# The option last sent to each TRV's selector, and when. The device confirms a
# switch seconds later, and until it does the selector still reports the old
# option; a check in between waits for the confirmation instead of sending the
# switch again. One that has not arrived after ``_SWITCH_CONFIRM_S`` counts as
# lost, and the next check sends the switch again; one check runs when that
# window ends, so a quiet room does not leave the lost switch unsent.
_PENDING_SWITCHES: dict[str, tuple[str, float]] = {}
_SWITCH_CONFIRM_S = 30.0


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
        matches nothing. A disabled entry is no sibling either.
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
        The entity id of the sibling whose translation key names it, the
        first id fragment match when no sibling carries one of the keys, or
        ``None`` when the device has no such entity.
    """
    siblings = [
        ent
        for ent in entity_registry.entities.values()
        if is_sibling_entry(ent, device_id) and ent.domain == domain
    ]
    for ent in siblings:
        if ent.translation_key in translation_keys:
            return ent.entity_id
    # The registry hands its entities out in insertion order, so a fragment
    # match tried per entry beats the canonical key of an entry behind it.
    # The fallback therefore runs as a second pass, and only over the entries
    # that name themselves nothing.
    for ent in siblings:
        if ent.translation_key is not None:
            continue
        haystacks = (
            (ent.entity_id or "").lower(),
            (ent.unique_id or "").lower(),
            (ent.original_name or "").lower().replace(" ", "_"),
        )
        if any(id_fragment in haystack for haystack in haystacks):
            return ent.entity_id
    return None


def _sensor_selector(self: ModelFixHost, entity_id: str) -> str | None:
    """Return the ``sensor`` selector on the TRV's device, or ``None``."""
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        return None
    return _find_device_entity(
        entity_registry, reg_entity.device_id, "select", _TK_SENSOR_SELECT, "sensor"
    )


async def _select_sensor(
    self: ModelFixHost, entity_id: str, selector: str, option: str
) -> bool:
    """Put ``selector`` on ``option`` unless it is already there.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass`` and the context
        the service call is made under.
    entity_id : str
        The TRV whose device carries the selector, for log lines.
    selector : str
        The ``select`` entity to set.
    option : str
        The option to put it on.

    Returns
    -------
    bool
        True when the selector is on ``option``, whether this call put it
        there or found it there.
    """
    state = self.hass.states.get(selector)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        # A selector that is not reporting names no option, and the device
        # behind it is in no state to take one either.
        return False
    if state.state == option:
        return True
    options = state.attributes.get("options")
    if not isinstance(options, (list, tuple)) or option not in options:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 selector %s offers no '%s' option (%s)",
            self.device_name,
            selector,
            option,
            options,
        )
        return False
    await self.hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": selector, "option": option},
        blocking=True,
        context=self.context,
    )
    _LOGGER.debug(
        "better_thermostat %s: set SRTS-A01 %s from '%s' to '%s' (for %s)",
        self.device_name,
        selector,
        state.state,
        option,
        entity_id,
    )
    return True


def _wanted_option(self: ModelFixHost) -> str:
    """Return the selector option the thermostat's control mode calls for.

    ``external`` while Better Thermostat controls on the room sensor,
    ``internal`` on every rung below. ``HOLD`` is no exception: it follows
    ``SENSOR_FALLBACK`` once the TRV temperature is gone too, and the input
    the device would read has gone stale with the room sensor.
    """
    if self.kernel_state.control_mode.mode == ControlMode.OPTIMAL:
        return _EXTERNAL_SENSOR_OPTION
    return _INTERNAL_SENSOR_OPTION


async def _keep_selector(self: ModelFixHost, entity_id: str) -> str | None:
    """Bring the TRV's sensor selector in line with the control mode.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass`` and the context
        the service call is made under.
    entity_id : str
        The TRV whose device carries the selector.

    Returns
    -------
    str | None
        The option the device confirmed in this check, a switch sent earlier
        now reported by the selector. ``None`` when a switch was only sent,
        awaits the device's confirmation, the selector was in line without
        one, the device has no selector or the selector is not reporting.
    """
    lock = _SELECTOR_LOCKS.setdefault(entity_id, asyncio.Lock())
    async with lock:
        return await _keep_selector_now(self, entity_id)


async def _keep_selector_now(self: ModelFixHost, entity_id: str) -> str | None:
    """Run one check of ``_keep_selector`` under the TRV's lock."""
    if device_released(self, entity_id):
        # The device is the user's: an option set by hand stays, and nothing
        # is written to it.
        return None
    selector = _sensor_selector(self, entity_id)
    if selector is None:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 %s has no sensor selector to keep",
            self.device_name,
            entity_id,
        )
        return None
    mode = self.kernel_state.control_mode.mode
    wanted = _wanted_option(self)
    state = self.hass.states.get(selector)
    current = state.state if state is not None else None
    pending = _PENDING_SWITCHES.get(entity_id)
    now = time.monotonic()
    if current == wanted:
        _PENDING_SWITCHES.pop(entity_id, None)
        outcome = (
            "switch confirmed by the device"
            if pending is not None and pending[0] == wanted
            else "left as it is"
        )
    elif (
        pending is not None
        and pending[0] == wanted
        and now - pending[1] < _SWITCH_CONFIRM_S
    ):
        outcome = (
            f"switch sent {now - pending[1]:.0f} s ago, waiting for the device "
            "to confirm it"
        )
    else:
        outcome = "switching"
    _LOGGER.debug(
        "better_thermostat %s: SRTS-A01 %s check: control mode '%s', "
        "selector %s is '%s', wants '%s': %s",
        self.device_name,
        entity_id,
        mode,
        selector,
        current,
        wanted,
        outcome,
    )
    if outcome == "switch confirmed by the device":
        return wanted
    if outcome != "switching":
        return None
    if not await _select_sensor(self, entity_id, selector, wanted):
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 %s selector %s not set to '%s' now "
            "(not reporting or no such option); the next change tries again",
            self.device_name,
            entity_id,
            selector,
            wanted,
        )
        return None
    _PENDING_SWITCHES[entity_id] = (wanted, time.monotonic())
    _recheck_when_unconfirmed(self, entity_id)
    # A device that keeps losing the switch gets it again every window; the
    # change it announces is logged once, not with every resend.
    level = (
        logging.DEBUG if pending is not None and pending[0] == wanted else logging.INFO
    )
    if wanted == _INTERNAL_SENSOR_OPTION:
        _LOGGER.log(
            level,
            "better_thermostat %s: control mode '%s' leaves the room sensor, "
            "SRTS-A01 %s regulates on its own sensor until it is back",
            self.device_name,
            mode,
            entity_id,
        )
    else:
        _LOGGER.log(
            level,
            "better_thermostat %s: control mode '%s', SRTS-A01 %s regulates on "
            "the room temperature",
            self.device_name,
            mode,
            entity_id,
        )
    return None


def _recheck_when_unconfirmed(self: ModelFixHost, entity_id: str) -> None:
    """Check the selector once more when the switch just sent goes unconfirmed.

    The watch answers state changes only, and Home Assistant reports none
    while neither the thermostat nor the selector changes. A switch the
    device lost would then stay unsent until something in the room changes.
    The check runs a second past the confirmation window, which it then
    finds over, and is skipped once the switch is confirmed or replaced. A
    thermostat reloaded meanwhile leaves the check to the one watching the
    TRV now, which found the switch pending and scheduled none of its own.
    """
    sent = _PENDING_SWITCHES[entity_id]

    @callback
    def _check(now: datetime) -> None:
        if _PENDING_SWITCHES.get(entity_id) is not sent:
            return
        host = self
        if host.is_removed:
            watch = _SELECTOR_WATCHES.get(entity_id)
            if watch is None or watch[0].is_removed:
                return
            host = watch[0]
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 %s did not confirm '%s' within %.0f s, "
            "checking its sensor selector again",
            host.device_name,
            entity_id,
            sent[0],
            _SWITCH_CONFIRM_S,
        )
        _start_keeping(host, entity_id)

    async_call_later(self.hass, _SWITCH_CONFIRM_S + 1.0, _check)


async def _keep_selector_once(self: ModelFixHost, entity_id: str) -> None:
    """Bring the selector in line outside a write, reporting a refused switch.

    Once the device confirms ``external``, it is handed the room temperature
    right away rather than at the next write, which can be half an hour off.
    A value written before the confirmation reached a device still on its
    own sensor, which drops it and goes on reporting its own reading.
    """
    try:
        confirmed = await _keep_selector(self, entity_id)
        if confirmed != _EXTERNAL_SENSOR_OPTION:
            return
        reading = self.room_temperature
        target = _input_number(self, entity_id)
        if reading is not None and target is not None:
            await _write_input(self, entity_id, target, reading)
    except (HomeAssistantError, OSError, TypeError, ValueError, KeyError) as ex:
        # The device is asleep or out of reach. The next change of the
        # thermostat or the selector, or the next write, tries again.
        _LOGGER.warning(
            "better_thermostat %s: SRTS-A01 sensor selector of %s not set: %s",
            self.device_name,
            entity_id,
            ex,
        )


async def _keep_selector_until_settled(self: ModelFixHost, entity_id: str) -> None:
    """Run ``_keep_selector_once`` until no change arrived during the run."""
    try:
        while not self.is_removed:
            _KEEP_AGAIN.discard(entity_id)
            await _keep_selector_once(self, entity_id)
            if entity_id not in _KEEP_AGAIN:
                return
    finally:
        _KEEPING.discard(entity_id)
        _KEEP_AGAIN.discard(entity_id)


def _start_keeping(self: ModelFixHost, entity_id: str) -> None:
    """Bring the selector in line under the thermostat's task manager."""
    if entity_id in _KEEPING:
        _KEEP_AGAIN.add(entity_id)
        return
    # Marked before the task exists: Home Assistant may start it eagerly, and
    # a run that finishes within that start clears the mark itself.
    _KEEPING.add(entity_id)
    task = self.task_manager.create_task(
        _keep_selector_until_settled(self, entity_id),
        name=f"bt_srts_a01_sensor_selector_{entity_id}",
    )
    if task is None:
        # The thermostat is being removed and started nothing.
        _KEEPING.discard(entity_id)
        return

    def _forget_cancelled(done: asyncio.Task[None]) -> None:
        # A task cancelled before its first step never enters the run, so
        # the run's own cleanup does not clear the mark; left behind, it
        # would keep a reloaded thermostat from checking the TRV at all.
        if done.cancelled():
            _KEEPING.discard(entity_id)
            _KEEP_AGAIN.discard(entity_id)

    task.add_done_callback(_forget_cancelled)


def _stop_watching(self: ModelFixHost, entity_id: str) -> None:
    """Unsubscribe the selector watch ``self`` registered for the TRV."""
    existing = _SELECTOR_WATCHES.get(entity_id)
    if existing is not None and existing[0] is self:
        del _SELECTOR_WATCHES[entity_id]
        existing[1]()


def _watch_selector(self: ModelFixHost, entity_id: str) -> None:
    """Keep the selector in line whenever the thermostat or the selector changes.

    Registered once per TRV and thermostat. The thermostat entity changes
    state when its ladder commits a new rung, which can call for the other
    option; the selector changing, by hand or by another automation, is put
    back to the option the control mode calls for.
    """
    existing = _SELECTOR_WATCHES.get(entity_id)
    if existing is not None:
        if existing[0] is self:
            return
        # A thermostat that was reloaded leaves the listener of its old self.
        existing[1]()
        del _SELECTOR_WATCHES[entity_id]
    selector = _sensor_selector(self, entity_id)
    if selector is None:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 %s has no sensor selector to watch",
            self.device_name,
            entity_id,
        )
        return
    watched = [self.entity_id, selector]

    @callback
    def _on_change(event: Event[EventStateChangedData]) -> None:
        if self.is_removed:
            _stop_watching(self, entity_id)
            return
        selected = self.hass.states.get(selector)
        if (
            selected is not None
            and selected.state == _wanted_option(self)
            and entity_id not in _PENDING_SWITCHES
        ):
            # The thermostat writes its state with every new reading; one
            # that leaves the selector in line needs no check.
            return
        new_state = event.data["new_state"]
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 %s changed to '%s', checking the "
            "sensor selector of %s",
            self.device_name,
            event.data["entity_id"],
            new_state.state if new_state is not None else None,
            entity_id,
        )
        _start_keeping(self, entity_id)

    unsubscribe = async_track_state_change_event(self.hass, watched, _on_change)
    _SELECTOR_WATCHES[entity_id] = (self, unsubscribe)
    _LOGGER.debug(
        "better_thermostat %s: SRTS-A01 watching %s to keep the sensor selector "
        "of %s in line",
        self.device_name,
        ", ".join(watched),
        entity_id,
    )


async def initial_tweak(self: ModelFixHost, entity_id: str) -> None:
    """Run the default tweaks, bring the selector in line and start watching it.

    A thermostat that starts on ``SENSOR_FALLBACK`` writes the input not a
    single time, so the selector is brought in line here as well.
    """
    await default_initial_tweak(self, entity_id)
    _watch_selector(self, entity_id)
    await _keep_selector_once(self, entity_id)


def _input_number(self: ModelFixHost, entity_id: str) -> str | None:
    """Return the ``external_temperature_input`` number on the TRV's device."""
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None:
        _LOGGER.debug(
            "better_thermostat %s: SRTS-A01 no registry entity for %s",
            self.device_name,
            entity_id,
        )
        return None
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
    return target


async def _write_input(
    self: ModelFixHost, entity_id: str, target: str, celsius: float
) -> None:
    """Write ``celsius`` into the external temperature input ``target``."""
    value = _external_input_value(self.hass.states.get(target), celsius)
    await self.hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": target, "value": value},
        blocking=True,
        context=self.context,
    )
    _LOGGER.debug(
        "better_thermostat %s: set SRTS-A01 external_temperature_input=%.2f on %s (for %s)",
        self.device_name,
        value,
        target,
        entity_id,
    )


def _external_input_value(state: State | None, celsius: float) -> float:
    """Return the value the external temperature input takes for ``celsius``.

    Zigbee2MQTT publishes the input as a ``temperature`` number in Celsius,
    which Home Assistant converts into the system unit, and checks a written
    value against the ``min`` and ``max`` it publishes in that unit. The
    reading is rounded once to the device's tenth of a degree Celsius, then
    converted into the unit the input publishes and held inside its bounds.

    Parameters
    ----------
    state : State or None
        State of the external temperature input, or None when it has none.
    celsius : float
        The room temperature in degrees Celsius.

    Returns
    -------
    float
        The value to write, in the input's published unit.
    """
    attributes: Mapping[str, object] = state.attributes if state is not None else {}
    declared_unit = attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    unit = (
        declared_unit
        if isinstance(declared_unit, str)
        and declared_unit in TemperatureConverter.VALID_UNITS
        else UnitOfTemperature.CELSIUS
    )

    def published(degrees: float) -> float:
        return TemperatureConverter.convert(degrees, UnitOfTemperature.CELSIUS, unit)

    def bound(name: str, fallback_celsius: float) -> float:
        try:
            return float(str(attributes[name]))
        except KeyError, TypeError, ValueError:
            return published(fallback_celsius)

    low = bound("min", _EXTERNAL_TEMPERATURE_MIN_C)
    high = bound("max", _EXTERNAL_TEMPERATURE_MAX_C)
    return max(low, min(high, published(round(celsius, 1))))


async def maybe_set_external_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Mirror the room temperature into the SRTS-A01 external temperature input.

    Looks for the number entity matching ``external_temperature_input`` on
    the TRV's device and writes the temperature, rounded to a tenth of a
    degree Celsius, converted into the unit the input publishes and held
    inside the range it publishes. The sensor selector is brought in line
    with the control mode first, because a device regulating on its own
    sensor never reads the input and may discard a value written to it.
    Under ``SENSOR_FALLBACK`` it stays on ``internal`` and the value is
    written all the same, ready for the return to ``OPTIMAL``.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying ``hass``, the TRV registry
        and the context the service calls are made under.
    entity_id : str
        The TRV whose device carries the input.
    temperature : float
        The room temperature to mirror into the device, in degrees Celsius.

    Returns
    -------
    bool
        True when the input was written, False when the device names no
        such input, the value is not a number, or the device refused the
        write.
    """
    try:
        target = _input_number(self, entity_id)
        if target is None:
            return False

        try:
            celsius = float(temperature)
        except TypeError, ValueError:
            _LOGGER.debug(
                "better_thermostat %s: SRTS-A01 maybe_set_external_temperature got non-float: %s",
                self.device_name,
                temperature,
            )
            return False

        _watch_selector(self, entity_id)

        # The selector goes first: a device on its internal sensor discards
        # the input. It takes the switch seconds after the call returns, so
        # the value written here can still be dropped; the watch writes the
        # room temperature again once the device confirms the switch.
        await _keep_selector(self, entity_id)

        await _write_input(self, entity_id, target, celsius)
        return True
    except (
        HomeAssistantError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ) as ex:
        # The device did not take the value: it is asleep, out of reach, its
        # integration is reloading, or it declares a narrower range than the
        # clamp above. Reporting the refused write as a declined one leaves
        # the caller free to serve the remaining TRVs and to control on the
        # new reading; the next write retries.
        _LOGGER.warning(
            "better_thermostat %s: SRTS-A01 external temperature write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    initial_tweak = staticmethod(initial_tweak)
    maybe_set_external_temperature = staticmethod(maybe_set_external_temperature)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_EXTERNAL_TEMPERATURE_QUIRK: ExternalTemperatureQuirk = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
