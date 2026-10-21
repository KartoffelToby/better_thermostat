"""The ``heating_blocked_by`` and ``active_overrides`` attributes.

``heating_blocked_by`` carries the block the decision kernel named, taken
from a decision and never derived in the shell. ``active_overrides`` lists
the switches held away from their default whatever the kernel decides.
"""

from dataclasses import replace
from datetime import UTC, datetime
from itertools import product
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACAction, HVACMode
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.desired import DesiredState, HeatingBlock
from custom_components.better_thermostat.core.fsm.mode import ModeState
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.snapshot import HvacMode
from custom_components.better_thermostat.utils.controlling import (
    compute_control_cycle,
    publish_heating_blocked_by,
    publish_kernel_verdict,
)
from tests.factories import (
    ThermostatStandIn,
    make_snapshot,
    make_state,
    make_state_attributes_bt,
    trv_record,
)

_CLIMATE = "custom_components.better_thermostat.climate"
_CONTROLLING = "custom_components.better_thermostat.utils.controlling"


def _extra_state_attributes(bt: BetterThermostat) -> dict[str, object]:
    """Read the property off the stand-in through the class's own getter."""
    getter = BetterThermostat.extra_state_attributes.fget
    assert getter is not None
    return getter(bt)


def _snapshot_of(bt: ThermostatStandIn):
    """build_snapshot stand-in: a heating room carrying the room's release."""
    return lambda _bt: make_snapshot(devices_released=bt.devices_released)


def _deciding_room(**state) -> ThermostatStandIn:
    """A stand-in whose control cycles run through the real kernel."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.hass = MagicMock()
    bt.kernel_state = make_state(**state)
    return bt


class TestPublishedValue:
    """The attribute publishes the block's word, or None."""

    @pytest.mark.parametrize("block", [*HeatingBlock, None])
    def test_the_block_is_published_as_its_word(self, block):
        """A plain string reaches templates and the recorder; None stays None."""
        bt = make_state_attributes_bt(heating_blocked_by=block)
        published = _extra_state_attributes(bt)["heating_blocked_by"]
        assert published == (None if block is None else block.value)
        assert published is None or type(published) is str

    @pytest.mark.parametrize(
        ("released", "paused", "detection"), list(product([False, True], repeat=3))
    )
    def test_every_held_switch_is_listed(self, released, paused, detection):
        """Each switch away from its default is listed, in a fixed order."""
        bt = make_state_attributes_bt(
            devices_released=released,
            learning_paused=paused,
            kernel_state=replace(make_state(), window_detection=detection),
        )
        expected = [
            word
            for word, held in (
                ("devices_released", released),
                ("learning_paused", paused),
                ("window_detection_off", not detection),
            )
            if held
        ]
        overrides = _extra_state_attributes(bt)["active_overrides"]
        assert isinstance(overrides, list)
        assert overrides == expected
        assert all(type(word) is str for word in overrides)

    def test_switches_are_listed_while_the_room_heats(self):
        """A paused learner and an ignored window do not block the heating.

        The block says the room heats, and the list still shows both switches,
        so a switch an automation left on does not act unseen.
        """
        bt = make_state_attributes_bt(
            heating_blocked_by=None,
            learning_paused=True,
            kernel_state=replace(make_state(), window_detection=False),
        )
        attributes = _extra_state_attributes(bt)
        assert attributes["heating_blocked_by"] is None
        assert attributes["active_overrides"] == [
            "learning_paused",
            "window_detection_off",
        ]


class TestPublishing:
    """A decision publishes its block and writes the state when it changed."""

    def test_a_changed_block_writes_the_state(self):
        """The trigger on the attribute fires with the decision."""
        bt = _deciding_room()
        publish_heating_blocked_by(
            bt, DesiredState(heating_blocked_by=HeatingBlock.WINDOW)
        )
        assert bt.heating_blocked_by is HeatingBlock.WINDOW
        bt.async_write_ha_state.assert_called_once_with()

    def test_an_unchanged_block_writes_nothing(self):
        """A decision that repeats the block leaves the state alone."""
        bt = _deciding_room()
        bt.heating_blocked_by = HeatingBlock.WINDOW
        publish_heating_blocked_by(
            bt, DesiredState(heating_blocked_by=HeatingBlock.WINDOW)
        )
        bt.async_write_ha_state.assert_not_called()

    def test_without_home_assistant_the_block_is_kept_unwritten(self):
        """Before the entity is added there is no state to write."""
        bt = _deciding_room()
        bt.hass = None
        publish_heating_blocked_by(bt, DesiredState(heating_blocked_by=None))
        publish_heating_blocked_by(
            bt, DesiredState(heating_blocked_by=HeatingBlock.DOOR)
        )
        assert bt.heating_blocked_by is HeatingBlock.DOOR
        bt.async_write_ha_state.assert_not_called()

    def test_a_control_cycle_publishes_the_kernel_block(self):
        """The cycle publishes the block of the decision it runs on."""
        bt = _deciding_room(window=WindowState(phase=WindowPhase.OPEN))
        with patch(f"{_CONTROLLING}.build_snapshot", _snapshot_of(bt)):
            _, desired = compute_control_cycle(bt)
        assert desired.heating_blocked_by is HeatingBlock.WINDOW
        assert bt.heating_blocked_by is HeatingBlock.WINDOW

    def test_a_probe_that_commits_nothing_publishes_nothing(self):
        """The reconciler's probe leaves the published block alone."""
        bt = _deciding_room(window=WindowState(phase=WindowPhase.OPEN))
        with patch(f"{_CONTROLLING}.build_snapshot", _snapshot_of(bt)):
            compute_control_cycle(bt, record=False, commit=False)
        assert bt.heating_blocked_by is None

    def test_the_kernel_verdict_is_published_without_a_cycle(self):
        """Asking the kernel publishes its block and records nothing."""
        bt = _deciding_room(window=WindowState(phase=WindowPhase.OPEN))
        before = bt.kernel_state
        with patch(f"{_CONTROLLING}.build_snapshot", _snapshot_of(bt)):
            publish_kernel_verdict(bt)
        assert bt.heating_blocked_by is HeatingBlock.WINDOW
        assert bt.kernel_state is before
        assert bt.flight_recorder.export() == []

    def test_a_failing_verdict_keeps_the_last_block(self, caplog):
        """The caller's work goes on; the failure is logged with its trace."""
        bt = _deciding_room()
        bt.heating_blocked_by = HeatingBlock.DOOR
        with (
            patch(f"{_CONTROLLING}.build_snapshot", side_effect=RuntimeError("x")),
            caplog.at_level(logging.ERROR),
        ):
            publish_kernel_verdict(bt)
        assert bt.heating_blocked_by is HeatingBlock.DOOR
        assert "heating block failed" in caplog.text
        assert caplog.records[-1].exc_info is not None


