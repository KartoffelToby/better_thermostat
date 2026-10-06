"""Delegate adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import math
import time
from typing import Any, Final

from homeassistant.helpers.importlib import async_import_module
from homeassistant.util import dt as dt_util

from custom_components.better_thermostat.utils.helpers import (
    round_by_step,
    sibling_disabled_at_write,
)

from ..utils.retry import async_retry

_LOGGER = logging.getLogger(__name__)


async def load_adapter(self, integration, entity_id):
    """Load the adapter module that speaks to one integration.

    An integration without an adapter module of its own is served by the
    generic adapter. The import error that leads there is logged with its
    traceback: a broken adapter module reads exactly like an unsupported
    ecosystem from the outside, and only the traceback tells them apart.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance, or the config flow
        standing in for it
    integration : str
        Name of the integration owning the TRV
    entity_id : str
        Entity ID of the TRV the adapter is loaded for

    Returns
    -------
    ModuleType
        The adapter module, which is also stored on ``self.adapter``
    """
    if integration == "generic_thermostat":
        integration = "generic"

    try:
        self.adapter = await async_import_module(
            self.hass, "custom_components.better_thermostat.adapters." + integration
        )
        _LOGGER.debug(
            "better_thermostat %s: uses adapter %s for trv %s",
            self.device_name,
            integration,
            entity_id,
        )
    except Exception:
        _LOGGER.debug(
            "better_thermostat %s: adapter %s could not be imported for trv %s",
            self.device_name,
            integration,
            entity_id,
            exc_info=True,
        )
        self.adapter = await async_import_module(
            self.hass, "custom_components.better_thermostat.adapters.generic"
        )
        _LOGGER.info(
            "better_thermostat %s: integration: %s isn't native supported, feel free to open an issue, fallback adapter %s",
            self.device_name,
            integration,
            "generic",
        )

    return self.adapter


async def init(self, entity_id):
    """Init adapter.

    Transient unavailability is handled inside the adapter's
    ``wait_for_calibration_entity_or_timeout`` (6 × 5 s polls). The call
    is invoked under a 30 s outer budget in ``_initialize_trvs``.
    """
    return await self.real_trvs[entity_id].adapter.init(self, entity_id)


@async_retry(retries=5)
async def get_info(self, entity_id):
    """Get info."""
    return await self.real_trvs[entity_id].adapter.get_info(self, entity_id)


@async_retry(retries=5)
async def get_current_offset(self, entity_id):
    """Get current offset."""
    return await self.real_trvs[entity_id].adapter.get_current_offset(self, entity_id)


@async_retry(retries=5)
async def get_offset_step(self, entity_id):
    """Get offset steps."""
    return await self.real_trvs[entity_id].adapter.get_offset_step(self, entity_id)


@async_retry(retries=5)
async def get_min_offset(self, entity_id):
    """Get min offset."""
    return await self.real_trvs[entity_id].adapter.get_min_offset(self, entity_id)


@async_retry(retries=5)
async def get_max_offset(self, entity_id):
    """Get max offset."""
    return await self.real_trvs[entity_id].adapter.get_max_offset(self, entity_id)


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature.

    Round to device step if known and clamp to min/max before delegating.
    The TRV's recorded setpoint follows the value that actually goes out.

    A target that is not a number is refused rather than replaced by a
    stand-in. A stand-in is indistinguishable from a setpoint the user
    asked for: a device that reports a range would receive its lower
    bound, and a device that reports none would receive the stand-in
    itself. NaN and the infinities are refused on the same grounds:
    ``float()`` takes them, rounding carries them through, and the clamp
    turns them into a bound — a NaN target would reach the device as its
    maximum setpoint.
    """
    # Normalize input to float early
    try:
        t = float(temperature)
    except TypeError, ValueError:
        t = None
    if t is None or not math.isfinite(t):
        _LOGGER.error(
            "better_thermostat %s: target temperature %r for %s is not a number, "
            "nothing was written",
            getattr(self, "device_name", "unknown"),
            temperature,
            entity_id,
        )
        return None

    # Initialize step with default value
    step = 0.5
    try:
        # Step precedence: per-TRV > global config > default 0.5. Both sources
        # hold a Celsius step, matching the Celsius temperature being rounded;
        # the device's raw attribute carries the device's unit and is therefore
        # not a candidate here.
        trv = self.real_trvs.get(entity_id)
        per_trv_step = trv.target_temp_step if trv is not None else None
        global_cfg_step = getattr(self, "bt_target_temp_step", None)
        if global_cfg_step in (0, 0.0):
            global_cfg_step = None
        step = per_trv_step or global_cfg_step or 0.5
        rounded = round_by_step(float(t), float(step))
    except TypeError, ValueError, OverflowError:
        rounded = float(t)

    # Clamp to device min/max if available
    trv = self.real_trvs.get(entity_id)
    t_min_raw = trv.min_temp if trv is not None else None
    t_max_raw = trv.max_temp if trv is not None else None
    t_min = None
    t_max = None
    try:
        if t_min_raw is not None:
            t_min = float(t_min_raw)
        if t_max_raw is not None:
            t_max = float(t_max_raw)
    except TypeError, ValueError:
        t_min = None
        t_max = None
    if isinstance(t_min, (int, float)) and isinstance(t_max, (int, float)):
        low = float(t_min)
        high = float(t_max)
        rv = float(rounded) if isinstance(rounded, (int, float)) else float(t)
        if rv < low:
            rounded = low
        elif rv > high:
            rounded = high
        else:
            rounded = rv

    if rounded != t:
        _LOGGER.debug(
            "better_thermostat %s: delegate.set_temperature rounded %s -> %s (step=%s)",
            getattr(self, "device_name", "unknown"),
            t,
            rounded,
            step,
        )
    # The recorded setpoint is what the TRV event handler compares an inbound
    # report against to tell BT's own write apart from someone turning the
    # knob. The state change this write causes can be handled while the
    # service call is still in flight, so the value is recorded before it goes
    # out: recorded afterwards, the device's echo would arrive while the
    # previous value still stood and would be adopted as a user setpoint.
    # ``set_offset`` records after its write for the opposite reason: its
    # record says a calibration command is in flight, which a write that never
    # went out must not claim.
    self.real_trvs[entity_id].last_temperature = rounded

    return await _write_on_channel(
        self,
        entity_id,
        "temperature",
        f"setpoint {rounded}",
        self.real_trvs[entity_id].adapter.set_temperature,
        rounded,
    )


