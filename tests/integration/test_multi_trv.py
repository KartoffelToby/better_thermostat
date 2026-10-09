"""A room with more than one head, driven end to end.

The rest of the suite drives one device per config entry, so everything it
proves is a statement about a single head. These are the questions that only
have an answer with several: whether one head speaking for itself can speak
for the room, whether the heads that are still there keep heating while one is
gone, whether two heads of different models each get what they can express,
and how one room-level valve command is split between them.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import contextlib
from dataclasses import dataclass, replace
from datetime import timedelta
import time
from unittest.mock import MagicMock, patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    HVACMode,
)
from homeassistant.components.weather import (
    DOMAIN as WEATHER_DOMAIN,
    WeatherEntityFeature,
)
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.watchdog import WATCHDOG_MAX_AGE_S
from custom_components.better_thermostat.utils.calibration.mpc import (
    DISTRIBUTE_COMPENSATION_PCT_PER_K,
)
from custom_components.better_thermostat.utils.controlling import reconcile_tick
from custom_components.better_thermostat.utils.scheduler import request_control_cycle
from custom_components.better_thermostat.utils.watcher import (
    STARTUP_CRITICAL_GRACE_PERIOD,
)

from .conftest import (
    BT_ENTITY,
    COOLER_RESEND,
    CRITICAL_GRACE,
    DEVICE_CALL_DEADLINE,
    DOMAIN,
    HUMIDITY_ID,
    INITIAL_TWEAK_BUDGET,
    WINDOW_ID,
    WRITE_BUDGET,
    SimulatedClimate,
    assert_profile_adopted,
    assert_write_is,
    build_devices,
    make_entry,
    mode_commands,
    profile_id,
    set_room_humidity,
    set_room_sensor,
    setpoint_commands,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import (
    COOLER_ID,
    GROUP_OF_THREE,
    GROUP_SCENARIOS,
    MIXED_GRID_GROUP,
    MQTT_OFFSET_TRV,
    ROOM_AC_COOLER,
    VALVE_GROUP,
    GroupScenario,
)
from .write_hold import holding_next_write


async def report_mode(hass, heads, mode: HVACMode) -> None:
    """Let every head in ``heads`` publish ``mode``, as if a dial was turned.

    Driven through the real climate service rather than by writing the state,
    so the device confirms the mode the way it confirms one Better Thermostat
    sends — the state change Better Thermostat then sees is the same either
    way round, which is exactly what makes it worth telling apart.

    Several heads go in one call because Better Thermostat writes back at
    them: a room turned off head by head has its first head commanded back
    into heat before the last one has been touched, so no instant exists at
    which the room ever was off.
    """
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {"entity_id": [head.entity_id for head in heads], "hvac_mode": mode},
        blocking=True,
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("trv_group", GROUP_SCENARIOS, indirect=True, ids=profile_id)
async def test_startup_adopts_every_head_of_the_group(hass, trv_group):
    """Every head in the entry is read, with its own capabilities.

    The guard under the rest of this file: a group whose second head never
    made it into ``real_trvs`` still passes everything that only looks at the
    first, and a group whose heads were all read as one shape no longer tells
    them apart at all.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert list(bt.real_trvs) == [p.entity_id for p in trv_group.scenario.profiles]
    for profile in trv_group.scenario.profiles:
        assert_profile_adopted(bt, profile)


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_one_head_reporting_off_does_not_switch_the_room_off(hass, trv_group):
    """A single valve dropping out of heat leaves the room heating.

    A head enters frost protection, or somebody turns one dial down, and Home
    Assistant reports that as ``off``. Adopting it as the room's mode is what
    made a whole flat go cold from one valve (#2063): the other heads are
    still asking for heat, and nothing but this head said otherwise.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.bt_hvac_mode == HVACMode.HEAT

    await report_mode(hass, [trv_group[1]], HVACMode.OFF)

    assert bt.bt_hvac_mode == HVACMode.HEAT
    assert hass.states.get(BT_ENTITY).state == HVACMode.HEAT


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_the_room_switches_off_once_every_head_reports_off(hass, trv_group):
    """A room whose heads all went off follows them.

    The other half of the rule, and the reason the one above is a rule and not
    a refusal: the mode is still adopted from the devices, just not from one
    of them. Without this a quorum that never passes reads exactly like a
    quorum that works.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    await report_mode(hass, trv_group.entities, HVACMode.OFF)

    assert await wait_for(hass, lambda: bt.bt_hvac_mode == HVACMode.OFF)
    assert hass.states.get(BT_ENTITY).state == HVACMode.OFF


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_the_group_keeps_heating_the_room_while_one_head_is_gone(hass, trv_group):
    """A head that drops off the air takes only itself out of the room.

    The bulkhead: a battery head out of radio range must not stop the heads
    that are still reachable from being commanded, because the room is still
    cold and they can still heat it. The absent head is left alone rather than
    written into the void — coming back is what the reachability backoff
    watches for, and a command sent meanwhile would be lost anyway.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    absent = trv_group[1]
    present = [head for head in trv_group.entities if head is not absent]
    absent.set_available(False)
    await hass.async_block_till_done()
    baselines = {head.entity_id: len(head.set_temperature_calls) for head in present}
    # Only from here on does the absence mean anything, so the bus is read
    # from here on too: everything before it was addressed to a head that
    # was still there.
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 23.0},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: all(
                len(head.set_temperature_calls) > baselines[head.entity_id]
                for head in present
            ),
        ), {head.entity_id: head.set_temperature_calls for head in present}

    for head in present:
        assert_write_is(head.set_temperature_calls[-1], 23.0, head.profile)
    assert setpoint_commands(events, absent.entity_id) == []
    assert bt.heat_target_temperature == pytest.approx(23.0)


# A deadline the test can wait out in a blink, for devices that never answer.
SHORT_DEVICE_DEADLINE = 0.05


async def _never_answers(*_args: object, **_kwargs: object) -> None:
    """Take a write and never return, like a device whose call never comes back."""
    await asyncio.Event().wait()


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_a_head_that_never_answers_does_not_hold_up_the_others(
    hass, trv_group, caplog
):
    """A head that stops answering costs the room one deadline per cycle.

    Some integrations keep a service call open until the device answers and
    put no bound on the wait: a sleeping Z-Wave node, a cloud API without a
    request timeout. The room's heads are written one after another, so a
    write that never returns would keep every other head waiting with it, and
    the room would stop following its target and its mode without a word in
    the log. The write is given up at the deadline, named in the log, and the
    other heads are written as before, a switch to off included.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    hung = trv_group[0]
    others = [head for head in trv_group.entities if head is not hung]
    # An instance attribute shadows the method the service handler looks up.
    hung.async_set_temperature = _never_answers
    try:
        with (
            patch(WRITE_BUDGET, 0.0),
            patch(DEVICE_CALL_DEADLINE, SHORT_DEVICE_DEADLINE),
        ):
            assert await room_target_reaches(hass, others, 23.0), {
                head.entity_id: head.set_temperature_calls for head in others
            }
            assert any(
                record.levelname == "WARNING"
                and hung.entity_id in record.getMessage()
                and "could not be written" in record.getMessage()
                for record in caplog.records
            ), [record.getMessage() for record in caplog.records]

            await hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
                {"entity_id": BT_ENTITY, "hvac_mode": HVACMode.OFF},
                blocking=True,
            )
            assert await wait_for(
                hass,
                lambda: all(
                    hass.states.get(head.entity_id).state == HVACMode.OFF
                    for head in others
                ),
            ), {head.entity_id: head.set_hvac_mode_calls for head in others}
            assert await wait_for(hass, lambda: not bt.ignore_states)
    finally:
        del hung.async_set_temperature


