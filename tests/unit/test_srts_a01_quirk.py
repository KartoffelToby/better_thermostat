"""Tests for the Aqara SRTS-A01 external temperature and sensor selector quirk."""

import asyncio
from collections.abc import Callable
import importlib
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, EventStateChangedData, State
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from tests.factories import (
    ThermostatStandIn,
    make_entity_registry,
    make_registry_entry,
    make_state,
)

quirk = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.SRTS-A01"
)

BT = "climate.living_room"
TRV = "climate.trv1"
INPUT = "number.trv1_external_temperature_input"
SELECTOR = "select.trv1_sensor"


def _entry(entity_id, *, translation_key=None, device_id="dev1"):
    """A registry entry on the TRV's device; its domain is the entity id's."""
    return make_registry_entry(
        entity_id, translation_key=translation_key, device_id=device_id
    )


def _thermostat(mode):
    """A state of the Better Thermostat entity on ladder rung ``mode``."""
    return State(BT, "heat", {"control_mode": str(mode)})


def _make_self(
    monkeypatch,
    *,
    selected="internal",
    entries=None,
    mode=ControlMode.OPTIMAL,
    room=21.0,
):
    """A BT stand-in whose SRTS-A01 carries the input and the sensor selector.

    ``selected`` is what the selector reports; ``None`` stands for a selector
    that publishes no state. ``mode`` is the rung the thermostat's ladder
    stands on, and ``room`` the room temperature it controls on.
    """
    mock_self = ThermostatStandIn()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.is_removed = False
    mock_self.devices_released = False
    mock_self.released_trvs = set()
    mock_self.entity_id = BT
    mock_self.room_temperature = room
    mock_self.kernel_state = make_state(control_mode=ControlModeState(mode=mode))
    registry = make_entity_registry(
        *(
            entries
            if entries is not None
            else [
                _entry(TRV),
                _entry(INPUT, translation_key="external_temperature_input"),
                _entry(SELECTOR, translation_key="sensor"),
            ]
        )
    )
    monkeypatch.setattr(quirk.er, "async_get", lambda hass: registry, raising=True)

    states = {
        INPUT: State(INPUT, "20.0", {"min": 0, "max": 55, "unit_of_measurement": "°C"}),
        BT: _thermostat(mode),
    }
    if selected is not None:
        states[SELECTOR] = _selector(selected)
    mock_self.hass.states.get = states.get
    mock_self.states = states

    async def _call(domain, service, data, **kwargs):
        # Home Assistant moves the selector once the device has taken the
        # option, after the call has returned; the watch then sees that
        # change like any other.
        if (domain, service) == ("select", "select_option"):
            confirmed = _selector(data["option"])
            states[SELECTOR] = confirmed
            loop = asyncio.get_running_loop()
            for listener in list(listeners):
                loop.call_soon(listener, _event(confirmed))

    mock_self.hass.services.async_call = AsyncMock(side_effect=_call)

    listeners: list[Callable[[Event[EventStateChangedData]], None]] = []
    watched: list[object] = []
    unsubscribed: list[object] = []
    mock_self.listeners = listeners
    mock_self.watched = watched
    mock_self.unsubscribed = unsubscribed

    def _track(hass, entity_ids, listener):
        watched.append(list(entity_ids))
        listeners.append(listener)
        return MagicMock(side_effect=lambda: unsubscribed.append(listener))

    monkeypatch.setattr(quirk, "async_track_state_change_event", _track)
    return mock_self


def _payloads(mock_self):
    """Every (domain, service, data) the quirk dispatched, in order."""
    return [
        (call.args[0], call.args[1], call.args[2])
        for call in mock_self.hass.services.async_call.await_args_list
    ]


def _event(new_state):
    """A state change of a watched entity."""
    return Event(
        EVENT_STATE_CHANGED,
        EventStateChangedData(
            entity_id=new_state.entity_id, old_state=None, new_state=new_state
        ),
    )


async def _settle():
    """Let the work a change started run to its end."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _change(mock_self, new_state):
    """Put ``new_state`` in place and deliver its change to every listener."""
    mock_self.states[new_state.entity_id] = new_state
    for listener in list(mock_self.listeners):
        listener(_event(new_state))
    await _settle()


async def _step(mock_self, mode):
    """Commit ladder rung ``mode`` and write the thermostat's state, as BT does."""
    mock_self.kernel_state = make_state(control_mode=ControlModeState(mode=mode))
    await _change(mock_self, _thermostat(mode))


