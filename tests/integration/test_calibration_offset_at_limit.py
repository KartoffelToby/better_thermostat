"""A TRV that ignores its calibration offset is reported once the offset is stuck.

The simulated head keeps reporting the same reading whatever offset it holds,
the way some Matter heads do. Offset calibration adds the gap to the room on
every cycle, so the offset climbs to the top of the head's range and stays
there. Once it has stayed there for the whole window, the TRV carries a repair
issue. The issue goes as soon as the offset leaves the limit, and with the
calibration type that put it there.
"""

import contextlib
from dataclasses import replace
from datetime import timedelta
import math
from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    HVACMode,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import async_fire_time_changed

from custom_components.better_thermostat.core.offset_limit import (
    CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS,
)
from custom_components.better_thermostat.utils.const import CalibrationOutput
from custom_components.better_thermostat.utils.helpers import entry_issue_id

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    WRITE_BUDGET,
    click_through_the_options,
    make_entry,
    set_room_sensor,
    wait_for,
    wait_for_startup,
)
from .device_profiles import MQTT_OFFSET_TRV, TRV_ID

NARROW_OFFSET_TRV = replace(
    MQTT_OFFSET_TRV,
    name="mqtt_offset_trv_with_a_narrow_offset_range",
    offset_range=(-5.0, 5.0, 0.5),
)
"""A Zigbee2MQTT head whose offset runs from -5 to 5 K in half-Kelvin steps.

The range is narrower than the safety hull's fallback range, so a limit read
off anything but the head's own range is caught.
"""

# The head's own reading, which no offset moves.
_HEAD_READING = MQTT_OFFSET_TRV.current_temperature
# Six degrees above the head's reading: every cycle asks for six more Kelvin
# of offset than the head holds, more than its whole range.
_ROOM_TEMPERATURE = 25.0
_OFFSET_LIMIT = 5.0
# Minutes of virtual time between two control ticks.
_TICK_MINUTES = 6
# Ticks a test may fire before it gives up on a condition.
_MAX_TICKS = 40


class _VirtualTime:
    """A clock the test moves on, together with Home Assistant's timers.

    The origin is the real monotonic time rounded up to a whole second, so
    every reading is the origin plus whole ticks and sums of them are exact.
    With a fractional origin, the sum that reaches the end of the window can
    round to a hair below it in the test and onto it in the integration.
    """

    def __init__(self, real_clock) -> None:
        self.t = 0.0
        self._real = real_clock
        self.origin = float(math.ceil(real_clock.monotonic()))

    def monotonic(self) -> float:
        return self.origin + self.t

    def now(self):
        return self._real.now() + timedelta(seconds=self.t)

    def utcnow(self):
        return self._real.utcnow() + timedelta(seconds=self.t)


class _Ticks:
    """Fire the control ticks, moving the thermostat's clock with them.

    A calibration mode without a periodic recompute runs a cycle only when
    something it reads changes. For such a mode ``room_temperature`` is set,
    and every tick moves the room sensor by half a degree and back.
    """

    def __init__(self, hass, bt, clock: _VirtualTime) -> None:
        self.hass = hass
        self.bt = bt
        self.clock = clock
        self.start = dt_util.utcnow()
        self.count = 0
        self.room_temperature: float | None = None

    async def fire(self) -> None:
        """Move on one tick and wait for the control cycle it asks for."""
        self.count += 1
        self.clock.t = self.count * _TICK_MINUTES * 60.0
        if self.room_temperature is not None:
            set_room_sensor(self.hass, self.room_temperature + 0.5 * (self.count % 2))
        async_fire_time_changed(
            self.hass, self.start + timedelta(minutes=_TICK_MINUTES * self.count)
        )
        assert await wait_for(
            self.hass,
            lambda: (
                self.bt.kernel_state.last_control_monotonic == self.clock.monotonic()
            ),
        )
        await self.hass.async_block_till_done()

    async def until(self, predicate) -> bool:
        while not predicate() and self.count < _MAX_TICKS:
            await self.fire()
        return predicate()


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, entry_issue_id(entry.entry_id, "calibration_offset_at_limit", TRV_ID)
    )