async def _set_room_mode(hass, mode: HVACMode) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {"entity_id": BT_ENTITY, "hvac_mode": mode},
        blocking=True,
    )


async def _quirk_writes_the_mode(
    bt: BetterThermostat, entity_id: str, hvac_mode: str
) -> bool:
    """A model quirk that switches the mode with its own service call."""
    await bt.hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {"entity_id": entity_id, "hvac_mode": hvac_mode},
        blocking=True,
        context=bt.context,
    )
    return True


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
@pytest.mark.parametrize("through_quirk", [False, True], ids=["adapter", "quirk"])
async def test_a_mode_write_that_never_answered_and_lands_later_is_not_a_press(
    hass, trv_group, through_quirk
):
    """A mode the room gave up waiting for is not a press when it lands later.

    A sleeping Z-Wave node queues a command and keeps the call open until it
    wakes. The write is given up at the deadline, but the command is still
    queued and the device applies it whenever it wakes. When the user has
    turned the room off in the meantime, that late heat is Better
    Thermostat's own command, and the room stays off. While its mode channel
    does not answer, the head still gets its setpoints. Both hold whether the
    adapter or a model quirk sends the mode.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    hung = trv_group[0]
    others = [head for head in trv_group.entities if head is not hung]
    quirks = bt.real_trvs[hung.entity_id].model_quirks
    assert quirks is not None
    with (
        patch(WRITE_BUDGET, 0.0),
        patch(DEVICE_CALL_DEADLINE, SHORT_DEVICE_DEADLINE),
        patch.object(
            quirks,
            "override_set_hvac_mode",
            autospec=True,
            side_effect=_quirk_writes_the_mode,
        )
        if through_quirk
        else contextlib.nullcontext(),
    ):
        await _set_room_mode(hass, HVACMode.OFF)
        assert await wait_for(
            hass,
            lambda: all(
                hass.states.get(head.entity_id).state == HVACMode.OFF
                for head in trv_group.entities
            ),
        )
        assert await wait_for(hass, lambda: not bt.ignore_states)

        queued: list[str] = []

        async def queue_and_never_answer(hvac_mode: str) -> None:
            queued.append(hvac_mode)
            await asyncio.Event().wait()

        # An instance attribute shadows the method the service handler looks up.
        hung.async_set_hvac_mode = queue_and_never_answer
        try:
            await _set_room_mode(hass, HVACMode.HEAT)
            assert await wait_for(
                hass,
                lambda: all(
                    hass.states.get(head.entity_id).state == HVACMode.HEAT
                    for head in others
                ),
            )
            assert await wait_for(hass, lambda: not bt.ignore_states)
            assert HVACMode.HEAT in queued
            assert await room_target_reaches(hass, trv_group.entities, 23.0), {
                head.entity_id: head.set_temperature_calls
                for head in trv_group.entities
            }

            await _set_room_mode(hass, HVACMode.OFF)
            assert await wait_for(
                hass,
                lambda: all(
                    hass.states.get(head.entity_id).state == HVACMode.OFF
                    for head in others
                ),
            )
            assert await wait_for(hass, lambda: not bt.ignore_states)
        finally:
            del hung.async_set_hvac_mode

        # The node wakes and applies the command it queued. The report comes
        # long after the call, so it carries none of Better Thermostat's
        # contexts.
        hung.async_set_context(Context())
        hung._attr_hvac_mode = HVACMode.HEAT
        hung.async_write_ha_state()
        await hass.async_block_till_done()
        assert await wait_for(hass, lambda: not bt.ignore_states)

        assert await wait_for(
            hass, lambda: hass.states.get(hung.entity_id).state == HVACMode.OFF
        ), hung.set_hvac_mode_calls
        assert bt.bt_hvac_mode == HVACMode.OFF
        assert hass.states.get(BT_ENTITY).state == HVACMode.OFF


async def test_a_cooler_that_never_answers_does_not_hold_up_the_heads(hass, caplog):
    """A cooler that stops answering costs the room one deadline per write.

    The cooler is written first in every cycle, before any head. An air
    conditioner behind a cloud API without a request timeout would keep the
    cycle waiting for good, and no head would get another write. The cooler
    write is given up at the deadline, named in the log, and the heads are
    written as before.
    """
    *heads, cooler = await build_devices(hass, *GROUP_OF_THREE.profiles, ROOM_AC_COOLER)
    set_room_sensor(hass, 19.5)
    base = make_entry(GROUP_OF_THREE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=base.version,
        data={**base.data, "cooler": COOLER_ID},
        title=base.title,
    )
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    # Instance attributes shadow the methods the service handler looks up.
    cooler.async_set_temperature = _never_answers
    cooler.async_set_hvac_mode = _never_answers
    try:
        with (
            patch(WRITE_BUDGET, 0.0),
            patch(COOLER_RESEND, 0.0),
            patch(DEVICE_CALL_DEADLINE, SHORT_DEVICE_DEADLINE),
        ):
            baselines = {
                head.entity_id: len(head.set_temperature_calls) for head in heads
            }
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                "set_temperature",
                {
                    "entity_id": BT_ENTITY,
                    "target_temp_low": 23.0,
                    "target_temp_high": 27.0,
                },
                blocking=True,
            )
            assert await wait_for(
                hass,
                lambda: all(
                    len(head.set_temperature_calls) > baselines[head.entity_id]
                    for head in heads
                ),
            ), {head.entity_id: head.set_temperature_calls for head in heads}
            for head in heads:
                written = head.set_temperature_calls[-1]
                assert isinstance(written, float), written
                assert_write_is(written, 23.0, head.profile)
            assert any(
                record.levelname == "WARNING"
                and COOLER_ID in record.getMessage()
                and "failed" in record.getMessage()
                for record in caplog.records
            ), [record.getMessage() for record in caplog.records]
            assert await wait_for(hass, lambda: not bt.ignore_states)
    finally:
        del cooler.async_set_temperature
        del cooler.async_set_hvac_mode


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_a_control_cycle_that_does_not_end_is_reported_once(
    hass, trv_group, caplog
):
    """The control watchdog names a cycle that runs past its window.

    A running cycle holds the inbound handler and the reconciler off, so
    neither notices a cycle that never ends. Once a cycle has run for the
    watchdog window, an error says so, once rather than on every tick.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    clock = FakeClock(monotonic_value=time.monotonic(), now_value=dt_util.now())
    bt.clock = clock

    def overrun_reports() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelname == "ERROR"
            and "control cycle has been running" in record.getMessage()
        ]

    with patch(WRITE_BUDGET, 0.0):
        async with holding_next_write(trv_group[0], "async_set_temperature") as held:
            await set_room_target(hass, 23.0)
            await held.wait_reached(hass)
            assert bt.ignore_states

            clock.advance(WATCHDOG_MAX_AGE_S / 2)
            await reconcile_tick(bt)
            assert overrun_reports() == []

            clock.advance(WATCHDOG_MAX_AGE_S)
            await reconcile_tick(bt)
            clock.advance(WATCHDOG_MAX_AGE_S)
            await reconcile_tick(bt)
            assert len(overrun_reports()) == 1, overrun_reports()