def _selector(option):
    """A state of the sensor selector."""
    return State(SELECTOR, option, {"options": ["internal", "external"]})


def _to_internal():
    """The call that puts the TRV on its own sensor."""
    return ("select", "select_option", {"entity_id": SELECTOR, "option": "internal"})


def _to_external():
    """The call that puts the TRV on the input BT writes."""
    return ("select", "select_option", {"entity_id": SELECTOR, "option": "external"})


def _write(value):
    """The call that writes ``value`` into the input."""
    return ("number", "set_value", {"entity_id": INPUT, "value": value})


@pytest.fixture(autouse=True)
def _no_watch_outlives_its_test():
    """Forget every selector watch a test registered."""
    yield
    quirk._SELECTOR_WATCHES.clear()
    quirk._KEEPING.clear()
    quirk._KEEP_AGAIN.clear()
    quirk._SELECTOR_LOCKS.clear()
    quirk._PENDING_SWITCHES.clear()


class TestMaybeSetExternalTemperature:
    """The room temperature reaches the input the device regulates on."""

    async def test_the_selector_is_switched_before_the_input_is_written(
        self, monkeypatch
    ):
        """A device on its internal sensor may discard a value written first."""
        mock_self = _make_self(monkeypatch, selected="internal")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.42)

        assert _payloads(mock_self) == [_to_external(), _write(21.4)]

    async def test_the_input_is_written_again_once_the_device_confirms(
        self, monkeypatch
    ):
        """The device takes the switch seconds after the call and drops what came first."""
        mock_self = _make_self(monkeypatch, selected="internal", room=21.42)

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.42)
        await _settle()

        assert _payloads(mock_self) == [_to_external(), _write(21.4), _write(21.4)]

    async def test_a_selector_already_on_external_is_left_alone(self, monkeypatch):
        """Only the input is written while the device already reads it."""
        mock_self = _make_self(monkeypatch, selected="external")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 19.0)

        assert _payloads(mock_self) == [_write(19.0)]

    async def test_the_value_is_held_inside_the_published_range(self, monkeypatch):
        """Zigbee2MQTT publishes the input as 0 to 55 °C."""
        mock_self = _make_self(monkeypatch, selected="external")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 70.0)

        assert _payloads(mock_self)[-1] == _write(55.0)

    async def test_an_input_without_bounds_is_held_to_the_device_range(
        self, monkeypatch
    ):
        """Without published bounds the SRTS-A01 range applies, not the TRVZB one."""
        mock_self = _make_self(monkeypatch, selected="external")
        mock_self.states[INPUT] = State(INPUT, "20.0", {})

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 70.0)

        assert _payloads(mock_self)[-1] == _write(55.0)

    async def test_a_device_without_the_input_is_not_written(self, monkeypatch):
        """Nothing on the device takes the room temperature."""
        mock_self = _make_self(
            monkeypatch,
            entries=[_entry(TRV), _entry(SELECTOR, translation_key="sensor")],
        )

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0) is False

        assert _payloads(mock_self) == []

    async def test_a_refused_write_is_reported_as_declined(self, monkeypatch):
        """A device that does not take the value leaves the caller free to go on."""
        mock_self = _make_self(monkeypatch, selected="external")
        mock_self.hass.services.async_call.side_effect = HomeAssistantError("asleep")

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0) is False

    async def test_a_temperature_that_is_no_number_is_not_written(self, monkeypatch):
        """The caller is told the write was declined."""
        mock_self = _make_self(monkeypatch, selected="external")

        assert (
            await quirk.maybe_set_external_temperature(mock_self, TRV, "warm") is False
        )

        assert _payloads(mock_self) == []

    async def test_the_keepalive_under_sensor_fallback_keeps_the_own_sensor(
        self, monkeypatch
    ):
        """The input is written, but the device stays on its real reading."""
        mock_self = _make_self(
            monkeypatch, selected="internal", mode=ControlMode.SENSOR_FALLBACK
        )

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0)
        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.2)

        assert _payloads(mock_self) == [_write(21.0), _write(21.2)]

    @pytest.mark.parametrize("mode", [ControlMode.SENSOR_FALLBACK, ControlMode.HOLD])
    async def test_a_write_below_optimal_hands_the_device_its_own_sensor(
        self, monkeypatch, mode
    ):
        """The selector follows the control mode before any value is written."""
        mock_self = _make_self(monkeypatch, selected="external", mode=mode)

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0)

        assert _payloads(mock_self) == [_to_internal(), _write(21.0)]


