"""What the per-TRV mode cache holds once a control cycle has ended.

A control cycle is a window in which Better Thermostat drops inbound TRV
events, and it stays open for seconds while the adapters wait for their writes
to be confirmed. Each test here drives one cycle with a device that changes
mode inside that window and states what the cache owes the user afterwards.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.events.trv import trigger_trv_change
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CONF_HOMEMATICIP,
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import (
    _locked_device_moved,
    control_queue,
    read_reports_held_during_cycle,
    refresh_cached_trv_modes,
)

ENTITY_ID = "climate.test_trv"
_CTRL = "custom_components.better_thermostat.utils.controlling"
_COOLER = "custom_components.better_thermostat.events.cooler"

# The mode list of an ordinary radiator valve.
OFFERED_MODES = [HVACMode.OFF, HVACMode.HEAT]


def _reported_state(mode: str, setpoint: float = 19.0) -> State:
    """Build the state one TRV publishes."""
    return State(
        ENTITY_ID,
        mode,
        attributes={
            "current_temperature": 18.0,
            "temperature": setpoint,
            "hvac_modes": OFFERED_MODES,
        },
    )


@pytest.fixture
def reported_states() -> dict[str, State]:
    """Hold what each device publishes, so a test can change it mid-cycle."""
    return {ENTITY_ID: _reported_state("heat")}


@pytest.fixture
def thermostat(reported_states):
    """Build a Better Thermostat driving one TRV that heats."""
    bt = MagicMock()
    bt.hass = MagicMock()
    # Climate entities publish no unit attribute, so every temperature read off
    # a TRV state resolves through the system unit.
    bt.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    bt.hass.states.get.side_effect = reported_states.get
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.hvac_mode = HVACMode.HEAT
    bt.bt_target_temp = 19.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.bt_target_cooltemp = 25.0
    bt.bt_target_temp_step = 0.5
    bt.cur_temp = 18.0
    bt.tolerance = 0.3
    bt.window_open = False
    bt.contact_open = False
    bt.startup_running = False
    bt.bt_update_lock = False
    bt.in_maintenance = False
    bt.ignore_states = False
    bt.cooler_entity_id = None
    bt.context = MagicMock()  # unique context so != event.context
    bt.async_write_ha_state = MagicMock()
    bt.calculate_heating_power = AsyncMock()
    bt.calculate_heat_loss = AsyncMock()
    bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: False}}]
    bt._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(bt, **kwargs)
    )
    bt._clamp_inbound_heat_target = lambda value: (
        BetterThermostat._clamp_inbound_heat_target(bt, value)
    )
    bt.real_trvs = {
        ENTITY_ID: Trv.from_legacy_dict(
            ENTITY_ID,
            {
                "hvac_mode": "heat",
                "hvac_modes": OFFERED_MODES,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "current_temperature": 18.0,
                "temperature": 19.0,
                "last_temperature": 19.0,
                "last_hvac_mode": "heat",
                "target_temp_received": True,
                "system_mode_received": True,
                "calibration_received": True,
                "calibration": 1,
                "last_calibration": 0.0,
                "ignore_trv_states": False,
                "model": "SomeModel",
                "model_quirks": None,
                "hvac_action": "heating",
                "valve_position": 50,
                "advanced": {
                    "calibration": CalibrationType.LOCAL_BASED,
                    "calibration_mode": CalibrationMode.DEFAULT,
                    "no_off_system_mode": False,
                    "heat_auto_swapped": False,
                    "child_lock": False,
                },
            },
        )
    }
    return bt


async def _run_one_cycle(thermostat, reported_states, published_inside) -> None:
    """Drive one control cycle, with the device publishing inside it.

    ``published_inside`` is the state the TRV publishes while the cycle holds
    it, which is the window in which an event from that TRV is dropped.
    ``None`` stands for a device that publishes no state at all.
    """
    queue = asyncio.Queue()
    thermostat.control_queue_task = queue
    await queue.put(thermostat)

    async def _control_trv(*args, **kwargs) -> bool:
        assert thermostat.ignore_states is True
        if published_inside is None:
            reported_states.pop(ENTITY_ID)
        else:
            reported_states[ENTITY_ID] = published_inside
        return True

    with patch(
        "custom_components.better_thermostat.utils.controlling.control_trv",
        new=_control_trv,
    ):
        cycle = asyncio.create_task(control_queue(thermostat))
        try:
            await asyncio.wait_for(queue.join(), timeout=5)
        finally:
            cycle.cancel()
            try:
                await cycle
            except asyncio.CancelledError:
                pass


def _press_setpoint(thermostat, reported_states, setpoint: float):
    """Build the event a TRV sends when its knob was turned to ``setpoint``."""
    old_state = reported_states[ENTITY_ID]
    new_state = _reported_state(old_state.state, setpoint=setpoint)
    reported_states[ENTITY_ID] = new_state

    event = MagicMock()
    event.data = {
        "old_state": old_state,
        "new_state": new_state,
        "entity_id": ENTITY_ID,
    }
    event.context = MagicMock()  # differs from thermostat.context
    return event


class TestModeCacheAfterACycle:
    """The cached mode a TRV reports, once the cycle that hid it has ended."""

    @pytest.mark.asyncio
    async def test_a_mode_reported_inside_the_cycle_reaches_the_cache(
        self, thermostat, reported_states
    ):
        """A TRV that comes on during a cycle is cached as heating."""
        thermostat.real_trvs[ENTITY_ID].hvac_mode = "off"
        reported_states[ENTITY_ID] = _reported_state("off")

        await _run_one_cycle(thermostat, reported_states, _reported_state("heat"))

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "heat"

    @pytest.mark.asyncio
    async def test_a_setpoint_pressed_after_the_cycle_is_adopted(
        self, thermostat, reported_states
    ):
        """A knob turned after such a cycle moves the heating target.

        This is the half the user feels: the setpoint guard turns away a press
        from a device it holds as switched off, so a cache left behind by the
        cycle silently drops the press.
        """
        thermostat.real_trvs[ENTITY_ID].hvac_mode = "off"
        reported_states[ENTITY_ID] = _reported_state("off")

        await _run_one_cycle(thermostat, reported_states, _reported_state("heat"))
        await trigger_trv_change(
            thermostat, _press_setpoint(thermostat, reported_states, 22.0)
        )

        assert thermostat.bt_target_temp == 22.0

    @pytest.mark.asyncio
    async def test_a_mode_switched_during_the_cycle_is_read_on_the_next_report(
        self, thermostat, reported_states
    ):
        """A TRV switched off during a cycle switches the room off on its next report.

        The end of the cycle does not read the mode into the entity: nobody
        read that report. The cache keeps the mode Better Thermostat
        commanded, so the device's next report reaches the handler as the
        change it is, and the handler takes it as the user's.
        """
        await _run_one_cycle(thermostat, reported_states, _reported_state("off"))

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "heat"
        assert thermostat.bt_hvac_mode == HVACMode.HEAT

        old_state = reported_states[ENTITY_ID]
        event = MagicMock()
        event.data = {
            "old_state": old_state,
            "new_state": _reported_state("off"),
            "entity_id": ENTITY_ID,
        }
        event.context = MagicMock()  # differs from thermostat.context
        await trigger_trv_change(thermostat, event)

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "off"
        assert thermostat.bt_hvac_mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_an_unavailable_device_keeps_its_cached_mode(
        self, thermostat, reported_states
    ):
        """A TRV that drops off the network keeps the mode it last reported."""
        await _run_one_cycle(
            thermostat, reported_states, State(ENTITY_ID, STATE_UNAVAILABLE)
        )

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "heat"

    @pytest.mark.asyncio
    async def test_a_device_that_publishes_nothing_keeps_its_cached_mode(
        self, thermostat, reported_states
    ):
        """A TRV with no state at all keeps the mode it last reported."""
        await _run_one_cycle(thermostat, reported_states, None)

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "heat"

    @pytest.mark.asyncio
    async def test_a_child_locked_device_keeps_its_cached_mode(
        self, thermostat, reported_states
    ):
        """A child lock holds the cache as firmly as it holds the handler.

        The lock exists so that what happens at the device does not reach
        Better Thermostat, and the end of a cycle is not a way around it.
        """
        thermostat.real_trvs[ENTITY_ID].advanced["child_lock"] = True

        await _run_one_cycle(thermostat, reported_states, _reported_state("off"))

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "heat"


class TestReportsHeldDuringACycle:
    """What the end of a cycle reads of the reports the handler held off."""

    @pytest.mark.asyncio
    async def test_a_device_that_reported_is_read_once(
        self, thermostat, reported_states
    ):
        """A TRV that reported during the cycle is read once, and only it."""
        thermostat.real_trvs[ENTITY_ID].report_unread = True
        handler = AsyncMock()

        with patch(f"{_CTRL}.trigger_trv_change", new=handler):
            await read_reports_held_during_cycle(thermostat)
            await read_reports_held_during_cycle(thermostat)

        handler.assert_awaited_once()
        event = handler.await_args.args[1]
        assert event.data["new_state"] is reported_states[ENTITY_ID]
        assert event.context != thermostat.context
        assert handler.await_args.kwargs["mode_settled"] is False

    @pytest.mark.asyncio
    async def test_a_pending_mode_command_leaves_the_mode_to_the_next_report(
        self, thermostat
    ):
        """While a mode command is unconfirmed, the mode is not read at cycle end."""
        thermostat.real_trvs[ENTITY_ID].report_unread = True
        thermostat.real_trvs[ENTITY_ID].system_mode_received = False
        handler = AsyncMock()

        with patch(f"{_CTRL}.trigger_trv_change", new=handler):
            await read_reports_held_during_cycle(thermostat)

        assert handler.await_args.kwargs["mode_settled"] is True

    @pytest.mark.asyncio
    async def test_an_unavailable_device_has_nothing_to_read(
        self, thermostat, reported_states
    ):
        """A TRV that dropped off the network is not read."""
        thermostat.real_trvs[ENTITY_ID].report_unread = True
        reported_states[ENTITY_ID] = State(ENTITY_ID, STATE_UNAVAILABLE)
        handler = AsyncMock()

        with patch(f"{_CTRL}.trigger_trv_change", new=handler):
            await read_reports_held_during_cycle(thermostat)

        handler.assert_not_awaited()
        assert thermostat.real_trvs[ENTITY_ID].report_unread is False

    @pytest.mark.asyncio
    async def test_a_failing_read_leaves_the_control_loop_running(self, thermostat):
        """A report that cannot be read is logged, not raised into the queue worker."""
        thermostat.real_trvs[ENTITY_ID].report_unread = True
        handler = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch(f"{_CTRL}.trigger_trv_change", new=handler),
            patch(f"{_CTRL}._LOGGER") as logger,
        ):
            await read_reports_held_during_cycle(thermostat)

        logger.exception.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operating", [True, False])
    async def test_an_unknown_device_is_read_when_its_model_operates_so(
        self, thermostat, reported_states, operating
    ):
        """A TRV reporting ``unknown`` is read exactly when the handler reads it."""
        thermostat.real_trvs[ENTITY_ID].report_unread = True
        reported_states[ENTITY_ID] = State(ENTITY_ID, STATE_UNKNOWN)
        handler = AsyncMock()

        with (
            patch(f"{_CTRL}.trigger_trv_change", new=handler),
            patch(
                "custom_components.better_thermostat.model_fixes.model_quirks."
                "trv_state_unknown_as_available",
                return_value=operating,
            ),
        ):
            await read_reports_held_during_cycle(thermostat)

        assert handler.await_count == (1 if operating else 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("adopt", "requested"),
        [
            pytest.param(None, False, id="nothing_moved"),
            pytest.param(("bt_target_temp", 23.0), True, id="target_adopted"),
            pytest.param(("bt_hvac_mode", HVACMode.OFF), True, id="mode_adopted"),
        ],
    )
    async def test_a_cycle_is_requested_only_for_what_a_cycle_acts_on(
        self, thermostat, adopt, requested
    ):
        """Reading a held report requests a cycle only when it moved a control input.

        A report that moved nothing a cycle acts on, such as a new heating
        action, requests none: a device answering inside every cycle would
        otherwise keep one cycle following the next.
        """
        thermostat.real_trvs[ENTITY_ID].report_unread = True

        async def read(bt, event, **kwargs):
            bt.real_trvs[ENTITY_ID].hvac_action = "idle"
            if adopt is not None:
                setattr(bt, *adopt)

        with (
            patch(f"{_CTRL}.trigger_trv_change", new=AsyncMock(side_effect=read)),
            patch(f"{_CTRL}.request_control_cycle") as request,
        ):
            await read_reports_held_during_cycle(thermostat)

        assert request.called is requested

    @pytest.mark.asyncio
    async def test_an_internal_temperature_reported_during_a_cycle_requests_one(
        self, thermostat, reported_states
    ):
        """A TRV's new internal temperature reported inside a cycle is acted on.

        Outside a cycle the new reading requests one. Inside a cycle it is
        taken as it arrives, so the report read again at the end of the cycle
        carries nothing new, and the reading would wait for some other event.
        """
        old_state = reported_states[ENTITY_ID]
        warmer = State(
            ENTITY_ID,
            old_state.state,
            attributes={**old_state.attributes, "current_temperature": 19.5},
        )
        reported_states[ENTITY_ID] = warmer
        event = MagicMock()
        event.data = {
            "old_state": old_state,
            "new_state": warmer,
            "entity_id": ENTITY_ID,
        }
        event.context = MagicMock()  # differs from thermostat.context
        thermostat.ignore_states = True
        await trigger_trv_change(thermostat, event)
        thermostat.ignore_states = False

        with patch(f"{_CTRL}.request_control_cycle") as request:
            await read_reports_held_during_cycle(thermostat)

        assert thermostat.real_trvs[ENTITY_ID].current_temperature == 19.5
        assert request.called

    @pytest.mark.asyncio
    async def test_an_internal_temperature_held_back_inside_the_cycle_requests_one(
        self, thermostat, reported_states
    ):
        """A reading the debounce held back inside a cycle is acted on at its end.

        The reading arrives too soon after the previous one, so the handler
        neither takes it nor marks it as moved. By the end of the cycle that
        interval has passed, and the report read again then takes the reading,
        which outside a cycle requests one.
        """
        trv = thermostat.real_trvs[ENTITY_ID]
        trv.last_internal_sensor_change = dt_util.now()
        old_state = reported_states[ENTITY_ID]
        warmer = State(
            ENTITY_ID,
            old_state.state,
            attributes={**old_state.attributes, "current_temperature": 19.5},
        )
        reported_states[ENTITY_ID] = warmer
        event = MagicMock()
        event.data = {
            "old_state": old_state,
            "new_state": warmer,
            "entity_id": ENTITY_ID,
        }
        event.context = MagicMock()  # differs from thermostat.context
        thermostat.ignore_states = True
        await trigger_trv_change(thermostat, event)

        assert trv.current_temperature == 18.0
        assert trv.report_unread is True
        assert trv.temperature_moved_while_held is False

        trv.last_internal_sensor_change = dt_util.now() - timedelta(seconds=10)
        thermostat.ignore_states = False

        with patch(f"{_CTRL}.request_control_cycle") as request:
            await read_reports_held_during_cycle(thermostat)

        assert trv.current_temperature == 19.5
        request.assert_called_once_with(thermostat)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("commanded", ["heat", None])
    async def test_a_head_switched_on_during_a_cycle_keeps_the_room_target(
        self, thermostat, reported_states, commanded
    ):
        """A head switched on inside a cycle does not bring a setpoint turned while off.

        The head is off while the room heats, and the report that switches it
        on carries a setpoint turned while it was off. Read outside a cycle,
        that setpoint is not a press. The end of the cycle settles the mode
        cache on the mode Better Thermostat commanded before it reads the held
        report, and the report is still judged against the mode the head was
        in before it.
        """
        trv = thermostat.real_trvs[ENTITY_ID]
        trv.hvac_mode = "off"
        trv.last_hvac_mode = commanded
        previous = _reported_state("off")
        reported_states[ENTITY_ID] = _reported_state("heat", setpoint=23.0)
        event = MagicMock()
        event.data = {
            "old_state": previous,
            "new_state": reported_states[ENTITY_ID],
            "entity_id": ENTITY_ID,
        }
        event.context = MagicMock()
        thermostat.control_queue_task = MagicMock()

        thermostat.ignore_states = True
        await trigger_trv_change(thermostat, event)
        assert trv.report_unread is True
        refresh_cached_trv_modes(thermostat)
        thermostat.ignore_states = False
        with patch(f"{_CTRL}.request_control_cycle"):
            await read_reports_held_during_cycle(thermostat)

        assert thermostat.bt_target_temp == 19.0


class TestHeldReportAgainstThePreviousState:
    """A report read at cycle end is judged against the state it replaced."""

    @staticmethod
    async def _report_inside_a_cycle(thermostat, reported_states, previous, setpoint):
        """Let the TRV report ``setpoint`` while a cycle holds the handler off.

        ``previous`` is the state the report replaces. The cycle end then
        reads what the handler held off.
        """
        reported_states[ENTITY_ID] = _reported_state("heat", setpoint=setpoint)
        event = MagicMock()
        event.data = {
            "old_state": previous,
            "new_state": reported_states[ENTITY_ID],
            "entity_id": ENTITY_ID,
        }
        event.context = MagicMock()
        thermostat.control_queue_task = MagicMock()

        thermostat.ignore_states = True
        await trigger_trv_change(thermostat, event)
        assert thermostat.real_trvs[ENTITY_ID].report_unread is True
        thermostat.ignore_states = False
        with patch(f"{_CTRL}.request_control_cycle"):
            await read_reports_held_during_cycle(thermostat)

    @pytest.mark.asyncio
    async def test_a_device_coming_back_does_not_set_the_room_target(
        self, thermostat, reported_states
    ):
        """A TRV back from an outage inside a cycle leaves the room target alone.

        Its first report after ``unavailable`` carries whatever the device
        holds, such as a default it fell back to. Read outside a cycle, that
        report has no previous setpoint and is not taken as a press; read at
        cycle end it is judged the same way.
        """
        await self._report_inside_a_cycle(
            thermostat,
            reported_states,
            previous=State(ENTITY_ID, STATE_UNAVAILABLE),
            setpoint=16.0,
        )

        assert thermostat.bt_target_temp == 19.0

    @pytest.mark.asyncio
    async def test_a_knob_turned_inside_the_cycle_sets_the_room_target(
        self, thermostat, reported_states
    ):
        """A TRV that was there before the cycle and was turned in it is a press."""
        await self._report_inside_a_cycle(
            thermostat,
            reported_states,
            previous=_reported_state("heat", setpoint=19.0),
            setpoint=23.0,
        )

        assert thermostat.bt_target_temp == 23.0


class TestHeldReportsAcrossAnOutage:
    """Held reports that span a device dropping out and coming back."""

    UNAVAILABLE = State(ENTITY_ID, STATE_UNAVAILABLE)

    @staticmethod
    async def _reports_inside_a_cycle(thermostat, reported_states, states):
        """Let the TRV publish ``states`` in order while a cycle holds it off.

        Each state is reported against the one before it, the first against
        the state the TRV held when the cycle started. The cycle end then
        reads what the handler held off.
        """
        thermostat.control_queue_task = MagicMock()
        thermostat.ignore_states = True
        for state in states:
            previous = reported_states[ENTITY_ID]
            reported_states[ENTITY_ID] = state
            event = MagicMock()
            event.data = {
                "old_state": previous,
                "new_state": state,
                "entity_id": ENTITY_ID,
            }
            event.context = MagicMock()
            with patch(
                "custom_components.better_thermostat.events.trv.request_control_cycle"
            ):
                await trigger_trv_change(thermostat, event)
        thermostat.ignore_states = False
        with patch(f"{_CTRL}.request_control_cycle"):
            await read_reports_held_during_cycle(thermostat)

    @pytest.mark.asyncio
    async def test_a_return_after_an_earlier_report_does_not_set_the_room_target(
        self, thermostat, reported_states
    ):
        """A device that reported, dropped out and came back is read as a return.

        The report before the outage does not make the device's state after it
        a press: the value it came back with is whatever the device holds.
        """
        await self._reports_inside_a_cycle(
            thermostat,
            reported_states,
            [
                _reported_state("heat", setpoint=19.0),
                self.UNAVAILABLE,
                _reported_state("heat", setpoint=16.0),
            ],
        )

        assert thermostat.bt_target_temp == 19.0

    @pytest.mark.asyncio
    async def test_a_knob_turned_after_the_return_sets_the_room_target(
        self, thermostat, reported_states
    ):
        """A setpoint changed at the device after it came back is a press."""
        reported_states[ENTITY_ID] = self.UNAVAILABLE

        await self._reports_inside_a_cycle(
            thermostat,
            reported_states,
            [
                _reported_state("heat", setpoint=16.0),
                _reported_state("heat", setpoint=23.0),
            ],
        )

        assert thermostat.bt_target_temp == 23.0

    @pytest.mark.asyncio
    async def test_a_report_after_the_return_that_keeps_the_setpoint_is_no_press(
        self, thermostat, reported_states
    ):
        """A second report carrying the value the device came back with is no press."""
        reported_states[ENTITY_ID] = self.UNAVAILABLE
        returned = _reported_state("heat", setpoint=16.0)
        settled = State(
            ENTITY_ID,
            "heat",
            attributes={**returned.attributes, "current_temperature": 18.5},
        )

        await self._reports_inside_a_cycle(
            thermostat, reported_states, [returned, settled]
        )

        assert thermostat.bt_target_temp == 19.0


class TestALockedPressHeldDuringACycle:
    """A press at a child-locked TRV that the cycle held off is turned back."""

    @staticmethod
    def _lock(thermostat):
        trv = thermostat.real_trvs[ENTITY_ID]
        trv.advanced["child_lock"] = True
        trv.report_unread = True
        return trv

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "published",
        [
            pytest.param(_reported_state("heat", setpoint=25.0), id="setpoint_up"),
            pytest.param(_reported_state("heat", setpoint=16.0), id="setpoint_down"),
            pytest.param(_reported_state("off"), id="mode"),
        ],
    )
    async def test_a_locked_press_requests_a_cycle(
        self, thermostat, reported_states, published
    ):
        """The next cycle drives the device back without waiting for a tick."""
        self._lock(thermostat)
        reported_states[ENTITY_ID] = published

        with patch(f"{_CTRL}.request_control_cycle") as request:
            await read_reports_held_during_cycle(thermostat)

        request.assert_called_once_with(thermostat)
        assert thermostat.bt_target_temp == 19.0
        assert thermostat.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "published",
        [
            pytest.param(_reported_state("heat", setpoint=19.0), id="its_own_write"),
            pytest.param(_reported_state("heat", setpoint=19.2), id="within_a_step"),
        ],
    )
    async def test_a_locked_device_holding_the_write_requests_none(
        self, thermostat, reported_states, published
    ):
        """A locked device that reports what it was sent needs no cycle."""
        self._lock(thermostat)
        reported_states[ENTITY_ID] = published

        with patch(f"{_CTRL}.request_control_cycle") as request:
            await read_reports_held_during_cycle(thermostat)

        request.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_lagging_report_of_a_pending_write_requests_none(
        self, thermostat, reported_states
    ):
        """A report of the value before an unconfirmed write is not a press."""
        trv = self._lock(thermostat)
        trv.remember_setpoint_confirmed(19.0)
        trv.last_temperature = 22.0
        trv.target_temp_received = False
        reported_states[ENTITY_ID] = _reported_state("heat", setpoint=19.0)

        with patch(f"{_CTRL}.request_control_cycle") as request:
            await read_reports_held_during_cycle(thermostat)

        request.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_lagging_report_of_a_pending_mode_requests_none(
        self, thermostat, reported_states
    ):
        """A report of the mode before an unconfirmed mode command is not a press."""
        trv = self._lock(thermostat)
        trv.last_hvac_mode = "off"
        trv.system_mode_received = False
        reported_states[ENTITY_ID] = _reported_state("heat")

        with patch(f"{_CTRL}.request_control_cycle") as request:
            await read_reports_held_during_cycle(thermostat)

        request.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pressed_to", "requested"),
        [
            pytest.param(24.0, True, id="one_step_below_the_held_target"),
            pytest.param(24.5, False, id="the_held_target"),
        ],
    )
    async def test_a_dual_role_device_compares_cooling_writes_as_it_holds_them(
        self, thermostat, reported_states, pressed_to, requested
    ):
        """A locked reversible unit reads a press against its grid.

        The cooling target 24.3 is held as 24.5 on a 0.5 grid, so a press to
        24.0 is one step away from what the device was sent, and 24.5 is the
        write itself.
        """
        trv = self._lock(thermostat)
        trv.last_temperature = 21.0
        trv.last_hvac_mode = "cool"
        thermostat.bt_target_cooltemp = 24.3
        state = _reported_state("cool", setpoint=pressed_to)
        reported_states[ENTITY_ID] = state

        with (
            patch(f"{_CTRL}.dual_role_entity_id", return_value=ENTITY_ID),
            patch(f"{_COOLER}.last_sent_cooler_temperature", return_value=24.3),
        ):
            moved = _locked_device_moved(thermostat, ENTITY_ID, trv, state)

        assert moved is requested

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pressed_to", "requested"),
        [
            pytest.param(76.0, True, id="one_degree_above_the_write"),
            pytest.param(75.0, False, id="the_write"),
        ],
    )
    async def test_a_fahrenheit_dual_role_device_compares_on_the_cooling_grid(
        self, thermostat, reported_states, pressed_to, requested
    ):
        """A locked reversible unit without a published step reads whole °F.

        The cooling channel sends a cool target of 24 °C as 75 °F and caches
        the 23.89 °C it sent. A press to 76 °F, 24.44 °C, is a whole degree
        away from that write, and it lies within half a Celsius degree of the
        24 °C the cache would round to on the room's own 0.5 °C grid.
        """
        thermostat.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        trv = self._lock(thermostat)
        trv.last_temperature = 21.0
        trv.last_hvac_mode = "cool"
        thermostat.bt_target_cooltemp = 24.0
        state = _reported_state("cool", setpoint=pressed_to)
        reported_states[ENTITY_ID] = state

        with (
            patch(f"{_CTRL}.dual_role_entity_id", return_value=ENTITY_ID),
            patch(
                f"{_COOLER}.last_sent_cooler_temperature",
                return_value=(75.0 - 32.0) * 5.0 / 9.0,
            ),
        ):
            moved = _locked_device_moved(thermostat, ENTITY_ID, trv, state)

        assert moved is requested

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("held_mode", "requested"),
        [
            pytest.param("cool", False, id="the_cooling_command"),
            pytest.param("heat", True, id="the_idle_heating_command"),
        ],
    )
    async def test_a_cooled_dual_role_device_is_held_to_the_cooling_command(
        self, thermostat, reported_states, held_mode, requested
    ):
        """A locked reversible unit the cooling channel drives holds its mode.

        The cooling channel owns the device and last sent it cool, while the
        heating channel's own mode command is still heat. A device holding
        cool holds what it was sent and needs no cycle; one holding heat was
        turned away from the cooling command and is turned back.
        """
        trv = self._lock(thermostat)
        trv.last_hvac_mode = "heat"
        trv.last_temperature = 19.0
        thermostat.cooler_entity_id = ENTITY_ID
        thermostat._cooler_last_sent = {
            "hvac_mode_decided": HVACMode.COOL,
            "hvac_mode": (HVACMode.COOL, 1.0),
            "temperature": (25.0, 1.0),
        }
        state = _reported_state(held_mode, setpoint=25.0)

        moved = _locked_device_moved(thermostat, ENTITY_ID, trv, state)

        assert moved is requested

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("setpoint", "requested"),
        [
            pytest.param(19.0, False, id="holding_the_write"),
            pytest.param(25.0, True, id="setpoint_pressed"),
        ],
    )
    async def test_an_operating_unknown_report_is_read_for_its_setpoint_only(
        self, thermostat, reported_states, setpoint, requested
    ):
        """A model that reports an operating device as unknown names no mode.

        The report is read, since the model says the device operates, but
        unknown is not a mode the device was turned to; only a setpoint it
        was not sent asks for the cycle that turns it back.
        """
        self._lock(thermostat)
        reported_states[ENTITY_ID] = _reported_state(STATE_UNKNOWN, setpoint=setpoint)

        with (
            patch(
                "custom_components.better_thermostat.model_fixes.model_quirks."
                "trv_state_unknown_as_available",
                return_value=True,
            ),
            patch(f"{_CTRL}.request_control_cycle") as request,
        ):
            await read_reports_held_during_cycle(thermostat)

        assert request.called is requested
