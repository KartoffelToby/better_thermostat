"""One TRV of a room released to the user on its own, and its switch.

A released TRV is left alone like an unreachable one while the room's other
TRVs are controlled. Taking it back brings it onto the room's settings with
one control cycle.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.switch import BetterThermostatReleaseTrvSwitch
from custom_components.better_thermostat.utils.const import ATTR_STATE_RELEASED_TRVS
from custom_components.better_thermostat.utils.helpers import (
    device_released,
    every_trv_released,
)
from tests.factories import ThermostatStandIn, trv_record

_CLIMATE = "custom_components.better_thermostat.climate"
RELEASED = "climate.by_hand"
CONTROLLED = "climate.radiator"


def _host(*, devices_released=False, released_trvs=()):
    return SimpleNamespace(
        devices_released=devices_released, released_trvs=set(released_trvs)
    )


class TestTheReleaseQuestion:
    """Which device Better Thermostat leaves alone."""

    @pytest.mark.parametrize(
        ("host", "entity_id", "released"),
        [
            pytest.param(_host(), RELEASED, False, id="nothing_released"),
            pytest.param(_host(released_trvs={RELEASED}), RELEASED, True, id="trv"),
            pytest.param(
                _host(released_trvs={RELEASED}), CONTROLLED, False, id="other_trv"
            ),
            pytest.param(_host(devices_released=True), CONTROLLED, True, id="room"),
            pytest.param(_host(released_trvs={RELEASED}), None, False, id="no_device"),
        ],
    )
    def test_a_device_is_released_with_the_room_or_on_its_own(
        self, host, entity_id, released
    ):
        """The room's release covers every device, a TRV's only itself."""
        assert device_released(host, entity_id) is released

    @pytest.mark.parametrize(
        ("host", "trv_ids", "every"),
        [
            pytest.param(_host(), [RELEASED, CONTROLLED], False, id="none"),
            pytest.param(
                _host(released_trvs={RELEASED}), [RELEASED, CONTROLLED], False, id="one"
            ),
            pytest.param(
                _host(released_trvs={RELEASED, CONTROLLED}),
                [RELEASED, CONTROLLED],
                True,
                id="each",
            ),
            pytest.param(_host(devices_released=True), [CONTROLLED], True, id="room"),
            pytest.param(_host(released_trvs={RELEASED}), [], False, id="no_trv"),
        ],
    )
    def test_every_trv_is_released_only_when_none_is_left(self, host, trv_ids, every):
        """A room without TRVs has none released; the room's release has all."""
        assert every_trv_released(host, trv_ids) is every