@pytest.mark.parametrize("trv_group", [MIXED_GRID_GROUP], indirect=True, ids=profile_id)
async def test_two_heads_of_different_models_each_get_what_they_can_express(
    hass, trv_group
):
    """One room target reaches two heads in the two shapes they accept.

    A room is rarely fitted out in one go, so its heads differ in the grid
    their setpoint sits on and in what they call the mode they heat in. The
    room holds one target either way, and rounding it once for the room would
    put it beside the grid of whichever head did not get to decide.
    """
    half_degree, whole_degree = trv_group.scenario.profiles
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, half_degree)
    assert_profile_adopted(bt, whole_degree)
    # Startup writes a setpoint of its own, so the write under test is the
    # last one past this mark rather than the only one.
    baselines = {
        head.entity_id: len(head.set_temperature_calls) for head in trv_group.entities
    }

    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 21.5},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: all(
                len(head.set_temperature_calls) > baselines[head.entity_id]
                for head in trv_group.entities
            ),
        ), {head.entity_id: head.set_temperature_calls for head in trv_group.entities}

    for head in trv_group.entities:
        assert_write_is(head.set_temperature_calls[-1], 21.5, head.profile)
    assert mode_commands(events, whole_degree.entity_id) == []
    assert hass.states.get(whole_degree.entity_id).state == HVACMode.HEAT_COOL


@pytest.mark.parametrize("trv_group", [VALVE_GROUP], indirect=True, ids=profile_id)
async def test_the_colder_head_of_a_valve_group_is_opened_further(hass, trv_group):
    """One room-level valve command reaches the heads as two openings.

    The heads of a room do not sit in the same air: one is by the window and
    reads colder than the other. The controller computes a single opening for
    the room, and splitting it means the colder head opens further while the
    warmest gets the room's figure unchanged — which is the only thing that
    makes the difference between the two openings mean anything.

    The target is barely above the room, because the split is an addition on
    top of the room's opening: a room that calls for everything the heads have
    puts both of them at fully open, where no difference can show.
    """
    warm_head, cold_head = trv_group.entities
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    warm_valve, cold_valve = warm_head.valve_number, cold_head.valve_number
    expected_extra = DISTRIBUTE_COMPENSATION_PCT_PER_K * (
        warm_head.profile.current_temperature - cold_head.profile.current_temperature
    )

    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 20.0},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: (
                any(v > 0 for v in warm_valve.set_value_calls)
                and any(v > 0 for v in cold_valve.set_value_calls)
            ),
        ), (warm_valve.set_value_calls, cold_valve.set_value_calls)

    opened_warm = warm_valve.set_value_calls[-1]
    opened_cold = cold_valve.set_value_calls[-1]
    # Both ends of the split have to be off their stops for the difference to
    # be the compensation rather than a clamp.
    assert 0.0 < opened_warm < 100.0
    assert opened_cold < 100.0
    assert opened_cold - opened_warm == pytest.approx(expected_extra, abs=1.0)
    cold_valve_percent = bt.real_trvs[cold_head.entity_id].last_valve_percent
    warm_valve_percent = bt.real_trvs[warm_head.entity_id].last_valve_percent
    assert cold_valve_percent is not None
    assert warm_valve_percent is not None
    assert cold_valve_percent > warm_valve_percent