async def _set_room_mode(hass, hvac_mode: HVACMode) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {"entity_id": BT_ENTITY, "hvac_mode": hvac_mode},
        blocking=True,
    )
    await hass.async_block_till_done()


@contextlib.contextmanager
def _head_applying_its_offset(fake_trv, raw_reading: float):
    """Make the head report its raw reading plus every offset written to it."""
    offset_number = fake_trv.offset_number
    assert offset_number is not None
    write = type(offset_number).async_set_native_value

    async def apply(number, value: float) -> None:
        await write(number, value)
        fake_trv._attr_native_current_temperature = raw_reading + value
        fake_trv.async_write_ha_state()

    with patch.object(type(offset_number), "async_set_native_value", apply):
        yield


@contextlib.asynccontextmanager
async def _room_with_an_ignoring_head(
    hass, fake_trv, *, protect_overheating=False, calibration_mode=None
):
    """Start a room on the head, which by default ignores its offset."""
    set_room_sensor(hass, _ROOM_TEMPERATURE)
    entry = make_entry(fake_trv.profile)
    entry.add_to_hass(hass)
    advanced = {"protect_overheating": protect_overheating}
    if calibration_mode is not None:
        advanced["calibration_mode"] = calibration_mode
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "thermostat": [
                {**trv, "advanced": {**trv["advanced"], **advanced}}
                for trv in entry.data["thermostat"]
            ],
        },
    )
    with patch(WRITE_BUDGET, 0.0):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        bt = await wait_for_startup(hass, entry)
        await hass.async_block_till_done()
        clock = _VirtualTime(bt.clock)
        bt.clock = clock
        yield bt, entry, _Ticks(hass, bt, clock)


async def _until_the_offset_rests_at_the_limit(bt, ticks):
    """Fire ticks until a run at the limit starts, and return that run."""
    trv = bt.real_trvs[TRV_ID]
    assert await ticks.until(lambda: trv.calibration_offset_at_limit is not None)
    run = trv.calibration_offset_at_limit
    assert run is not None
    assert run.limit == pytest.approx(_OFFSET_LIMIT)
    return run


@pytest.mark.parametrize("fake_trv", [NARROW_OFFSET_TRV], indirect=True)
async def test_an_offset_the_head_ignores_is_reported_after_the_window(hass, fake_trv):
    """The issue appears once the offset has stayed at the limit for the window.

    It does not appear while the window is still running. Once the head
    reports a reading that follows its offset and the gap to the room is
    within the range, the next cycle asks for an offset off the limit and
    the issue goes.
    """
    offset_number = fake_trv.offset_number
    assert offset_number is not None
    async with _room_with_an_ignoring_head(hass, fake_trv) as (bt, entry, ticks):
        trv = bt.real_trvs[TRV_ID]
        run = await _until_the_offset_rests_at_the_limit(bt, ticks)
        assert offset_number.set_value_calls[-1] == pytest.approx(_OFFSET_LIMIT)
        reported_from = run.since_monotonic + CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS

        while (
            bt.clock.monotonic() + _TICK_MINUTES * 60.0 < reported_from
            and ticks.count < _MAX_TICKS
        ):
            await ticks.fire()
            assert _issue(hass, entry) is None
        assert trv.calibration_offset_at_limit is not None
        assert trv.calibration_offset_at_limit.since_monotonic == run.since_monotonic

        await ticks.fire()
        assert bt.clock.monotonic() >= reported_from
        issue = _issue(hass, entry)
        assert issue is not None
        assert issue.translation_key == "calibration_offset_at_limit"
        assert issue.translation_placeholders is not None
        assert issue.translation_placeholders["entity"] == TRV_ID
        assert issue.learn_more_url == (
            "https://better-thermostat.org/faq/calibration-offset-at-limit"
        )

        fake_trv._attr_native_current_temperature = _HEAD_READING + _OFFSET_LIMIT
        fake_trv.async_write_ha_state()
        set_room_sensor(hass, 23.0)
        await hass.async_block_till_done()
        assert await ticks.until(lambda: _issue(hass, entry) is None)
        assert not trv.calibration_offset_at_limit_reported
        assert trv.calibration_offset_at_limit is None
        assert offset_number.set_value_calls[-1] < _OFFSET_LIMIT - 0.5