async def set_hvac_mode(self, entity_id, hvac_mode) -> bool:
    """Set a new hvac mode on the TRV.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity ID of the TRV to write to
    hvac_mode : str
        The mode to switch the TRV to

    Returns
    -------
    bool
        True when the mode went out, False when every attempt raised
    """
    write = self.real_trvs[entity_id].adapter.set_hvac_mode
    try:
        await _write_on_channel(
            self, entity_id, "hvac_mode", f"hvac mode {hvac_mode}", write, hvac_mode
        )
    except Exception:  # noqa: BLE001 - _write_on_channel logged the failure
        return False
    return True


# How often an outage that goes on is named again in the log.
OUTAGE_REPORT_INTERVAL_S: Final = 3600.0


@dataclass
class WriteOutage:
    """A write channel of one TRV whose last write spent every attempt.

    Attributes
    ----------
    since : datetime
        When the attempts were spent.
    reported_at : float
        Monotonic time the outage was last named at WARNING.
    """

    since: datetime
    reported_at: float


async def _write_on_channel(
    self,
    entity_id: str,
    channel: str,
    what: str,
    write: Callable[..., Awaitable[Any]],
    value: Any,
) -> Any:
    """Put one value on one write channel of the TRV and answer the write's answer.

    The write runs under the room's control lock, so every attempt it
    makes is time the other TRVs of the room wait. A channel whose writes
    have been going through gets the retry chain, which covers a dropped
    message within the cycle. A channel whose last write spent that chain
    and still raised gets one attempt: the device is out of reach, the next
    cycle asks again anyway, and the chain would cost the room its backoff
    on every cycle. Only a write that goes through ends the outage. A TRV
    that keeps dropping out and coming back, or that takes a write while
    raising, keeps costing one attempt per write rather than a chain per
    return. The outage is named at WARNING when it begins, once an hour
    while it lasts, and at INFO when it ends.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity ID of the TRV to write to
    channel : str
        Name of the write channel, the key its reachability is kept under
    what : str
        The command as the log names it
    write : Callable
        The adapter or quirk write, called as ``write(self, entity_id, value)``
    value : Any
        The value to write

    Returns
    -------
    Any
        What the write answered

    Raises
    ------
    Exception
        The write's own exception, once the attempts it gets are spent
    """
    trv = self.real_trvs.get(entity_id)
    found = getattr(trv, "unreachable_write_channels", None)
    outages: dict[str, WriteOutage] = found if isinstance(found, dict) else {}
    device_name = getattr(self, "device_name", "unknown")
    outage = outages.get(channel)

    async def write_to_device(host, target, payload):
        return await write(host, target, payload)

    attempt = (
        write_to_device
        if outage is not None
        else async_retry(retries=5, identifier=f"{device_name} {channel}")(
            write_to_device
        )
    )
    try:
        answer = await attempt(self, entity_id, value)
    except Exception:
        now = time.monotonic()
        if outage is None:
            outages[channel] = WriteOutage(since=dt_util.utcnow(), reported_at=now)
            _LOGGER.warning(
                "better_thermostat %s: %s for %s could not be written; each "
                "following cycle tries it once until a write goes through",
                device_name,
                what,
                entity_id,
            )
        elif now - outage.reported_at >= OUTAGE_REPORT_INTERVAL_S:
            outage.reported_at = now
            _LOGGER.warning(
                "better_thermostat %s: %s for %s is still out of reach, as it "
                "has been since %s",
                device_name,
                what,
                entity_id,
                outage.since.isoformat(timespec="seconds"),
            )
        else:
            _LOGGER.debug(
                "better_thermostat %s: %s for %s is still out of reach",
                device_name,
                what,
                entity_id,
                exc_info=True,
            )
        raise
    if outage is not None:
        del outages[channel]
        _LOGGER.info(
            "better_thermostat %s: %s for %s went through, the channel is back "
            "in reach",
            device_name,
            what,
            entity_id,
        )
    return answer


