"""What a device reports while Better Thermostat is not listening to it.

Better Thermostat stops reading its devices in four states: while a control
cycle runs (the inbound handler stands down), while it waits for a device to
confirm a mode, a setpoint or a calibration offset (the matching watchdog holds
the channel), and for a setpoint inside the echo window of a value it wrote.
Each state is a window in which a report can mean two things: the device
repeats something Better Thermostat already knows, or the device carries
something newer, a press at the device or a newer command landing. Both
directions are driven here for every state, because a rule that is right for
the first is exactly the rule that swallows the second.

The simulated devices confirm every write at once and the sleeps are
compressed, so none of these windows exist in the harness on its own.
``write_hold`` opens them: a held write keeps the cycle running while the test
acts, and a deferred write is a slow device that keeps reporting what it held
before.
"""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.core import Context
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    SimulatedClimate,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import (
    GENERIC_HEAT_TRV,
    GROUP_OF_THREE,
    INTEGER_GRID_TRV,
    MQTT_OFFSET_TRV,
    TRV_ID,
    DeviceProfile,
    GroupScenario,
)
from .write_hold import (
    CONFIRM_TIMEOUT,
    deferring_next_write,
    holding_next_write,
    poll_until,
)

SWITCHABLE_HEAD = GROUP_OF_THREE.profiles[0]
ALWAYS_ON_HEAD = replace(
    GROUP_OF_THREE.profiles[1],
    name="always_on_head",
    hvac_modes=(HVACMode.HEAT,),
    supported_features=ClimateEntityFeature.TARGET_TEMPERATURE,
)
MIXED_OFF_ROOM = GroupScenario(
    name="switchable_and_always_on", profiles=(SWITCHABLE_HEAD, ALWAYS_ON_HEAD)
)
"""A room with one head that can be switched off and one that cannot.

With the room off, the head without an off mode is still written to: it is
parked on its minimum setpoint. That write is what gives an off room a cycle
that can be held while the other head is operated.
"""

TWO_HEADS = GroupScenario(
    name="two_heads", profiles=(GROUP_OF_THREE.profiles[0], GROUP_OF_THREE.profiles[1])
)
"""Two identical heads: one to hold a write on, one to operate meanwhile."""

# Long enough for a watchdog that is going to end on the device's answer to
# have ended many times over; the confirmation timeout is out of reach while
# it runs, so nothing else can end it.
PROMPTLY_S = 2.0

# Long enough for a watchdog that is going to end without an answer to have
# done so, if it were going to.
SETTLE_S = 0.3


async def _start(hass, devices: DeviceProfile | GroupScenario, room: float = 18.0):
    """Set up an entry for ``devices`` and return the entity and the devices."""
    profiles = devices.profiles if isinstance(devices, GroupScenario) else (devices,)
    entities = await build_devices(hass, *profiles)
    set_room_sensor(hass, room)
    entry = make_entry(devices)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _settle(hass, bt)
    return bt, entities


async def _settle(hass, bt) -> None:
    """Wait until no cycle runs and every device has confirmed its commands."""
    assert await wait_for(
        hass,
        lambda: (
            not bt.ignore_states
            and all(
                trv.system_mode_received
                and trv.target_temp_received
                and trv.calibration_received
                for trv in bt.real_trvs.values()
            )
        ),
    )


async def _command(hass, **data) -> None:
    """Change the Better Thermostat entity the way a user in the UI does."""
    service = (
        SERVICE_SET_TEMPERATURE if "temperature" in data else SERVICE_SET_HVAC_MODE
    )
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {"entity_id": BT_ENTITY, **data}, blocking=True
    )


def _publish(device: SimulatedClimate) -> None:
    """Publish the device's state as a change that came from the device.

    An entity writes its state under the context of the service call that
    last reached it for a few seconds afterwards, so a write straight after
    one of Better Thermostat's commands would carry Better Thermostat's own
    context. A change made at the device carries a context of its own.
    """
    device.async_set_context(Context())
    device.async_write_ha_state()


def _operate(
    device: SimulatedClimate,
    *,
    hvac_mode: HVACMode | None = None,
    temperature: float | None = None,
) -> None:
    """Change the device at the device itself, and let it publish the change.

    A press at the device does not go through a service call: it reaches
    Home Assistant as a state the device publishes, and that is all Better
    Thermostat ever sees of it.
    """
    if hvac_mode is not None:
        device._attr_hvac_mode = hvac_mode
    if temperature is not None:
        device._attr_target_temperature = temperature
    _publish(device)