@pytest.mark.parametrize("fake_trv", [NARROW_OFFSET_TRV], indirect=True)
async def test_the_issue_goes_with_the_offset_based_calibration_type(hass, fake_trv):
    """Switching the TRV to Target Temperature Based takes the issue back."""
    async with _room_with_an_ignoring_head(hass, fake_trv) as (bt, entry, ticks):
        assert await ticks.until(lambda: _issue(hass, entry) is not None)
        with patch(WRITE_BUDGET, 0.0):
            await click_through_the_options(
                hass, entry, calibration="target_temp_based"
            )
        assert await wait_for(hass, lambda: _issue(hass, entry) is None)
        reloaded = await wait_for_startup(hass, entry)
        assert reloaded is not bt
        reloaded_trv = reloaded.real_trvs[TRV_ID]
        assert reloaded_trv.calibration is CalibrationOutput.TARGET_TEMP_BASED
        assert not reloaded_trv.calibration_offset_at_limit_reported
        assert _issue(hass, entry) is None


@pytest.mark.parametrize("fake_trv", [NARROW_OFFSET_TRV], indirect=True)
async def test_the_issue_goes_with_the_entry(hass, fake_trv):
    """Unloading the entry deletes the issue it raised."""
    async with _room_with_an_ignoring_head(hass, fake_trv) as (_bt, entry, ticks):
        assert await ticks.until(lambda: _issue(hass, entry) is not None)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert _issue(hass, entry) is None


@pytest.mark.parametrize("fake_trv", [NARROW_OFFSET_TRV], indirect=True)
async def test_an_offset_left_at_the_limit_before_the_start_gets_the_whole_window(
    hass, fake_trv
):
    """A head that starts out at its limit is not reported straight away.

    The startup writes the head's offset back to zero, and the offset climbs
    back from there: the window starts with the first cycle that finds it at
    the limit again, not with the start.
    """
    offset_number = fake_trv.offset_number
    assert offset_number is not None
    offset_number._attr_native_value = _OFFSET_LIMIT
    offset_number.async_write_ha_state()
    async with _room_with_an_ignoring_head(hass, fake_trv) as (bt, entry, ticks):
        assert 0.0 in offset_number.set_value_calls
        run = await _until_the_offset_rests_at_the_limit(bt, ticks)
        assert run.since_monotonic > ticks.clock.origin
        assert _issue(hass, entry) is None


@pytest.mark.parametrize("fake_trv", [NARROW_OFFSET_TRV], indirect=True)
async def test_a_room_switched_off_starts_the_window_again(hass, fake_trv):
    """A cycle that follows no offset breaks the run the window needs."""
    async with _room_with_an_ignoring_head(hass, fake_trv) as (bt, entry, ticks):
        trv = bt.real_trvs[TRV_ID]
        first = await _until_the_offset_rests_at_the_limit(bt, ticks)

        await _set_room_mode(hass, HVACMode.OFF)
        await ticks.fire()
        assert trv.calibration_offset_at_limit is None

        await _set_room_mode(hass, HVACMode.HEAT)
        second = await _until_the_offset_rests_at_the_limit(bt, ticks)
        assert second.since_monotonic > first.since_monotonic
        assert _issue(hass, entry) is None


@pytest.mark.parametrize("fake_trv", [NARROW_OFFSET_TRV], indirect=True)
async def test_an_ignoring_head_in_a_room_overheating_protection_closes_is_reported(
    hass, fake_trv
):
    """Overheating protection does not hide a head that ignores its offset.

    The head reads 6 K below the room, so the correction alone lies past its
    range; that the protection pushes the offset the same way changes nothing
    about the head not applying it.
    """
    async with _room_with_an_ignoring_head(
        hass, fake_trv, protect_overheating=True
    ) as (bt, entry, ticks):
        assert bt.heat_target_temperature + bt.tolerance < _ROOM_TEMPERATURE
        assert await ticks.until(lambda: _issue(hass, entry) is not None)