DOOR_ID = "binary_sensor.door"
OUTDOOR_ID = "sensor.outdoor_temperature"
WEATHER_ID = "weather.home"

# Either side of the entry's off temperature of 5 °C: heating is wanted below
# it and summer mode takes over above it.
COLD_OUTSIDE = 0.0
WARM_OUTSIDE = 30.0

# The ambient check averages the outdoor sensor's recorder history. The recorder
# commits in batches, so a reading published a moment ago is not in it yet and
# the average would still be the previous one. Without any history the check
# falls back to the current reading, which is the reading under test.
OUTDOOR_HISTORY = (
    "custom_components.better_thermostat.utils.weather.history."
    "state_changes_during_period"
)

# The control-cycle request under the name the entity's handlers call it by.
ENTITY_CYCLE_REQUEST = (
    "custom_components.better_thermostat.climate.request_control_cycle"
)

# A startup grace window that is already over by the time the first check
# runs, and the production one, for a room that has to start without waiting.
NO_GRACE = timedelta(seconds=0)
STARTUP_GRACE = STARTUP_CRITICAL_GRACE_PERIOD

# How long a reaction is waited for. Everything under test answers within a
# few loop turns, and a case that is expected to fail waits this out in full.
REACTION_TIMEOUT_S = 3.0


def _publish_forecast(
    hass: HomeAssistant, forecast: list[float], temperature: float
) -> None:
    """Let the weather entity forecast ``temperature`` from now on."""
    forecast[:] = [temperature, temperature]
    hass.states.async_set(
        WEATHER_ID,
        "sunny",
        {
            "temperature": temperature,
            "supported_features": WeatherEntityFeature.FORECAST_DAILY,
        },
    )


@dataclass
class OutageRoom:
    """A three-head room with every input it can be given, and its thermostat.

    ``present`` are the heads that are still on the air, ``absent`` the head
    taken off it (``None`` while every head is there). ``cycle_requests``
    records the control cycles the entity's own handlers ask for.
    """

    hass: HomeAssistant
    bt: BetterThermostat
    present: list[SimulatedClimate]
    absent: SimulatedClimate | None
    cooler: SimulatedClimate
    forecast: list[float]
    cycle_requests: MagicMock

    def publish_forecast(self, temperature: float) -> None:
        """Let the weather entity forecast ``temperature`` from now on."""
        _publish_forecast(self.hass, self.forecast, temperature)


@dataclass(frozen=True)
class Entrance:
    """One way news reaches the room, and what the room does about it.

    ``config`` names the input in the config entry, ``report`` puts the news
    on the bus the way the device or the clock would, and ``reached`` says
    whether the heads or the thermostat acted on it.
    """

    name: str
    config: Mapping[str, object]
    report: Callable[[OutageRoom], Awaitable[None]]
    reached: Callable[[OutageRoom], bool]


def _present_heads_are_off(room: OutageRoom) -> bool:
    """Whether every reachable head was last commanded off."""
    return all(head.set_hvac_mode_calls[-1:] == [HVACMode.OFF] for head in room.present)


async def _window_opens(room: OutageRoom) -> None:
    room.hass.states.async_set(WINDOW_ID, "on")


async def _door_opens(room: OutageRoom) -> None:
    room.hass.states.async_set(DOOR_ID, "on")


async def _room_sensor_reports(room: OutageRoom) -> None:
    set_room_sensor(room.hass, 21.3)


async def _humidity_sensor_reports(room: OutageRoom) -> None:
    set_room_humidity(room.hass, 61.0)


def _report_on_its_own(device: SimulatedClimate) -> None:
    """Publish the device's state as a report of its own.

    The entity still holds the context of the last command Better Thermostat
    sent it, and a state written under that context is read as the echo of
    that command. A device that reports by itself does so under a new one.
    """
    device.async_set_context(Context())
    device.async_write_ha_state()


async def _present_head_reports(room: OutageRoom) -> None:
    head = room.present[-1]
    head._attr_current_temperature = 16.5
    _report_on_its_own(head)


async def _cooler_reports(room: OutageRoom) -> None:
    room.cooler._attr_target_temperature = 26.0
    _report_on_its_own(room.cooler)


async def _outdoor_sensor_reports(room: OutageRoom) -> None:
    """Report warm weather that has held for three days.

    The outdoor check damps the readings over about a day, so a warm reading
    switches summer mode on once it has been current long enough. The
    entity's clock is moved three days past the report.
    """
    clock = FakeClock(monotonic_value=time.monotonic(), now_value=dt_util.now())
    clock.advance(timedelta(days=3).total_seconds())
    room.bt.clock = clock
    room.hass.states.async_set(
        OUTDOOR_ID, str(WARM_OUTSIDE), {"unit_of_measurement": "°C"}
    )


