"""Delegate adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import logging
import time
from typing import Any, Final

from homeassistant.core import State
from homeassistant.helpers.importlib import async_import_module
from homeassistant.util import dt as dt_util

from custom_components.better_thermostat.utils.helpers import (
    convert_to_float_celsius,
    round_by_step,
    sibling_disabled_at_write,
    state_temperature_unit,
)

from ..utils.retry import async_retry

_LOGGER = logging.getLogger(__name__)


async def load_adapter(self, integration, entity_id, get_name=False):
    """Load adapter."""
    if get_name:
        self.device_name = "-"

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
        self.adapter = await async_import_module(
            self.hass, "custom_components.better_thermostat.adapters.generic"
        )
        _LOGGER.info(
            "better_thermostat %s: integration: %s isn't native supported, feel free to open an issue, fallback adapter %s",
            self.device_name,
            integration,
            "generic",
        )

    if get_name:
        return integration
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
    Also updates last_temperature to the (potentially) rounded value for consistency.
    """
    # Normalize input to float early
    try:
        t = float(temperature)
    except TypeError, ValueError:
        t = 0.0

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
    except Exception:
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
    # Keep last_temperature in sync with the actually sent value
    try:
        self.real_trvs[entity_id].last_temperature = rounded
    except Exception as e:
        _LOGGER.warning(
            "better_thermostat %s: Failed to update last_temperature for entity_id %s: %s",
            getattr(self, "device_name", "unknown"),
            entity_id,
            e,
        )

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
    try:
        await _write_on_channel(
            self,
            entity_id,
            "hvac_mode",
            f"hvac mode {hvac_mode}",
            self.real_trvs[entity_id].adapter.set_hvac_mode,
            hvac_mode,
        )
    except Exception:
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
    attempted : Any
        The value that write carried.
    reported_at : float
        Monotonic time the outage was last named at WARNING.
    """

    since: datetime
    attempted: Any
    reported_at: float


def _outage_is_over(self, entity_id: str, channel: str, outage: WriteOutage) -> bool:
    """Whether what the TRV reports shows the recorded outage has ended.

    A TRV whose state changed after the failure is talking again: it came
    back from being unavailable, or it took a command. A TRV that reports
    the mode or setpoint whose write raised took that write, the error
    notwithstanding.
    """
    state = self.hass.states.get(entity_id)
    if not isinstance(state, State):
        return False
    if state.last_changed > outage.since:
        return True
    if channel == "hvac_mode":
        return state.state == str(outage.attempted)
    if channel == "temperature":
        reported = convert_to_float_celsius(
            state.attributes.get("temperature"),
            getattr(self, "device_name", "unknown"),
            "write outage",
            state_temperature_unit(
                state.attributes, self.hass.config.units.temperature_unit
            ),
        )
        return reported is not None and abs(reported - outage.attempted) < 0.05
    return False


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
    on every cycle. The outage ends with a write that goes through, or once
    the TRV's own reports show it is over (see :func:`_outage_is_over`).
    It is named at WARNING when it begins, once an hour while it lasts,
    and at INFO when it ends.

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
    if outage is not None and _outage_is_over(self, entity_id, channel, outage):
        del outages[channel]
        outage = None

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
            outages[channel] = WriteOutage(
                since=dt_util.utcnow(), attempted=value, reported_at=now
            )
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


async def set_offset(self, entity_id, offset) -> bool:
    """Set new target offset.

    An adapter answers ``True`` once the offset write went out and ``False``
    when the device has no offset channel to write to. Only ``True`` counts
    as a command in flight: it is what records the requested value and what
    tells the caller to arm the confirmation watchdog. The written offset
    itself is not a usable answer, because the legitimate value 0.0 reads
    the same as a device that wrote nothing.

    The requested value is recorded on a write only, so a command that never
    left the house neither counts as issued nor suppresses the retry on the
    next control cycle.

    A calibration entity disabled in Home Assistant since it was adopted
    is not written to: the call would be dropped, and answering ``True``
    would leave the caller waiting on an offset that never arrives.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance.
    entity_id : str
        Entity id of the TRV to write the offset to.
    offset : float
        Calibration offset to request, before the adapter's own clamp to the
        device's declared offset range.

    Returns
    -------
    bool
        True when the adapter put the offset on the wire, False when the
        device has no offset channel, its calibration entity is disabled,
        or every attempt raised.
    """
    calibration_entity = self.real_trvs[entity_id].local_temperature_calibration_entity
    if calibration_entity is not None and sibling_disabled_at_write(
        self, entity_id, calibration_entity, "local calibration"
    ):
        return False

    try:
        wrote = await _write_on_channel(
            self,
            entity_id,
            "offset",
            "calibration offset",
            self.real_trvs[entity_id].adapter.set_offset,
            offset,
        )
    except Exception:
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


async def set_valve(self, entity_id, valve) -> bool:
    """Set a new valve position and record the value that went out.

    A model quirk's ``override_set_valve`` owns the valve channel where
    one exists and is asked first; a quirk that answers it did not take
    the position falls through to the adapter's helper entity, which is
    written only when it is known to be writable and is still enabled in
    Home Assistant. Whichever wrote records ``last_valve_percent`` and
    ``last_valve_method``.

    A device with no valve channel is not a failure and is answered
    ``False`` without a single attempt. A write that raises is an
    infrastructure failure and is retried as :func:`_write_on_channel`
    describes; a channel whose attempts are spent leaves the position to the
    next channel. Only once no channel took it is it answered ``False``,
    which leaves the caller free to re-derive the position on its next
    cycle.

    Returns True when a position was put on the wire, False otherwise.
    """
    try:
        target_pct = int(valve)
    except Exception:
        target_pct = valve
    trv_state = self.real_trvs.get(entity_id)

    # Each channel carries whether its own answer decides the outcome: a quirk
    # reports whether it took the position, while an adapter call that returns
    # is the write.
    channels = []
    _override_set_valve = getattr(
        trv_state.model_quirks if trv_state is not None else None,
        "override_set_valve",
        None,
    )
    if _override_set_valve is not None:
        channels.append(("override", _override_set_valve, True))
    valve_entity = trv_state.valve_position_entity if trv_state is not None else None
    valve_writable = (
        trv_state.valve_position_writable if trv_state is not None else None
    )
    # Only write to a helper entity when we know it's writable and it is
    # still enabled.
    if (
        valve_entity
        and valve_writable is True
        and not sibling_disabled_at_write(
            self, entity_id, valve_entity, "valve position"
        )
    ):
        channels.append(("adapter", trv_state.adapter.set_valve, False))

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
        except Exception:
            continue
        if answer_decides and not answer:
            continue
        try:
            trv_state.last_valve_percent = int(target_pct)
            trv_state.last_valve_method = method
        except Exception as exc:
            _LOGGER.debug(
                "better_thermostat %s: Failed to record last_valve_percent/method for %s: %s",
                getattr(self, "device_name", "unknown"),
                entity_id,
                exc,
            )
        return True
    return False