@pytest.mark.parametrize("fake_trv", [NARROW_OFFSET_TRV], indirect=True)
async def test_an_offset_overheating_protection_holds_at_the_limit_is_not_reported(
    hass, fake_trv
):
    """A head that applies its offset is not reported for the protection's push.

    The room idles 5 K above its target and the head reads the room. The
    protection drives the offset to the top of the range to close the valve,
    and the head's reading carries it, so the gap to the room asks for no
    correction past the range. Two windows go by without an issue.
    """
    offset_number = fake_trv.offset_number
    assert offset_number is not None
    fake_trv._attr_native_current_temperature = _ROOM_TEMPERATURE
    with _head_applying_its_offset(fake_trv, _ROOM_TEMPERATURE):
        async with _room_with_an_ignoring_head(
            hass, fake_trv, protect_overheating=True, calibration_mode="fix_calibration"
        ) as (bt, entry, ticks):
            assert bt.heat_target_temperature + bt.tolerance < _ROOM_TEMPERATURE
            ticks.room_temperature = _ROOM_TEMPERATURE
            while ticks.clock.t < 2 * CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS:
                await ticks.fire()
            assert offset_number.set_value_calls[-1] == pytest.approx(_OFFSET_LIMIT)
            assert fake_trv.native_current_temperature == pytest.approx(
                _ROOM_TEMPERATURE + _OFFSET_LIMIT
            )
            assert bt.real_trvs[TRV_ID].calibration_offset_at_limit is None
            assert _issue(hass, entry) is None


@pytest.mark.parametrize("fake_trv", [NARROW_OFFSET_TRV], indirect=True)
async def test_an_unreadable_offset_entity_leaves_a_raised_issue(hass, fake_trv):
    """An offset entity that reports nothing for a while says nothing new.

    The adapter reads such an entity as an offset of zero; that is no offset
    the head holds, so the issue stands until a readable cycle decides.
    """
    offset_number = fake_trv.offset_number
    assert offset_number is not None
    async with _room_with_an_ignoring_head(hass, fake_trv) as (bt, entry, ticks):
        trv = bt.real_trvs[TRV_ID]
        assert await ticks.until(lambda: _issue(hass, entry) is not None)

        offset_number._attr_available = False
        offset_number.async_write_ha_state()
        await hass.async_block_till_done()
        await ticks.fire()
        await ticks.fire()

        assert _issue(hass, entry) is not None
        assert trv.calibration_offset_at_limit_reported
        assert trv.calibration_offset_at_limit is None


_COLD_ROOM_TEMPERATURE = 17.0


@pytest.mark.parametrize("fake_trv", [NARROW_OFFSET_TRV], indirect=True)
async def test_a_head_heating_power_holds_at_the_limit_is_not_reported(hass, fake_trv):
    """A head that applies the offset Heating Power drives to a limit is not reported.

    The room is cold and heats. Heating Power sets the offset from the valve
    opening it wants, which on this narrow range is the lowest offset, and the
    head's reading carries it. The gap to the room asks for no correction past
    the range, so two windows go by without an issue.
    """
    offset_number = fake_trv.offset_number
    assert offset_number is not None
    fake_trv._attr_native_current_temperature = _COLD_ROOM_TEMPERATURE
    with _head_applying_its_offset(fake_trv, _COLD_ROOM_TEMPERATURE):
        async with _room_with_an_ignoring_head(
            hass, fake_trv, calibration_mode="heating_power_calibration"
        ) as (bt, entry, ticks):
            set_room_sensor(hass, _COLD_ROOM_TEMPERATURE)
            ticks.room_temperature = _COLD_ROOM_TEMPERATURE
            while ticks.clock.t < 2 * CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS:
                await ticks.fire()
            assert offset_number.set_value_calls[-1] == pytest.approx(-_OFFSET_LIMIT)
            assert bt.real_trvs[TRV_ID].calibration_offset_at_limit is None
            assert _issue(hass, entry) is None