async def _weather_tick_fires(room: OutageRoom) -> None:
    """Report warm weather that has held for three days, then fire the hourly check.

    The current temperature of the weather entity is damped over about a
    day, like an outdoor sensor's, so the entity's clock is moved three days
    past the report.
    """
    clock = FakeClock(monotonic_value=time.monotonic(), now_value=dt_util.now())
    clock.advance(timedelta(days=3).total_seconds())
    room.bt.clock = clock
    room.publish_forecast(WARM_OUTSIDE)
    async_fire_time_changed(room.hass, dt_util.utcnow() + timedelta(hours=1, seconds=1))


async def _periodic_tick_fires(room: OutageRoom) -> None:
    async_fire_time_changed(
        room.hass, dt_util.utcnow() + timedelta(minutes=5, seconds=1)
    )


WINDOW_OPENS = Entrance(
    "window_opens",
    {"window_sensors": WINDOW_ID, "window_off_delay": 0, "window_off_delay_after": 0},
    _window_opens,
    _present_heads_are_off,
)

ENTRANCES = [
    WINDOW_OPENS,
    Entrance(
        "door_opens",
        {"door_sensors": DOOR_ID, "door_off_delay": 0, "door_off_delay_after": 0},
        _door_opens,
        _present_heads_are_off,
    ),
    Entrance(
        "room_sensor_reports",
        {},
        _room_sensor_reports,
        lambda room: room.bt.room_temperature == pytest.approx(21.3),
    ),
    Entrance(
        "humidity_sensor_reports",
        {"humidity_sensor": HUMIDITY_ID},
        _humidity_sensor_reports,
        lambda room: room.bt.current_humidity == pytest.approx(61.0),
    ),
    Entrance(
        "reachable_head_reports",
        {},
        _present_head_reports,
        lambda room: (
            room.bt.real_trvs[room.present[-1].entity_id].current_temperature
            == pytest.approx(16.5)
        ),
    ),
    Entrance(
        "cooler_reports",
        {"cooler": COOLER_ID},
        _cooler_reports,
        lambda room: room.bt.cool_target_temperature == pytest.approx(26.0),
    ),
    Entrance(
        "outdoor_sensor_reports",
        {"outdoor_sensor": OUTDOOR_ID},
        _outdoor_sensor_reports,
        _present_heads_are_off,
    ),
    Entrance(
        "hourly_weather_tick",
        {"weather": WEATHER_ID},
        _weather_tick_fires,
        _present_heads_are_off,
    ),
    Entrance(
        "periodic_tick",
        {},
        _periodic_tick_fires,
        lambda room: room.cycle_requests.called,
    ),
]


def entrance_id(entrance: Entrance) -> str:
    """Name a parametrized case after the entrance it drives."""
    return entrance.name


async def open_room(
    hass, entrance: Entrance, *, one_head_gone: bool, gone_at_boot: bool = False
) -> OutageRoom:
    """Start a heating three-head room wired for ``entrance``.

    Every input the room can have is published before startup, cold outside
    and all contacts shut, but only the one the entrance needs is in the
    entry, so no other input can answer for it. With ``one_head_gone`` the
    middle head drops off the air once the room is running; with
    ``gone_at_boot`` as well, it is already off the air when the room starts
    and the startup grace window is over before the first check.
    """
    *heads, cooler = await build_devices(hass, *GROUP_OF_THREE.profiles, ROOM_AC_COOLER)
    set_room_sensor(hass, 19.5)
    set_room_humidity(hass, 40.0)
    hass.states.async_set(WINDOW_ID, "off")
    hass.states.async_set(DOOR_ID, "off")
    hass.states.async_set(OUTDOOR_ID, str(COLD_OUTSIDE), {"unit_of_measurement": "°C"})
    forecast: list[float] = []

    async def get_forecasts(call):
        return {WEATHER_ID: {"forecast": [{"temperature": t} for t in forecast]}}

    hass.services.async_register(
        WEATHER_DOMAIN,
        "get_forecasts",
        get_forecasts,
        supports_response=SupportsResponse.ONLY,
    )
    base = make_entry(GROUP_OF_THREE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=base.version,
        data={**base.data, **entrance.config},
        title=base.title,
    )
    _publish_forecast(hass, forecast, COLD_OUTSIDE)
    if one_head_gone and gone_at_boot:
        heads[1].set_available(False)
    with patch(CRITICAL_GRACE, NO_GRACE if gone_at_boot else STARTUP_GRACE):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
    room = OutageRoom(hass, bt, heads, None, cooler, forecast, MagicMock())

    target = (
        {"target_temp_low": 22.0, "target_temp_high": 25.0}
        if "cooler" in entrance.config
        else {"temperature": 22.0}
    )
    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {"entity_id": BT_ENTITY, **target},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert room.bt.hvac_mode != HVACMode.OFF

    if one_head_gone:
        room.absent = heads[1]
        room.present = [head for head in heads if head is not room.absent]
        room.absent.set_available(False)
        await hass.async_block_till_done()
    return room


async def report_and_wait(room: OutageRoom, entrance: Entrance) -> bool:
    """Put the entrance's news on the bus and wait for the room to act on it."""
    with (
        patch(WRITE_BUDGET, 0.0),
        patch(COOLER_RESEND, 0.0),
        patch(ENTITY_CYCLE_REQUEST, wraps=request_control_cycle) as requests,
    ):
        room.cycle_requests = requests
        await entrance.report(room)
        return await wait_for(
            room.hass,
            lambda: entrance.reached(room),
            timeout_seconds=REACTION_TIMEOUT_S,
        )


@pytest.mark.parametrize("entrance", ENTRANCES, ids=entrance_id)
async def test_every_entrance_reaches_a_room_with_all_heads(hass, entrance):
    """Every input of a room reaches it while all of its heads are there.

    The baseline for the case below: each entrance is wired and observed here
    exactly as it is there, so a failure below is about the gone head and not
    about the wiring.
    """
    with patch(OUTDOOR_HISTORY, return_value={}):
        room = await open_room(hass, entrance, one_head_gone=False)
        assert await report_and_wait(room, entrance)