def _report(device: SimulatedClimate) -> None:
    """Let the device publish its next routine report.

    The internal temperature moves a little, so the report is a state change
    Better Thermostat receives; everything else it carries is what the device
    already held.
    """
    device._attr_current_temperature = (device.current_temperature or 0.0) + 0.1
    _publish(device)


async def _handled(hass, bt) -> None:
    """Wait until Better Thermostat has handled what the devices published.

    The state listener hands each device report to a task of the entity's own,
    which Home Assistant does not wait for when it settles.
    """
    await hass.async_block_till_done()
    assert await poll_until(
        hass,
        lambda: (
            not any(
                task.get_name().startswith("bt_trigger_trv_change")
                for task in bt._owned_tasks
            )
        ),
    )


async def _run_reconcile_tick(hass, bt) -> None:
    """Let the five-minute reconciler run, and the cycle it queues finish."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert await wait_for(hass, lambda: not bt.ignore_states)


async def _start_off_room(hass):
    """Return a mixed room that has been switched off and has settled."""
    bt, (switchable, always_on) = await _start(hass, MIXED_OFF_ROOM)
    with patch(WRITE_BUDGET, 0.0):
        await _command(hass, hvac_mode=HVACMode.OFF)
        assert await wait_for(hass, lambda: switchable.hvac_mode == HVACMode.OFF)
        assert await wait_for(
            hass, lambda: always_on.target_temperature == ALWAYS_ON_HEAD.min_temp
        )
        await _settle(hass, bt)
    assert bt.bt_hvac_mode == HVACMode.OFF
    return bt, switchable, always_on


# ---------------------------------------------------------------------------
# A running control cycle
# ---------------------------------------------------------------------------


async def test_a_held_write_keeps_the_cycle_that_sent_it_running(hass):
    """The harness halts a cycle for as long as the test holds its write.

    Everything below that drives a report into a running cycle rests on this:
    a cycle that carried on regardless would put the report after the cycle
    on a fast loop and inside it on a slow one.
    """
    bt, (fake_trv,) = await _start(hass, GENERIC_HEAT_TRV)

    with patch(WRITE_BUDGET, 0.0):
        async with holding_next_write(fake_trv, "async_set_temperature") as held:
            await _command(hass, temperature=23.0)
            await held.wait_reached(hass)
            assert fake_trv.target_temperature == held.keyword_arguments["temperature"]

            assert not await poll_until(hass, lambda: not bt.ignore_states, SETTLE_S)

            held.release()
            assert await poll_until(hass, lambda: not bt.ignore_states)


@pytest.mark.xfail(
    strict=True,
    reason="a head switched on while a control cycle runs is taken into the mode "
    "cache at the end of the cycle, so its next report reads as no change and the "
    "room stays off",
)
async def test_a_head_switched_on_while_a_cycle_drives_another_head_is_adopted(hass):
    """A head the user switches on during a cycle switches the room on.

    The handler stands down while the cycle runs, so the press is read on the
    head's next report instead. It is the user's intent whenever it arrives:
    the room has to follow the head, and the reconciler must not switch the
    head back off.
    """
    bt, switchable, always_on = await _start_off_room(hass)

    with patch(WRITE_BUDGET, 0.0):
        async with holding_next_write(always_on, "async_set_temperature") as held:
            # The room is off, so the knob turn on the always-on head is not
            # adopted, and the cycle parks the head on its minimum again.
            _operate(always_on, temperature=20.0)
            set_room_sensor(hass, 18.2)
            await held.wait_reached(hass)
            assert bt.ignore_states

            _operate(switchable, hvac_mode=HVACMode.HEAT)
            assert bt.ignore_states, "the press has to land inside the cycle"
            held.release()
            assert await poll_until(hass, lambda: not bt.ignore_states)

        _report(switchable)
        await _handled(hass, bt)
        assert bt.bt_hvac_mode == HVACMode.HEAT

        await _run_reconcile_tick(hass, bt)

    assert switchable.hvac_mode == HVACMode.HEAT
    assert bt.bt_hvac_mode == HVACMode.HEAT


@pytest.mark.xfail(
    strict=True,
    reason="the mode cache still holds the mode the head reported before the cycle "
    "switched it off, so the head switched back on reads as no change and the room "
    "stays off",
)
async def test_a_head_switched_back_on_during_the_cycle_that_switched_it_off_is_adopted(
    hass,
):
    """A head switched back on right after the room switched it off turns the room on.

    The press comes after Better Thermostat's command, so it is the newest
    word on the head's mode. The mode watchdog may hold it off until it has
    given up on its own command, but not beyond that.
    """
    bt, (fake_trv,) = await _start(hass, GENERIC_HEAT_TRV)
    trv = bt.real_trvs[TRV_ID]
    assert bt.bt_hvac_mode == HVACMode.HEAT

    with patch(WRITE_BUDGET, 0.0):
        async with holding_next_write(fake_trv, "async_set_hvac_mode") as held:
            await _command(hass, hvac_mode=HVACMode.OFF)
            await held.wait_reached(hass)
            assert fake_trv.hvac_mode == HVACMode.OFF

            _operate(fake_trv, hvac_mode=HVACMode.HEAT)
            assert bt.ignore_states, "the press has to land inside the cycle"
            held.release()
            assert await poll_until(hass, lambda: not bt.ignore_states)

        assert await wait_for(hass, lambda: trv.system_mode_received)
        _report(fake_trv)
        await _handled(hass, bt)
        assert bt.bt_hvac_mode == HVACMode.HEAT

        await _run_reconcile_tick(hass, bt)

    assert fake_trv.hvac_mode == HVACMode.HEAT
    assert bt.bt_hvac_mode == HVACMode.HEAT


async def test_a_head_still_reporting_the_mode_it_was_switched_out_of_is_not_adopted(
    hass,
):
    """A head that has not yet taken the room's off command does not turn the room on.

    A slow head keeps reporting heat after the cycle that switched the room
    off has ended. That is the mode Better Thermostat commanded it out of,
    not a press, so the room stays off and the head follows once the command
    lands.
    """
    bt, (fake_trv,) = await _start(hass, GENERIC_HEAT_TRV)
    trv = bt.real_trvs[TRV_ID]

    with patch(WRITE_BUDGET, 0.0):
        async with deferring_next_write(fake_trv, "async_set_hvac_mode") as deferred:
            await _command(hass, hvac_mode=HVACMode.OFF)
            assert await wait_for(hass, lambda: deferred.apply is not None)
            assert await wait_for(hass, lambda: not bt.ignore_states)

            _report(fake_trv)
            await _handled(hass, bt)
            assert fake_trv.hvac_mode == HVACMode.HEAT
            assert bt.bt_hvac_mode == HVACMode.OFF

            await deferred.land()
            assert await wait_for(hass, lambda: trv.system_mode_received)

    assert fake_trv.hvac_mode == HVACMode.OFF
    assert bt.bt_hvac_mode == HVACMode.OFF


async def test_a_knob_turned_while_a_cycle_drives_another_head_is_adopted(hass):
    """A setpoint the user turns during a cycle becomes the room's target.

    The handler stands down while the cycle runs, so the turn is read on the
    head's next report, where the value is still one Better Thermostat never
    wrote.
    """
    bt, (turned, held_head) = await _start(hass, TWO_HEADS)
    turned_trv = bt.real_trvs[turned.entity_id]

    with patch(WRITE_BUDGET, 0.0):
        async with holding_next_write(held_head, "async_set_temperature") as held:
            await _command(hass, temperature=21.0)
            await held.wait_reached(hass)
            assert await poll_until(hass, lambda: turned_trv.target_temp_received)

            _operate(turned, temperature=25.0)
            assert bt.ignore_states, "the turn has to land inside the cycle"
            held.release()
            assert await poll_until(hass, lambda: not bt.ignore_states)

        _report(turned)
        await _handled(hass, bt)

    assert bt.bt_target_temp == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# The mode watchdog
# ---------------------------------------------------------------------------


async def test_the_mode_watchdog_waits_while_the_device_reports_its_previous_mode(hass):
    """A mode command stays open until the device reports it, and closes then.

    The device still reporting the mode it held before is no answer; the
    command landing is, and it ends the wait without the timeout.
    """
    bt, (fake_trv,) = await _start(hass, GENERIC_HEAT_TRV)
    trv = bt.real_trvs[TRV_ID]

    with patch(WRITE_BUDGET, 0.0), patch(CONFIRM_TIMEOUT, 10**9):
        async with deferring_next_write(fake_trv, "async_set_hvac_mode") as deferred:
            await _command(hass, hvac_mode=HVACMode.OFF)
            assert await poll_until(hass, lambda: deferred.apply is not None)
            await poll_until(hass, lambda: trv.system_mode_received, SETTLE_S)
            assert fake_trv.hvac_mode == HVACMode.HEAT
            assert trv.system_mode_received is False

            await deferred.land()
            assert await poll_until(hass, lambda: trv.system_mode_received, PROMPTLY_S)

    assert fake_trv.hvac_mode == HVACMode.OFF


@pytest.mark.xfail(
    strict=True,
    reason="the mode watchdog keeps waiting for a mode command the room no longer "
    "wants when the device already holds the newer one, and holds user presses off "
    "until its timeout",
)
async def test_the_mode_watchdog_ends_once_the_device_holds_the_newer_intent(hass):
    """A mode command the room has taken back no longer holds the device's channel.

    The room is switched off and straight back on before the slow device has
    taken the off command. The device holds heat, which is what the room now
    wants, so there is nothing left to wait for, and a knob turn at the device
    is the user's again.
    """
    bt, (fake_trv,) = await _start(hass, GENERIC_HEAT_TRV)
    trv = bt.real_trvs[TRV_ID]

    with patch(WRITE_BUDGET, 0.0), patch(CONFIRM_TIMEOUT, 10**9):
        async with deferring_next_write(fake_trv, "async_set_hvac_mode") as deferred:
            await _command(hass, hvac_mode=HVACMode.OFF)
            assert await poll_until(hass, lambda: deferred.apply is not None)
            await _command(hass, hvac_mode=HVACMode.HEAT)
            assert await poll_until(hass, lambda: not bt.ignore_states)
            assert fake_trv.hvac_mode == HVACMode.HEAT

            assert await poll_until(hass, lambda: trv.system_mode_received, PROMPTLY_S)
            _operate(fake_trv, temperature=25.0)
            await poll_until(hass, lambda: bt.bt_target_temp == 25.0, SETTLE_S)

    assert bt.bt_target_temp == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# The setpoint watchdog
# ---------------------------------------------------------------------------


async def test_the_setpoint_watchdog_waits_while_the_device_reports_its_previous_setpoint(
    hass,
):
    """A setpoint command stays open until the device reports it, and closes then.

    The device still reporting the setpoint it held before is no answer; the
    command landing is, it ends the wait without the timeout, and it is the
    value the device is then known to hold.
    """
    bt, (fake_trv,) = await _start(hass, GENERIC_HEAT_TRV)
    trv = bt.real_trvs[TRV_ID]
    previous = fake_trv.target_temperature

    with patch(WRITE_BUDGET, 0.0), patch(CONFIRM_TIMEOUT, 10**9):
        async with deferring_next_write(fake_trv, "async_set_temperature") as deferred:
            await _command(hass, temperature=21.0)
            assert await poll_until(hass, lambda: deferred.apply is not None)
            written = deferred.keyword_arguments["temperature"]
            assert written != previous
            await poll_until(hass, lambda: trv.target_temp_received, SETTLE_S)
            assert fake_trv.target_temperature == previous
            assert trv.target_temp_received is False

            await deferred.land()
            assert await poll_until(hass, lambda: trv.target_temp_received, PROMPTLY_S)

    assert trv.confirmed_setpoint == pytest.approx(written)


@pytest.mark.xfail(
    strict=True,
    reason="the setpoint watchdog keeps waiting for the write a newer one replaced, "
    "and holds user presses off until its timeout",
)
async def test_the_setpoint_watchdog_ends_once_the_device_confirms_a_newer_write(hass):
    """A setpoint write a newer one replaced no longer holds the device's channel.

    The slow device never takes the first write; the second one lands and the
    device reports it. That is the newest command confirmed, so the wait is
    over, and a knob turn at the device is the user's again.
    """
    bt, (fake_trv,) = await _start(hass, GENERIC_HEAT_TRV)
    trv = bt.real_trvs[TRV_ID]

    with patch(WRITE_BUDGET, 0.0), patch(CONFIRM_TIMEOUT, 10**9):
        async with deferring_next_write(fake_trv, "async_set_temperature") as deferred:
            await _command(hass, temperature=21.0)
            assert await poll_until(hass, lambda: deferred.apply is not None)
            first = deferred.keyword_arguments["temperature"]

            await _command(hass, temperature=22.0)
            assert await poll_until(
                hass, lambda: fake_trv.set_temperature_calls[-1] != first
            )
            second = fake_trv.set_temperature_calls[-1]
            assert fake_trv.target_temperature == second

            assert await poll_until(hass, lambda: trv.target_temp_received, PROMPTLY_S)
            _operate(fake_trv, temperature=25.0)
            await poll_until(hass, lambda: bt.bt_target_temp == 25.0, SETTLE_S)

    assert bt.bt_target_temp == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# The calibration watchdog
# ---------------------------------------------------------------------------


async def test_the_calibration_gate_stays_shut_while_the_device_reports_its_previous_offset(
    hass,
):
    """An offset command holds the channel until the device reports it, and not longer.

    While the device still reports the offset it held before, no second
    offset goes out however the room moves; the command landing reopens the
    channel without the timeout.
    """
    bt, (fake_trv,) = await _start(hass, MQTT_OFFSET_TRV, room=21.0)
    trv = bt.real_trvs[TRV_ID]
    offset_number = fake_trv.offset_number
    previous = offset_number.native_value

    with patch(WRITE_BUDGET, 0.0), patch(CONFIRM_TIMEOUT, 10**9):
        async with deferring_next_write(
            offset_number, "async_set_native_value"
        ) as deferred:
            set_room_sensor(hass, 23.0)
            assert await poll_until(
                hass,
                lambda: deferred.apply is not None and not trv.calibration_received,
            )
            written_so_far = len(offset_number.set_value_calls)

            set_room_sensor(hass, 24.0)
            await poll_until(hass, lambda: trv.calibration_received, SETTLE_S)
            assert offset_number.native_value == previous
            assert trv.calibration_received is False
            assert len(offset_number.set_value_calls) == written_so_far

            await deferred.land()
            assert await poll_until(hass, lambda: trv.calibration_received, PROMPTLY_S)


# ---------------------------------------------------------------------------
# The echo window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "turn",
    [
        pytest.param("none", id="device_repeats_the_written_setpoint"),
        pytest.param("away", id="knob_turned_away_from_the_target"),
        pytest.param(
            "toward",
            id="knob_turned_toward_the_target",
            marks=pytest.mark.xfail(
                strict=True,
                reason="the room target counts as a value Better Thermostat "
                "wrote, so a turn that lands within one step of an off-grid "
                "target is taken for an echo and dropped",
            ),
        ),
    ],
)
async def test_a_setpoint_is_an_echo_only_when_it_is_what_was_written(hass, turn):
    """A reported setpoint is an echo when it is the value written, and a turn otherwise.

    The room target is 20.3 on a whole-degree device, so the value written is
    a whole degree next to it. The device repeating that value is Better
    Thermostat's own write; any other value on the grid is a knob turn and
    becomes the room's target, whichever side of the target it lands on.
    """
    bt, (fake_trv,) = await _start(
        hass, INTEGER_GRID_TRV, room=INTEGER_GRID_TRV.current_temperature
    )
    trv = bt.real_trvs[TRV_ID]
    target = 20.3

    written_before = len(fake_trv.set_temperature_calls)
    with patch(WRITE_BUDGET, 0.0):
        await _command(hass, temperature=target)
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > written_before
        )
        await _settle(hass, bt)
    written = fake_trv.target_temperature
    step = INTEGER_GRID_TRV.target_temperature_step
    assert bt.bt_target_temp == pytest.approx(target)
    assert written != pytest.approx(target)
    assert trv.last_temperature == pytest.approx(written)

    toward = written - step if written > target else written + step
    away = written + step if written > target else written - step
    # The premise of the toward case: the turn lands closer to the target
    # than one step, which is the width of the echo window.
    assert abs(toward - target) < step
    reported = {"none": written, "away": away, "toward": toward}[turn]

    if turn == "none":
        # The routine report carries the setpoint unchanged, so it is a state
        # change for its temperature alone.
        _report(fake_trv)
    else:
        _operate(fake_trv, temperature=reported)
    await _handled(hass, bt)
    assert fake_trv.target_temperature == pytest.approx(reported)

    expected = target if turn == "none" else reported
    assert bt.bt_target_temp == pytest.approx(expected)