def _adopted_helper_disabled(self, entity_id: str, attribute: str, role: str) -> bool:
    helper = getattr(self.real_trvs[entity_id], attribute, None)
    return helper is not None and sibling_disabled_at_write(
        self, entity_id, helper, role
    )


def calibration_entity_disabled(self, entity_id: str) -> bool:
    """Whether the TRV's adopted calibration entity is disabled right now.

    A disabled entity stays disabled until the user acts, so the offset
    channel is absent rather than failing: a caller does not pursue the
    offset, and does not schedule a retry for it, while this holds.
    """
    return _adopted_helper_disabled(
        self, entity_id, "local_temperature_calibration_entity", "local calibration"
    )


def valve_entity_disabled(self, entity_id: str) -> bool:
    """Whether the TRV's adopted valve entity is disabled right now.

    A disabled entity stays disabled until the user acts, so the valve
    channel is absent rather than failing: a caller does not pursue the
    valve position, and does not schedule a retry for it, while this holds.
    """
    return _adopted_helper_disabled(
        self, entity_id, "valve_position_entity", "valve position"
    )


async def set_offset(self, entity_id, offset) -> bool:
    """Set new target offset and record the value that was asked for.

    An adapter answers ``True`` once the offset write went out and
    ``False`` when the device has no offset channel to write to. Only
    ``True`` counts as a command in flight: it is what records
    ``last_calibration_requested`` and what tells the caller to arm the
    confirmation watchdog. The written offset itself is not a usable
    answer, because the legitimate value 0.0 reads the same as a device
    that wrote nothing.

    ``last_calibration_requested`` is written on a write only: a
    swallowed failure would otherwise look like a command in flight.

    A calibration entity disabled in Home Assistant since it was adopted
    is not written to: the call would be dropped, and answering ``True``
    would leave the caller waiting on an offset that never arrives.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity ID of the TRV to write to
    offset : float
        The offset asked for, before the adapter's own range clamp

    Returns
    -------
    bool
        True when the adapter put the offset on the wire, False when the
        device has no offset channel, its calibration entity is disabled,
        or every attempt raised
    """
    if calibration_entity_disabled(self, entity_id):
        return False

    write = self.real_trvs[entity_id].adapter.set_offset
    try:
        wrote = await _write_on_channel(
            self, entity_id, "offset", "calibration offset", write, offset
        )
    except Exception:  # noqa: BLE001 - _write_on_channel logged the failure
        return False
    if wrote is not True:
        _LOGGER.debug(
            "better_thermostat %s: %s has no calibration offset channel, "
            "nothing was written",
            getattr(self, "device_name", "unknown"),
            entity_id,
        )
        return False
    self.real_trvs[entity_id].last_calibration_requested = float(offset)
    return True