def _releasable_room() -> ThermostatStandIn:
    bt = _deciding_room()
    bt.devices_released = False
    bt.startup_running = False
    bt.clock = FakeClock()
    bt._heating_tracker = MagicMock()
    bt._loss_tracker = MagicMock()
    bt._compute_hvac_action = MagicMock(return_value=HVACAction.HEATING)
    bt._initialize_arrived_trvs = AsyncMock()
    bt._external_temperature_keepalive = AsyncMock()
    bt.control_queue_task = MagicMock()
    return bt


class TestRelease:
    """Releasing the devices runs no control cycle, so it asks the kernel."""

    @pytest.mark.asyncio
    async def test_releasing_publishes_the_block_with_the_flag(self):
        """The state that says the devices are released names the block too."""
        bt = _releasable_room()
        published = []
        bt.async_write_ha_state = MagicMock(
            side_effect=lambda: published.append(
                (bt.devices_released, bt.heating_blocked_by)
            )
        )
        with (
            patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
            patch(f"{_CONTROLLING}.build_snapshot", _snapshot_of(bt)),
        ):
            await BetterThermostat.async_set_devices_released(bt, True)

        assert bt.heating_blocked_by is HeatingBlock.DEVICES_RELEASED
        assert published[-1] == (True, HeatingBlock.DEVICES_RELEASED)
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_taking_the_devices_back_drops_the_block(self):
        """The kernel heats the room again, so the block clears at once."""
        bt = _releasable_room()
        bt.devices_released = True
        bt.heating_blocked_by = HeatingBlock.DEVICES_RELEASED
        with (
            patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
            patch(f"{_CONTROLLING}.build_snapshot", _snapshot_of(bt)),
        ):
            await BetterThermostat.async_set_devices_released(bt, False)

        blocked_by: HeatingBlock | None = bt.heating_blocked_by
        assert blocked_by is None


def _maintained_room() -> ThermostatStandIn:
    bt = _deciding_room()
    bt.devices_released = False
    bt.ignore_states = False
    bt.next_valve_maintenance = None
    bt.real_trvs = {"climate.trv": trv_record(entity_id="climate.trv")}
    bt.clock = FakeClock(monotonic_value=1000.0)
    bt.bt_hvac_mode = HVACMode.OFF
    bt._control_needed_after_maintenance = False
    bt.control_queue_task = MagicMock()
    return bt


@pytest.mark.asyncio
async def test_valve_maintenance_is_published_while_it_runs():
    """Maintenance runs no control cycle, so it asks the kernel at both ends.

    The room is off, so no control cycle follows the run either; the block
    still goes back to the kernel's word for the room once the run is over.
    """
    bt = _maintained_room()
    seen_during_run = []

    async def _exercise(*_args, **_kwargs):
        seen_during_run.append(bt.heating_blocked_by)

    snapshot = make_snapshot(hvac_mode=HvacMode.OFF)
    bt.kernel_state = replace(bt.kernel_state, mode=ModeState(hvac_mode=HvacMode.OFF))
    with (
        patch(f"{_CONTROLLING}.build_snapshot", lambda _bt: snapshot),
        patch(
            f"{_CLIMATE}.build_trv_snapshots",
            MagicMock(return_value=[SimpleNamespace(entity_id="climate.trv")]),
        ),
        patch(f"{_CLIMATE}.run_valve_maintenance", _exercise),
        patch(
            f"{_CLIMATE}.compute_next_maintenance",
            MagicMock(return_value=datetime(2026, 1, 8, tzinfo=UTC)),
        ),
    ):
        await BetterThermostat._run_valve_maintenance(bt, ["climate.trv"])

    assert seen_during_run == [HeatingBlock.VALVE_MAINTENANCE]
    assert bt.heating_blocked_by is HeatingBlock.HVAC_OFF
    bt.control_queue_task.put_nowait.assert_not_called()