@pytest.mark.parametrize("entrance", ENTRANCES, ids=entrance_id)
async def test_every_entrance_reaches_the_room_while_one_head_is_gone(hass, entrance):
    """A head off the air takes only itself out of the room.

    The room still has heads that can heat it and a user who can open its
    window, so every input keeps reaching it: a window or door opening turns
    the reachable heads off, summer mode does the same, a sensor reading
    becomes the room's reading, a reachable head's report is taken in, and
    the periodic tick keeps asking for control cycles. The absent head is
    what the reachability backoff and the repair issue are for.
    """
    with patch(OUTDOOR_HISTORY, return_value={}):
        room = await open_room(hass, entrance, one_head_gone=True)
        assert await report_and_wait(room, entrance)


async def let_two_hours_pass(room: OutageRoom) -> None:
    """Run two hours of periodic ticks past the startup grace windows."""
    # The grace windows and the degradation ladder read the entity's clock,
    # which is driven here together with Home Assistant's timers.
    clock = FakeClock(monotonic_value=time.monotonic(), now_value=dt_util.now())
    room.bt.clock = clock
    with patch(WRITE_BUDGET, 0.0):
        for step in range(1, 25):
            clock.advance(300)
            async_fire_time_changed(
                room.hass, dt_util.utcnow() + timedelta(minutes=5 * step, seconds=1)
            )
            await room.hass.async_block_till_done()


async def test_a_head_gone_for_hours_is_the_only_one_reported(hass):
    """The room reports the head that stays away, and only that head.

    Two hours after the startup grace windows, the absent head is the one
    entry in the room's device errors; the reachable heads are not listed.
    """
    room = await open_room(hass, WINDOW_OPENS, one_head_gone=True)
    await let_two_hours_pass(room)

    assert room.absent is not None
    assert room.bt.devices_errors == [room.absent.entity_id]


async def test_a_room_with_a_head_gone_for_hours_still_answers_its_window(hass):
    """A head that stays away does not leave the room deaf for its absence.

    The grace windows only decide when the outage is announced; once it is,
    and for as long as the head stays away, the reachable heads keep
    following the room. Two hours of periodic ticks after the announcement,
    an opened window still turns them off.
    """
    room = await open_room(hass, WINDOW_OPENS, one_head_gone=True)
    await let_two_hours_pass(room)

    assert await report_and_wait(room, WINDOW_OPENS)


@pytest.mark.parametrize("entrance", ENTRANCES, ids=entrance_id)
async def test_every_entrance_reaches_a_room_that_started_without_a_head(
    hass, entrance
):
    """A head that is off the air at boot takes only itself out of the room.

    The room starts once the startup grace window has closed, and from then
    on it answers every input exactly as a room whose head dropped out after
    startup does.
    """
    with patch(OUTDOOR_HISTORY, return_value={}):
        room = await open_room(hass, entrance, one_head_gone=True, gone_at_boot=True)
        assert await report_and_wait(room, entrance)


def missing_entity_issues(hass) -> list[str]:
    """Return the missing-entity repair issues Better Thermostat holds open."""
    return sorted(
        issue_id
        for (domain, issue_id) in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith("missing_entity_")
    )


async def boot_with_heads_gone(
    hass, trv_group, gone: list[SimulatedClimate], *, grace=NO_GRACE
):
    """Start the group's room with the heads in ``gone`` off the air.

    Returns the entity as soon as the entry is set up, whether or not its
    startup has finished; the grace window stays patched for the whole run
    of the setup, which is the only time startup reads it.
    """
    set_room_sensor(hass, 19.5)
    for head in gone:
        head.set_available(False)
    entry = make_entry(trv_group.scenario)
    with patch(CRITICAL_GRACE, grace):
        await setup_entry(hass, entry)
        for _ in range(20):
            await hass.async_block_till_done()
    return entry.runtime_data.climate, entry


async def set_room_target(hass, value: float) -> None:
    """Set a room target the heads do not hold, so reaching them takes a write."""
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": value},
        blocking=True,
    )


async def room_target_reaches(hass, heads, value: float) -> bool:
    """Set a room target and report whether every head gets a write carrying it.

    Only writes after the command count, so a write left over from startup
    cannot stand in for one the target caused.
    """
    baselines = {head.entity_id: len(head.set_temperature_calls) for head in heads}
    await set_room_target(hass, value)
    if not await wait_for(
        hass,
        lambda: all(
            len(head.set_temperature_calls) > baselines[head.entity_id]
            for head in heads
        ),
    ):
        return False
    for head in heads:
        assert_write_is(head.set_temperature_calls[-1], value, head.profile)
    return True