def _valve_channels(
    self, entity_id: str
) -> list[tuple[str, Callable[..., Awaitable[bool | None]], bool]]:
    """List the channels a valve position can go out through, in the order tried."""
    trv_state = self.real_trvs.get(entity_id)

    # The answer says a command went out, so the adapter's channel is tied to
    # the adapter's own declaration rather than to the discovered entity: an
    # ecosystem that declares no valve channel writes nothing, and reporting
    # the discovery as a completed write would tell the caller a position was
    # taken that the device never saw. An adapter without a declaration falls
    # back to the discovered surface, as elsewhere.
    declared = getattr(getattr(trv_state, "adapter", None), "CAPABILITIES", None)
    adapter_writes_valve = declared is None or declared.valve_write
    # An adapter whose valve channel is an ecosystem service call has no
    # helper entity to discover. `Trv.capabilities` already reads the flag
    # that way, so requiring an entity here would report a TRV as valve
    # capable and then never write to it.
    adapter_needs_valve_entity = declared is None or declared.valve_needs_entity
    valve_entity = getattr(trv_state, "valve_position_entity", None)
    valve_writable = getattr(trv_state, "valve_position_writable", None)
    adapter_write = getattr(getattr(trv_state, "adapter", None), "set_valve", None)

    # Each channel carries whether its own answer decides the outcome: a quirk
    # reports whether it took the position, while an adapter call that returns
    # is the write. The adapter's channel exists once a helper entity was
    # discovered and is known to be writable, or once the adapter declares it
    # needs no such entity because its valve channel is a service call.
    #
    # A quirk that can tell whether its device offers a valve to write to
    # answers ``has_valve_channel``; one that cannot is taken at its word
    # that ``override_set_valve`` is a channel.
    channels: list[tuple[str, Callable[..., Awaitable[bool | None]], bool]] = []
    model_quirks = getattr(trv_state, "model_quirks", None)
    quirk_write = getattr(model_quirks, "override_set_valve", None)
    quirk_has_channel = getattr(model_quirks, "has_valve_channel", None)
    if quirk_write is not None and (
        quirk_has_channel is None or quirk_has_channel(self, entity_id)
    ):
        channels.append(("override", quirk_write, True))
    # A valve entity disabled in Home Assistant since it was adopted drops
    # every write, so it is no channel until it is enabled again.
    if (
        adapter_write is not None
        and adapter_writes_valve
        and (
            not adapter_needs_valve_entity
            or (
                valve_entity
                and valve_writable is True
                and not valve_entity_disabled(self, entity_id)
            )
        )
    ):
        channels.append(("adapter", adapter_write, False))
    return channels


def valve_channel_available(self, entity_id: str) -> bool:
    """Whether any channel exists to write the TRV's valve position through.

    Without one, the valve is out of reach for a reason that lasts until
    the user acts (no valve entity, or only a disabled one), so a caller
    does not pursue the valve position, and does not schedule a retry for
    it, while this holds. A channel that exists but fails a write is a
    different matter and is retried.
    """
    return bool(_valve_channels(self, entity_id))


async def set_valve(self, entity_id, valve) -> bool:
    """Set a new valve position and record the value that went out.

    A model quirk's ``override_set_valve`` owns the valve channel where
    one exists and is asked first; a quirk that answers it did not take
    the position falls through to the adapter's own channel. Whichever
    wrote records ``last_valve_percent`` and ``last_valve_method``.

    A device with no valve channel is not a failure and is answered
    ``False`` without a single attempt: no number of attempts turns a
    missing channel into one. A write that raises is an infrastructure
    failure and is retried as :func:`_write_on_channel` describes; a
    channel whose attempts are spent leaves the position to the next
    channel. Only once no channel took it is it answered ``False``, which
    leaves the caller free to re-derive the position on its next cycle.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity ID of the TRV to write to
    valve : int
        The valve position to take, in percent

    Returns
    -------
    bool
        True when a position was put on the wire, False when the device
        has no valve channel or every attempt raised
    """
    try:
        target_pct = int(valve)
    except TypeError, ValueError, OverflowError:
        # `int()` refuses the infinities with OverflowError rather than
        # ValueError, and a position that cannot be converted is not one to
        # raise on: the caller re-derives it on the next cycle either way.
        _LOGGER.error(
            "better_thermostat %s: valve position %r for %s is not a number, "
            "nothing was written",
            getattr(self, "device_name", "unknown"),
            valve,
            entity_id,
        )
        return False

    trv_state = self.real_trvs.get(entity_id)
    channels = _valve_channels(self, entity_id)

    # A channel that raised leaves the position to the next channel, as one
    # that declined it does.
    for method, write, answer_decides in channels:
        try:
            answer = await _write_on_channel(
                self,
                entity_id,
                f"valve {method}",
                f"valve position {target_pct}% through the {method} channel",
                write,
                target_pct,
            )
        except Exception:  # noqa: BLE001 - _write_on_channel logged the failure
            continue
        if answer_decides and not answer:
            continue
        trv_state.last_valve_percent = target_pct
        trv_state.last_valve_method = method
        return True
    return False