class TestSelectorKeeping:
    """The selector follows the control mode and is put back when changed."""

    async def test_the_thermostat_and_the_selector_are_watched(self, monkeypatch):
        """The thermostat entity changes state when its ladder commits a rung."""
        mock_self = _make_self(monkeypatch, selected="external")

        quirk._watch_selector(mock_self, TRV)

        assert mock_self.watched == [[BT, SELECTOR]]

    async def test_the_watch_is_registered_once_per_thermostat(self, monkeypatch):
        """The initial tweak and every write share one listener."""
        mock_self = _make_self(monkeypatch, selected="external")
        monkeypatch.setattr(quirk, "default_initial_tweak", AsyncMock())

        await quirk.initial_tweak(mock_self, TRV)
        await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0)

        assert len(mock_self.listeners) == 1

    async def test_a_reloaded_thermostat_replaces_the_old_watch(self, monkeypatch):
        """The listener of a thermostat's old self is let go."""
        old_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(old_self, TRV)
        new_self = _make_self(monkeypatch, selected="external")

        quirk._watch_selector(new_self, TRV)

        assert old_self.unsubscribed == old_self.listeners
        assert quirk._SELECTOR_WATCHES[TRV][0] is new_self

    @pytest.mark.parametrize("mode", [ControlMode.SENSOR_FALLBACK, ControlMode.HOLD])
    async def test_leaving_optimal_hands_the_device_its_own_sensor(
        self, monkeypatch, mode
    ):
        """Every rung below OPTIMAL puts the TRV on its own sensor."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)

        await _step(mock_self, mode)

        assert _payloads(mock_self) == [_to_internal()]

    async def test_back_on_optimal_puts_the_device_on_the_input(self, monkeypatch):
        """Back on ``external``, the device is handed the room temperature."""
        mock_self = _make_self(
            monkeypatch,
            selected="internal",
            mode=ControlMode.SENSOR_FALLBACK,
            room=21.3,
        )
        quirk._watch_selector(mock_self, TRV)

        await _step(mock_self, ControlMode.OPTIMAL)

        assert _payloads(mock_self) == [_to_external(), _write(21.3)]

    async def test_back_on_optimal_without_a_room_temperature_writes_none(
        self, monkeypatch
    ):
        """A thermostat with no temperature yet only has the selector moved."""
        mock_self = _make_self(
            monkeypatch,
            selected="internal",
            mode=ControlMode.SENSOR_FALLBACK,
            room=None,
        )
        quirk._watch_selector(mock_self, TRV)

        await _step(mock_self, ControlMode.OPTIMAL)

        assert _payloads(mock_self) == [_to_external()]

    async def test_a_thermostat_change_in_line_starts_no_check(self, monkeypatch):
        """A new reading written to the entity leaves a selector in line alone."""
        mock_self = _make_self(monkeypatch, selected="external")
        task_manager = MagicMock()
        mock_self.task_manager = task_manager
        quirk._watch_selector(mock_self, TRV)

        await _step(mock_self, ControlMode.OPTIMAL)

        task_manager.create_task.assert_not_called()
        assert _payloads(mock_self) == []

    async def test_external_set_by_hand_under_sensor_fallback_is_put_back(
        self, monkeypatch
    ):
        """The device would read the stale input the room sensor no longer feeds."""
        mock_self = _make_self(
            monkeypatch, selected="internal", mode=ControlMode.SENSOR_FALLBACK
        )
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _selector("external"))

        assert _payloads(mock_self) == [_to_internal()]
        assert mock_self.states[SELECTOR].state == "internal"

    async def test_a_device_falling_back_on_its_own_is_put_back(self, monkeypatch):
        """The device goes back to the room temperature, written with it."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)

        await _change(mock_self, _selector("internal"))

        assert _payloads(mock_self) == [_to_external(), _write(21.0)]

    async def test_changes_in_a_row_switch_the_device_once(self, monkeypatch):
        """Changes arriving while a switch runs are checked once it is done."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)
        mock_self.kernel_state = make_state(
            control_mode=ControlModeState(mode=ControlMode.SENSOR_FALLBACK)
        )

        for listener in list(mock_self.listeners):
            listener(_event(_thermostat(ControlMode.SENSOR_FALLBACK)))
            listener(_event(_thermostat(ControlMode.SENSOR_FALLBACK)))
        await _settle()

        assert _payloads(mock_self) == [_to_internal()]
        assert TRV not in quirk._KEEPING

    async def test_a_write_and_a_new_rung_switch_the_device_once(self, monkeypatch):
        """Both arrive together, and the device confirms a switch seconds later.

        The selector keeps reporting ``internal`` until then, so each would
        otherwise send the switch on its own.
        """
        mock_self = _make_self(
            monkeypatch, selected="internal", mode=ControlMode.SENSOR_FALLBACK
        )
        quirk._watch_selector(mock_self, TRV)
        mock_self.hass.services.async_call = AsyncMock()
        mock_self.kernel_state = make_state(
            control_mode=ControlModeState(mode=ControlMode.OPTIMAL)
        )

        for listener in list(mock_self.listeners):
            listener(_event(_thermostat(ControlMode.OPTIMAL)))
        await asyncio.gather(
            quirk.maybe_set_external_temperature(mock_self, TRV, 22.8), _settle()
        )
        await _settle()

        switches = [call for call in _payloads(mock_self) if call[0] == "select"]
        assert switches == [_to_external()]

    async def test_no_value_is_written_before_the_device_confirms(self, monkeypatch):
        """A device still on its own sensor would drop the value."""
        mock_self = _make_self(
            monkeypatch, selected="internal", mode=ControlMode.SENSOR_FALLBACK
        )
        quirk._watch_selector(mock_self, TRV)
        mock_self.hass.services.async_call = AsyncMock()

        await _step(mock_self, ControlMode.OPTIMAL)
        assert _payloads(mock_self) == [_to_external()]

        await _change(mock_self, _selector("external"))

        assert _payloads(mock_self) == [_to_external(), _write(21.0)]
        assert TRV not in quirk._PENDING_SWITCHES

    async def test_a_switch_the_device_never_confirms_is_sent_again(self, monkeypatch):
        """Past the wait for the confirmation, the switch counts as lost."""
        monkeypatch.setattr(quirk, "_SWITCH_CONFIRM_S", 0.0)
        mock_self = _make_self(
            monkeypatch, selected="external", mode=ControlMode.SENSOR_FALLBACK
        )
        mock_self.hass.services.async_call = AsyncMock()

        await quirk._keep_selector(mock_self, TRV)
        await quirk._keep_selector(mock_self, TRV)

        assert _payloads(mock_self) == [_to_internal(), _to_internal()]

    async def test_a_confirmed_switch_is_no_longer_awaited(self, monkeypatch):
        """Once the selector reports the option, nothing waits for it."""
        mock_self = _make_self(
            monkeypatch, selected="external", mode=ControlMode.SENSOR_FALLBACK
        )

        await quirk._keep_selector(mock_self, TRV)
        assert TRV in quirk._PENDING_SWITCHES
        await quirk._keep_selector(mock_self, TRV)

        assert TRV not in quirk._PENDING_SWITCHES
        assert _payloads(mock_self) == [_to_internal()]

    async def test_a_refused_switch_is_tried_again_with_the_next_change(
        self, monkeypatch
    ):
        """A device out of reach still needs its own sensor once it is back."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)
        answer = mock_self.hass.services.async_call.side_effect
        mock_self.hass.services.async_call.side_effect = HomeAssistantError("asleep")
        await _step(mock_self, ControlMode.SENSOR_FALLBACK)
        mock_self.hass.services.async_call.side_effect = answer

        await _step(mock_self, ControlMode.SENSOR_FALLBACK)

        assert _payloads(mock_self) == [_to_internal(), _to_internal()]
        assert mock_self.states[SELECTOR].state == "internal"

    async def test_a_selector_coming_back_is_brought_in_line(self, monkeypatch):
        """A selector that is not reporting is out of reach; its return is a change."""
        mock_self = _make_self(monkeypatch, selected=None)
        quirk._watch_selector(mock_self, TRV)
        await _step(mock_self, ControlMode.SENSOR_FALLBACK)
        assert _payloads(mock_self) == []

        await _change(mock_self, _selector("external"))

        assert _payloads(mock_self) == [_to_internal()]

    async def test_a_device_without_a_selector_is_neither_watched_nor_switched(
        self, monkeypatch
    ):
        """Nothing on the device chooses the sensor."""
        mock_self = _make_self(
            monkeypatch,
            mode=ControlMode.SENSOR_FALLBACK,
            entries=[
                _entry(TRV),
                _entry(INPUT, translation_key="external_temperature_input"),
            ],
        )

        quirk._watch_selector(mock_self, TRV)
        await quirk._keep_selector(mock_self, TRV)

        assert mock_self.watched == []
        assert TRV not in quirk._SELECTOR_WATCHES
        assert _payloads(mock_self) == []

    @pytest.mark.parametrize("scope", ["room", "trv"])
    async def test_a_released_device_keeps_the_option_set_by_hand(
        self, monkeypatch, scope
    ):
        """The device is the user's: neither the selector nor the input is touched."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)
        if scope == "room":
            mock_self.devices_released = True
        else:
            mock_self.released_trvs = {TRV}

        await _change(mock_self, _selector("internal"))
        await _step(mock_self, ControlMode.SENSOR_FALLBACK)
        await _step(mock_self, ControlMode.OPTIMAL)

        assert _payloads(mock_self) == []
        assert mock_self.states[SELECTOR].state == "internal"

    async def test_the_initial_tweak_leaves_a_released_device_alone(self, monkeypatch):
        """The default tweaks skip a released TRV, and so does the selector."""
        mock_self = _make_self(
            monkeypatch, selected="external", mode=ControlMode.SENSOR_FALLBACK
        )
        mock_self.released_trvs = {TRV}
        monkeypatch.setattr(quirk, "default_initial_tweak", AsyncMock())

        await quirk.initial_tweak(mock_self, TRV)
        await _settle()

        assert _payloads(mock_self) == []

    async def test_a_removed_thermostat_stops_watching(self, monkeypatch):
        """The listener outliving its thermostat writes to no device."""
        mock_self = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(mock_self, TRV)
        mock_self.is_removed = True

        await _step(mock_self, ControlMode.SENSOR_FALLBACK)

        assert _payloads(mock_self) == []
        assert mock_self.unsubscribed == mock_self.listeners
        assert TRV not in quirk._SELECTOR_WATCHES

    async def test_the_initial_tweak_catches_a_start_on_sensor_fallback(
        self, monkeypatch
    ):
        """A thermostat starting without its room sensor writes no input at all."""
        mock_self = _make_self(
            monkeypatch, selected="external", mode=ControlMode.SENSOR_FALLBACK
        )
        default_tweak = AsyncMock()
        monkeypatch.setattr(quirk, "default_initial_tweak", default_tweak)

        await quirk.initial_tweak(mock_self, TRV)

        default_tweak.assert_awaited_once_with(mock_self, TRV)
        assert _payloads(mock_self) == [_to_internal()]
        assert TRV in quirk._SELECTOR_WATCHES

    async def test_the_initial_tweak_puts_a_device_on_optimal_on_the_input(
        self, monkeypatch
    ):
        """A device left on its own sensor goes back to the room temperature."""
        mock_self = _make_self(monkeypatch, selected="internal")
        monkeypatch.setattr(quirk, "default_initial_tweak", AsyncMock())

        await quirk.initial_tweak(mock_self, TRV)
        await _settle()

        assert _payloads(mock_self) == [_to_external(), _write(21.0)]


class TestEdges:
    """Devices and thermostats that do not look the way the quirk expects."""

    async def test_entities_without_a_translation_key_are_found_by_their_id(
        self, monkeypatch
    ):
        """An entry naming itself nothing is matched on its entity id."""
        mock_self = _make_self(
            monkeypatch,
            selected="internal",
            entries=[
                _entry(TRV),
                _entry("select.trv1_display", translation_key="display"),
                _entry("select.trv1_mode"),
                _entry(SELECTOR),
                _entry(INPUT),
            ],
        )

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0)

        assert _payloads(mock_self) == [_to_external(), _write(21.0)]

    async def test_a_trv_missing_from_the_registry_is_not_written(self, monkeypatch):
        """Without a registry entry there is no device to find siblings on."""
        mock_self = _make_self(
            monkeypatch,
            entries=[
                _entry(INPUT, translation_key="external_temperature_input"),
                _entry(SELECTOR, translation_key="sensor"),
            ],
        )

        assert await quirk.maybe_set_external_temperature(mock_self, TRV, 21.0) is False
        await quirk._keep_selector(mock_self, TRV)

        assert _payloads(mock_self) == []

    async def test_a_selector_without_the_option_is_not_switched(self, monkeypatch):
        """A selector offering other options is left as it is."""
        mock_self = _make_self(monkeypatch, selected="internal")
        mock_self.states[SELECTOR] = State(SELECTOR, "internal", {"options": ["a"]})

        assert await quirk._keep_selector(mock_self, TRV) is None

        assert _payloads(mock_self) == []
        assert TRV not in quirk._PENDING_SWITCHES

    async def test_a_selector_already_on_the_option_takes_no_call(self, monkeypatch):
        """Selecting the option the selector reports changes nothing."""
        mock_self = _make_self(monkeypatch, selected="external")

        assert await quirk._select_sensor(mock_self, TRV, SELECTOR, "external")

        assert _payloads(mock_self) == []

    async def test_back_on_external_without_an_input_writes_nothing(self, monkeypatch):
        """A device with a selector but no input only has its selector moved."""
        mock_self = _make_self(
            monkeypatch,
            selected="internal",
            entries=[_entry(TRV), _entry(SELECTOR, translation_key="sensor")],
        )

        await quirk._keep_selector_once(mock_self, TRV)

        assert _payloads(mock_self) == [_to_external()]

    async def test_a_change_during_a_check_is_checked_once_more(self, monkeypatch):
        """The run started first answers the change that arrived meanwhile."""
        mock_self = _make_self(monkeypatch, selected="external")
        checks: list[int] = []

        async def _check(self, entity_id):
            # A change arrives while the first check runs, none during the second.
            checks.append(1)
            if len(checks) == 1:
                quirk._KEEP_AGAIN.add(entity_id)

        check = AsyncMock(side_effect=_check)
        monkeypatch.setattr(quirk, "_keep_selector_once", check)
        quirk._KEEPING.add(TRV)

        await quirk._keep_selector_until_settled(mock_self, TRV)

        assert check.await_count == 2
        assert TRV not in quirk._KEEPING
        assert TRV not in quirk._KEEP_AGAIN

    async def test_a_removed_thermostat_checks_nothing(self, monkeypatch):
        """A run left behind by a removed thermostat ends without a check."""
        mock_self = _make_self(monkeypatch, selected="internal")
        mock_self.is_removed = True
        quirk._KEEPING.add(TRV)

        await quirk._keep_selector_until_settled(mock_self, TRV)

        assert _payloads(mock_self) == []
        assert TRV not in quirk._KEEPING

    async def test_a_thermostat_being_removed_starts_no_check(self, monkeypatch):
        """A task manager that starts nothing leaves no TRV marked as checked."""
        mock_self = _make_self(monkeypatch, selected="internal")
        task_manager = MagicMock()
        task_manager.create_task = MagicMock(
            side_effect=lambda coro, name: coro.close()
        )
        mock_self.task_manager = task_manager

        quirk._start_keeping(mock_self, TRV)

        assert TRV not in quirk._KEEPING

    async def test_a_check_cancelled_before_it_starts_leaves_no_mark(self, monkeypatch):
        """A mark left behind would keep a reloaded thermostat from checking."""
        mock_self = _make_self(monkeypatch, selected="internal")
        started: list[asyncio.Task[None]] = []

        def _create_task(coro, name):
            task = asyncio.get_running_loop().create_task(coro, name=name)
            started.append(task)
            return task

        task_manager = MagicMock()
        task_manager.create_task = MagicMock(side_effect=_create_task)
        mock_self.task_manager = task_manager

        quirk._start_keeping(mock_self, TRV)
        quirk._start_keeping(mock_self, TRV)
        started[0].cancel()
        await _settle()

        assert started[0].cancelled()
        assert TRV not in quirk._KEEPING
        assert TRV not in quirk._KEEP_AGAIN
        assert _payloads(mock_self) == []

    async def test_the_watch_of_another_thermostat_is_left_in_place(self, monkeypatch):
        """Only the thermostat that registered a watch takes it down."""
        owner = _make_self(monkeypatch, selected="external")
        quirk._watch_selector(owner, TRV)
        other = _make_self(monkeypatch, selected="external")

        quirk._stop_watching(other, TRV)

        assert quirk._SELECTOR_WATCHES[TRV][0] is owner
        assert owner.unsubscribed == []