def has_adopted(bt, profile) -> bool:
    """Whether Better Thermostat has read this device's capabilities."""
    try:
        assert_profile_adopted(bt, profile)
    except AssertionError:
        return False
    return True


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_a_room_booting_with_a_head_gone_starts_once_the_grace_window_closes(
    hass, trv_group
):
    """A head that is off the air at boot does not hold the room back for good.

    The startup grace window is what separates a head that is late from one
    that is gone. Once it has closed, the room starts with the heads it has:
    it takes its listeners, heats through the reachable heads, and names the
    absent one, which it leaves alone.
    """
    absent = trv_group[1]
    present = [head for head in trv_group.entities if head is not absent]
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    with patch(CRITICAL_GRACE, NO_GRACE):
        bt, entry = await boot_with_heads_gone(hass, trv_group, [absent])
        await wait_for_startup(hass, entry)

    assert hass.states.get(BT_ENTITY).state == HVACMode.HEAT
    assert await room_target_reaches(hass, present, 22.0), {
        head.entity_id: head.set_temperature_calls for head in present
    }
    assert absent.set_temperature_calls == []
    assert setpoint_commands(events, absent.entity_id) == []
    assert bt.devices_errors == [absent.entity_id]
    assert missing_entity_issues(hass) == [f"missing_entity_{absent.entity_id}"]
    for head in present:
        assert_profile_adopted(bt, head.profile)


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_a_room_booting_with_a_head_gone_waits_out_the_grace_window(
    hass, trv_group
):
    """A head that is still booting is waited for, not started without.

    A slow integration at boot looks exactly like a head that is gone. Inside
    the grace window the room keeps waiting for every head and commands none
    of them, so a late one is initialised with the others instead of being
    left behind.
    """
    absent = trv_group[1]
    bt, entry = await boot_with_heads_gone(
        hass, trv_group, [absent], grace=STARTUP_GRACE
    )

    assert not await wait_for(
        hass,
        lambda: any(head.set_temperature_calls for head in trv_group.entities),
        timeout_seconds=1.0,
    )
    assert bt.startup_running
    assert hass.states.get(BT_ENTITY).state == "unavailable"
    assert missing_entity_issues(hass) == []

    absent.set_available(True)
    bt = await wait_for_startup(hass, entry)
    for head in trv_group.entities:
        assert_profile_adopted(bt, head.profile)


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_a_room_with_every_head_gone_keeps_waiting_after_the_grace_window(
    hass, trv_group
):
    """A room with no head to drive does not start, it waits and says why.

    Starting would leave nothing to command, and every value startup reads
    off the heads, the temperature range and the mode among them, would be
    missing. The room stays unavailable, names every absent head, and starts
    as soon as one of them arrives.
    """
    bt, entry = await boot_with_heads_gone(hass, trv_group, trv_group.entities)

    assert bt.startup_running
    assert hass.states.get(BT_ENTITY).state == "unavailable"
    assert missing_entity_issues(hass) == sorted(
        f"missing_entity_{head.entity_id}" for head in trv_group.entities
    )

    first = trv_group[0]
    with patch(CRITICAL_GRACE, NO_GRACE):
        first.set_available(True)
        bt = await wait_for_startup(hass, entry)

    assert hass.states.get(BT_ENTITY).state == HVACMode.HEAT
    assert_profile_adopted(bt, first.profile)
    assert await room_target_reaches(hass, [first], 22.0), first.set_temperature_calls


@pytest.mark.parametrize("trv_group", GROUP_SCENARIOS, indirect=True, ids=profile_id)
async def test_a_head_that_arrives_after_the_room_started_is_initialised(
    hass, trv_group
):
    """A head the room started without is set up in full once it arrives.

    Startup reads each head's capabilities, discovers its calibration and
    valve channels and reads its offsets. A head that missed startup gets the
    same, the moment it is back, so it is not driven as a device without
    capabilities.
    """
    absent = trv_group[-1]
    with patch(CRITICAL_GRACE, NO_GRACE):
        bt, entry = await boot_with_heads_gone(hass, trv_group, [absent])
        bt = await wait_for_startup(hass, entry)
    assert not has_adopted(bt, absent.profile)

    absent.set_available(True)

    assert await wait_for(hass, lambda: has_adopted(bt, absent.profile))
    for head in trv_group.entities:
        assert_profile_adopted(bt, head.profile)
    assert await wait_for(hass, lambda: bt.devices_errors == [])


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_a_head_that_arrives_after_the_room_started_follows_the_room(
    hass, trv_group
):
    """A head the room started without takes the room's target once it is back.

    It is commanded as soon as it arrives, and from then on every change of
    the room's target reaches it like it reaches the heads that were there
    from the start.
    """
    absent = trv_group[1]
    with patch(CRITICAL_GRACE, NO_GRACE):
        bt, entry = await boot_with_heads_gone(hass, trv_group, [absent])
        bt = await wait_for_startup(hass, entry)
    assert absent.set_temperature_calls == []

    with patch(WRITE_BUDGET, 0.0):
        await set_room_target(hass, 22.0)
        absent.set_available(True)
        assert await wait_for(hass, lambda: absent.set_temperature_calls)
        assert bt.heat_target_temperature is not None
        assert_write_is(
            absent.set_temperature_calls[-1], bt.heat_target_temperature, absent.profile
        )

        baselines = {
            head.entity_id: len(head.set_temperature_calls)
            for head in trv_group.entities
        }
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 23.0},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: all(
                len(head.set_temperature_calls) > baselines[head.entity_id]
                for head in trv_group.entities
            ),
        ), {head.entity_id: head.set_temperature_calls for head in trv_group.entities}

    for head in trv_group.entities:
        assert_write_is(head.set_temperature_calls[-1], 23.0, head.profile)


MAINTENANCE_EXERCISE = (
    "custom_components.better_thermostat.climate.run_valve_maintenance"
)

OFFSET_GROUP = GroupScenario(
    name="offset_number_group",
    profiles=tuple(
        replace(
            MQTT_OFFSET_TRV,
            name=f"offset_group_{letter}",
            entity_id=f"climate.offset_group_{letter}",
            entity_name=f"offset group {letter}",
        )
        for letter in ("a", "b")
    ),
)
"""Two Zigbee2MQTT heads whose calibration is a number entity on the device.

Setting such a head up writes to its device: the calibration number is put
back to zero before Better Thermostat takes the calibration over.
"""