def _room() -> ThermostatStandIn:
    """A running room with two TRVs and nothing released."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.startup_running = False
    bt.real_trvs = {
        entity_id: trv_record(entity_id=entity_id)
        for entity_id in (RELEASED, CONTROLLED)
    }
    bt._heating_tracker = MagicMock()
    bt._loss_tracker = MagicMock()
    bt._initialize_arrived_trvs = AsyncMock()
    bt._external_temperature_keepalive = AsyncMock()
    bt.control_queue_task = MagicMock()
    return bt


async def _set_released(bt, entity_id, released):
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()) as critical,
        patch(f"{_CLIMATE}.publish_kernel_verdict") as verdict,
    ):
        await BetterThermostat.async_set_trv_released(bt, entity_id, released)
    return critical, verdict


class TestTheSetter:
    """Releasing a TRV and taking it back."""

    @pytest.mark.asyncio
    async def test_releasing_holds_the_learners_and_resumes_nothing(self):
        """The room's response is partly the user's from the release on.

        The learners hold at once and give up the stretches they follow. The
        outage check runs, the kernel's block is published and the state is
        written, and nothing is sent to the TRVs.
        """
        bt = _room()

        critical, verdict = await _set_released(bt, RELEASED, True)

        assert bt.released_trvs == {RELEASED}
        assert bt.learning_held is True
        assert bt.learning_pause_changes == 1
        bt._heating_tracker.abort_cycle.assert_called_once()
        bt._loss_tracker.abort_cycle.assert_called_once()
        critical.assert_awaited_once_with(bt)
        verdict.assert_called_once_with(bt)
        bt.async_write_ha_state.assert_called_once()
        bt._initialize_arrived_trvs.assert_not_called()
        bt._external_temperature_keepalive.assert_not_called()
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_taking_it_back_resumes_with_one_cycle(self):
        """A setup left for it runs, the room temperature goes out, one cycle follows."""
        bt = _room()
        bt.released_trvs = {RELEASED, CONTROLLED}
        bt.learning_held = True

        critical, verdict = await _set_released(bt, RELEASED, False)

        assert bt.released_trvs == {CONTROLLED}
        assert bt.learning_pause_changes == 1
        critical.assert_awaited_once_with(bt)
        verdict.assert_called_once_with(bt)
        bt._initialize_arrived_trvs.assert_awaited_once_with()
        bt._external_temperature_keepalive.assert_awaited_once_with()
        bt.control_queue_task.put_nowait.assert_called_once_with(bt)
        bt._heating_tracker.abort_cycle.assert_not_called()

    @pytest.mark.parametrize(
        ("released", "entity_id"),
        [(True, RELEASED), (False, CONTROLLED), (True, "climate.not_in_this_room")],
        ids=["already_released", "already_controlled", "unknown_trv"],
    )
    @pytest.mark.asyncio
    async def test_a_command_that_changes_nothing_does_nothing(
        self, released, entity_id
    ):
        """A repeated command or one for a TRV of another room is a no-op."""
        bt = _room()
        bt.released_trvs = {RELEASED}

        critical, _ = await _set_released(bt, entity_id, released)

        assert bt.released_trvs == {RELEASED}
        assert bt.learning_pause_changes == 0
        critical.assert_not_awaited()
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.parametrize(
        ("startup_running", "devices_released"),
        [(True, False), (False, True)],
        ids=["during_startup", "room_released"],
    )
    @pytest.mark.asyncio
    async def test_taking_it_back_resumes_nothing_the_room_holds_back(
        self, startup_running, devices_released
    ):
        """A running startup controls the TRV itself; a released room none."""
        bt = _room()
        bt.released_trvs = {RELEASED}
        bt.startup_running = startup_running
        bt.devices_released = devices_released

        await _set_released(bt, RELEASED, False)

        assert bt.released_trvs == set()
        bt._initialize_arrived_trvs.assert_not_called()
        bt._external_temperature_keepalive.assert_not_called()
        bt.control_queue_task.put_nowait.assert_not_called()


class TestRestore:
    """The release is read back from the thermostat's saved state."""

    @pytest.mark.parametrize(
        ("saved", "restored"),
        [
            pytest.param(None, set(), id="no_saved_state"),
            pytest.param({}, set(), id="no_attribute"),
            pytest.param({ATTR_STATE_RELEASED_TRVS: "climate.x"}, set(), id="no_list"),
            pytest.param(
                {ATTR_STATE_RELEASED_TRVS: [RELEASED, "climate.removed", 3]},
                {RELEASED},
                id="configured_only",
            ),
        ],
    )
    def test_only_configured_trvs_come_back_released(self, saved, restored):
        """A TRV no longer configured drops out, and so does anything not a name."""
        bt = ThermostatStandIn()
        bt.all_trvs = [{"trv": RELEASED}, {"trv": CONTROLLED}]
        bt._saved_state = (
            None if saved is None else State("climate.bt", "heat", attributes=saved)
        )

        assert BetterThermostat._restored_released_trvs(bt) == restored


class TestAvailability:
    """A released TRV keeps the room available, as the room's release does."""

    @pytest.mark.parametrize(
        ("released_trvs", "available"),
        [(set(), False), ({RELEASED}, True)],
        ids=["none_released", "one_released"],
    )
    def test_a_room_whose_trvs_are_away_stays_available_with_one_released(
        self, released_trvs, available
    ):
        """Every TRV is away; one released on its own keeps the room available."""
        bt = _room()
        bt._available = True
        bt.released_trvs = released_trvs
        getter = BetterThermostat.available.fget
        assert getter is not None
        with patch(f"{_CLIMATE}.is_trv_available", return_value=False):
            assert getter(bt) is available


