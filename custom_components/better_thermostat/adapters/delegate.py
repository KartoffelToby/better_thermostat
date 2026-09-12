"""Delegate adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
import math

from homeassistant.helpers.importlib import async_import_module

from custom_components.better_thermostat.utils.helpers import round_by_step

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


@async_retry(retries=5)
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
    # The recorded setpoint is what the TRV event handler compares an inbound
    # report against to tell BT's own write apart from someone turning the
    # knob. The state change this write causes can be handled while the
    # service call is still in flight, so the value is recorded before it goes
    # out: recorded afterwards, the device's echo would arrive while the
    # previous value still stood and would be adopted as a user setpoint.
    # ``set_offset`` records after its write for the opposite reason: its
    # record says a calibration command is in flight, which a write that never
    # went out must not claim.
    try:
        self.real_trvs[entity_id].last_temperature = rounded
    except Exception as e:
        _LOGGER.warning(
            "better_thermostat %s: Failed to update last_temperature for entity_id %s: %s",
            getattr(self, "device_name", "unknown"),
            entity_id,
            e,
        )

    return await self.real_trvs[entity_id].adapter.set_temperature(
        self, entity_id, rounded
    )


@async_retry(retries=5)
async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    return await self.real_trvs[entity_id].adapter.set_hvac_mode(
        self, entity_id, hvac_mode
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
        device has no offset channel or every retry raised
    """

    @async_retry(retries=5)
    async def inner():
        return await self.real_trvs[entity_id].adapter.set_offset(
            self, entity_id, offset
        )

    try:
        wrote = await inner()
    except Exception:
        _LOGGER.warning(
            "better_thermostat %s: set_local_temperature_calibration for %s failed; "
            "will retry on the next cycle",
            getattr(self, "device_name", "unknown"),
            entity_id,
        )
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
    the position falls through to the adapter's own channel. Whichever
    wrote records ``last_valve_percent`` and ``last_valve_method``.

    A device with no valve channel is not a failure and is answered
    ``False`` without a single attempt: no number of attempts turns a
    missing channel into one. A write that raises is an infrastructure
    failure and is retried; only once the attempts are spent is it
    reported and answered ``False``, which leaves the caller free to
    re-derive the position on its next cycle.

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
    channels = []
    quirk_write = getattr(
        getattr(trv_state, "model_quirks", None), "override_set_valve", None
    )
    if quirk_write is not None:
        channels.append(("override", quirk_write, True))
    if (
        adapter_write is not None
        and adapter_writes_valve
        and (
            (valve_entity and valve_writable is True) or not adapter_needs_valve_entity
        )
    ):
        channels.append(("adapter", adapter_write, False))

    @async_retry(
        retries=5,
        identifier=f"{getattr(self, 'device_name', 'unknown')} valve {entity_id}",
    )
    async def write_position(write: Callable[..., Awaitable[bool | None]]):
        return await write(self, entity_id, target_pct)

    for method, write, answer_decides in channels:
        try:
            answer = await write_position(write)
        except Exception:
            _LOGGER.warning(
                "better_thermostat %s: valve position %s%% for %s could not be "
                "written; will retry on the next cycle",
                getattr(self, "device_name", "unknown"),
                target_pct,
                entity_id,
            )
            return False
        if answer_decides and not answer:
            continue
        trv_state.last_valve_percent = target_pct
        trv_state.last_valve_method = method
        return True
    return False