@pytest.mark.parametrize(
    "trv_group", [*GROUP_SCENARIOS, OFFSET_GROUP], indirect=True, ids=profile_id
)
async def test_a_head_that_arrives_during_valve_maintenance_waits_for_its_end(
    hass, trv_group
):
    """A head that arrives while valve maintenance runs is set up after it.

    Setting a head up writes to its device, and maintenance holds the room's
    devices for the exercise, so nothing is set up and nothing is written
    until maintenance has ended. The head is set up then without having to
    report again, and takes the room's target.
    """
    absent = trv_group[-1]
    present = [head for head in trv_group.entities if head is not absent]
    with patch(CRITICAL_GRACE, NO_GRACE):
        bt, entry = await boot_with_heads_gone(hass, trv_group, [absent])
        bt = await wait_for_startup(hass, entry)
    with patch(WRITE_BUDGET, 0.0):
        assert await room_target_reaches(hass, present, 22.0)
    await hass.async_block_till_done()

    exercising = asyncio.Event()
    release = asyncio.Event()

    async def held_exercise(*_args, **_kwargs):
        exercising.set()
        await release.wait()

    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    with patch(MAINTENANCE_EXERCISE, held_exercise), patch(WRITE_BUDGET, 0.0):
        # Not a Home Assistant task: waiting for the loop to settle must not
        # wait for the exercise, which is held open on purpose.
        run = asyncio.ensure_future(
            bt._run_valve_maintenance([head.entity_id for head in present])
        )
        await exercising.wait()
        absent.set_available(True)
        for _ in range(20):
            await hass.async_block_till_done()

        during = (
            bt.in_maintenance,
            has_adopted(bt, absent.profile),
            [(e.data["domain"], e.data["service"]) for e in events],
        )

        release.set()
        await run
        assert await wait_for(hass, lambda: has_adopted(bt, absent.profile))
        assert await wait_for(hass, lambda: absent.set_temperature_calls)
        assert bt.heat_target_temperature is not None
        assert_write_is(
            absent.set_temperature_calls[-1], bt.heat_target_temperature, absent.profile
        )

    assert during == (True, False, [])


@pytest.mark.parametrize("trv_group", [OFFSET_GROUP], indirect=True, ids=profile_id)
async def test_a_head_whose_setup_never_answers_does_not_keep_the_room_from_starting(
    hass, trv_group, caplog
):
    """A head whose setup hangs does not keep the room from starting.

    Setting a head up puts its calibration number back to zero, and the heads
    are set up one after another before the room becomes available. A write
    that never returns would keep the room unavailable, with no repair issue
    and no retry, until Home Assistant restarts. The setup is given up after
    its budget, the failure is logged, and the room starts and drives its
    heads.
    """
    hung, other = trv_group.entities
    number = hung.offset_number
    original = number.async_set_native_value
    calls: list[float] = []

    async def first_write_never_returns(value: float) -> None:
        calls.append(value)
        if len(calls) == 1:
            await asyncio.Event().wait()
        await original(value)

    number.async_set_native_value = first_write_never_returns
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    try:
        with (
            patch(INITIAL_TWEAK_BUDGET, SHORT_DEVICE_DEADLINE),
            patch(DEVICE_CALL_DEADLINE, SHORT_DEVICE_DEADLINE),
        ):
            await setup_entry(hass, entry)
            bt = await wait_for_startup(hass, entry)
            assert calls, "the setup never wrote to the calibration number"
            assert hass.states.get(BT_ENTITY).state == HVACMode.HEAT
            assert any(
                record.levelname == "ERROR"
                and "initial tweak" in record.getMessage()
                and hung.entity_id in record.getMessage()
                for record in caplog.records
            ), [record.getMessage() for record in caplog.records]
            with patch(WRITE_BUDGET, 0.0):
                assert await room_target_reaches(hass, [other], 23.0)
            assert bt.heat_target_temperature == pytest.approx(23.0)
    finally:
        del number.async_set_native_value


ADAPTER_INIT = "custom_components.better_thermostat.climate.init"
MAINTENANCE_CYCLE_SLEEP = (
    "custom_components.better_thermostat.utils.valve_maintenance.asyncio.sleep"
)


@pytest.mark.parametrize("setup", ["failed_once", "in_progress"])
@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_valve_maintenance_leaves_a_head_that_is_not_set_up_alone(
    hass, trv_group, setup
):
    """Maintenance exercises only the heads that are set up.

    A head that came back and whose setup failed or has not completed is
    still waiting for it, and nothing about it is known that the exercise
    relies on. Maintenance leaves it out, and the setup it is waiting for
    brings it into the room.
    """
    absent = trv_group[-1]
    present = [head for head in trv_group.entities if head is not absent]
    with patch(CRITICAL_GRACE, NO_GRACE):
        bt, entry = await boot_with_heads_gone(hass, trv_group, [absent])
        bt = await wait_for_startup(hass, entry)

    setting_up = asyncio.Event()
    release = asyncio.Event()

    async def failing_setup(_bt, _entity_id):
        setting_up.set()
        raise RuntimeError("adapter")

    async def slow_setup(_bt, _entity_id):
        setting_up.set()
        await release.wait()

    with patch(ADAPTER_INIT, failing_setup if setup == "failed_once" else slow_setup):
        absent.set_available(True)
        await setting_up.wait()
        if setup == "failed_once":
            assert await wait_for(
                hass,
                lambda: bt.real_trvs[absent.entity_id].failed_initialization_attempts,
            )
    assert bt.real_trvs[absent.entity_id].awaiting_initialization
    assert bt.real_trvs[absent.entity_id].failed_initialization_attempts == (
        1 if setup == "failed_once" else 0
    )

    real_sleep = asyncio.sleep

    async def no_cycle_wait(_seconds):
        await real_sleep(0)

    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    with patch(MAINTENANCE_CYCLE_SLEEP, no_cycle_wait), patch(WRITE_BUDGET, 0.0):
        await bt._run_valve_maintenance([head.entity_id for head in trv_group.entities])
        exercised = sorted(
            {
                event.data["service_data"]["entity_id"]
                for event in events
                if event.data["domain"] == CLIMATE_DOMAIN
            }
        )
        release.set()
        assert await wait_for(hass, lambda: has_adopted(bt, absent.profile))

    assert exercised == sorted(head.entity_id for head in present)