class TestSetup:
    """Setting a TRV up writes to it, and a released TRV gets no writes."""

    @pytest.mark.asyncio
    async def test_only_the_controlled_trv_is_set_up(self):
        """The released TRV waits for its setup until it is taken back."""
        bt = _room()
        bt.hass = MagicMock()
        bt.hass.states.get.return_value = None
        bt.all_entities = dict.fromkeys(bt.real_trvs)
        bt._configured_temperature_step = None
        bt.bt_target_temperature_step = None
        bt.released_trvs = {RELEASED}

        with (
            patch(f"{_CLIMATE}.init", autospec=True) as init,
            patch(f"{_CLIMATE}.initial_tweak", autospec=True) as tweak,
        ):
            await BetterThermostat._initialize_trvs(bt)

        assert [call.args[1] for call in init.await_args_list] == [CONTROLLED]
        assert [call.args[1] for call in tweak.await_args_list] == [CONTROLLED]
        assert bt.real_trvs[RELEASED].awaiting_initialization is True
        assert bt.real_trvs[CONTROLLED].awaiting_initialization is False

    @pytest.mark.asyncio
    async def test_a_released_trv_that_arrives_is_not_set_up(self):
        """Back on the air, a released TRV waits; a controlled one is set up."""
        bt = _room()
        bt.is_removed = False
        bt.in_maintenance = False
        bt._trvs_initializing = set()
        bt.released_trvs = {RELEASED}
        for trv in bt.real_trvs.values():
            trv.awaiting_initialization = True
        bt._initialize_trvs = AsyncMock(return_value=set())
        bt._collect_trv_states = MagicMock(return_value=[])
        bt._bound_target_to_range = MagicMock(side_effect=lambda value: value)
        bt.heat_target_temperature = 21.0
        bt.cool_target_temperature = None

        with (
            patch(f"{_CLIMATE}.is_trv_available", return_value=True),
            patch(f"{_CLIMATE}.request_control_cycle"),
        ):
            await BetterThermostat._initialize_arrived_trvs(bt)

        bt._initialize_trvs.assert_awaited_once_with([CONTROLLED])
        assert bt.real_trvs[RELEASED].awaiting_initialization is True

    @pytest.mark.asyncio
    async def test_a_trv_released_while_it_is_set_up_waits_for_its_take_back(self):
        """Released during its own setup, it stays awaiting; the next one is set up."""
        bt = _room()
        bt.is_removed = False
        bt.in_maintenance = False
        bt._trvs_initializing = set()
        for trv in bt.real_trvs.values():
            trv.awaiting_initialization = True

        async def first_released_meanwhile(entity_ids):
            if entity_ids == [RELEASED]:
                bt.released_trvs = {RELEASED}
            return set()

        bt._initialize_trvs = AsyncMock(side_effect=first_released_meanwhile)
        bt._collect_trv_states = MagicMock(return_value=[])
        bt._bound_target_to_range = MagicMock(side_effect=lambda value: value)
        bt.heat_target_temperature = 21.0
        bt.cool_target_temperature = None

        with (
            patch(f"{_CLIMATE}.is_trv_available", return_value=True),
            patch(f"{_CLIMATE}.request_control_cycle"),
        ):
            await BetterThermostat._initialize_arrived_trvs(bt)

        assert [call.args[0] for call in bt._initialize_trvs.await_args_list] == [
            [RELEASED],
            [CONTROLLED],
        ]
        assert bt.real_trvs[RELEASED].awaiting_initialization is True
        assert bt.real_trvs[CONTROLLED].awaiting_initialization is False


def _release_switch(
    *, released_trvs: set[str], show_trv_name: bool = True
) -> tuple[BetterThermostatReleaseTrvSwitch, AsyncMock]:
    bt_climate = ThermostatStandIn()
    bt_climate.unique_id = "bt_1"
    bt_climate.hass = MagicMock()
    bt_climate.hass.states.get.return_value = State(
        RELEASED, "heat", attributes={"friendly_name": "Bathroom radiator"}
    )
    bt_climate.released_trvs = released_trvs
    setter = AsyncMock()
    bt_climate.async_set_trv_released = setter
    switch = BetterThermostatReleaseTrvSwitch(bt_climate, RELEASED, show_trv_name)
    switch.async_write_ha_state = MagicMock()
    return switch, setter


class TestTheSwitch:
    """The per-TRV switch shows and sets the thermostat's release of its TRV."""

    @pytest.mark.parametrize(
        ("released_trvs", "is_on"),
        [({RELEASED}, True), ({CONTROLLED}, False), (set(), False)],
        ids=["this_trv", "other_trv", "none"],
    )
    def test_the_switch_shows_its_trvs_release(self, released_trvs, is_on):
        """Only its own TRV's release turns the switch on."""
        switch, _ = _release_switch(released_trvs=released_trvs)

        assert switch.is_on is is_on
        assert switch.unique_id == f"bt_1_{RELEASED}_release"

    @pytest.mark.parametrize(
        ("show_trv_name", "translation_key", "placeholders"),
        [
            (True, "release_trv", {"trv_name": "Bathroom radiator"}),
            (False, "release_trv_no_trv", None),
        ],
    )
    def test_the_switch_is_named_like_the_other_trv_switches(
        self, show_trv_name, translation_key, placeholders
    ):
        """It carries the TRV's name in a room of several TRVs."""
        switch, _ = _release_switch(released_trvs=set(), show_trv_name=show_trv_name)

        assert switch.translation_key == translation_key
        assert getattr(switch, "_attr_translation_placeholders", None) == placeholders

    @pytest.mark.parametrize("turn_on", [True, False])
    @pytest.mark.asyncio
    async def test_the_switch_sets_its_trvs_release(self, turn_on):
        """On releases its TRV, off takes it back."""
        switch, setter = _release_switch(released_trvs=set())

        if turn_on:
            await switch.async_turn_on()
        else:
            await switch.async_turn_off()

        setter.assert_awaited_once_with(RELEASED, turn_on)
        switch.async_write_ha_state.assert_called_once()
