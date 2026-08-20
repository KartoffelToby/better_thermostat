"""Tests for control_cooler function in utils/controlling.py."""

from time import monotonic
from unittest.mock import AsyncMock, Mock

from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.controlling import (
    COOLER_MODE_HYSTERESIS_K,
    control_cooler,
)
from custom_components.better_thermostat.utils.helpers import (
    cooling_owns_dual_role_device,
)


def _make_mock_self(
    hass,
    *,
    bt_hvac_mode=HVACMode.COOL,
    cur_temp=25.0,
    bt_target_cooltemp=24.0,
    bt_target_temp=20.0,
    tolerance=0.5,
    last_sent_cooler_temp=None,
    last_sent_cooler_hvac_mode=None,
    last_sent_cooler_temp_ts=None,
    last_sent_cooler_hvac_mode_ts=None,
    last_cooler_mode_decided=None,
    min_cooler_resend_interval_s=0,
    contact_open=False,
):
    """Build a minimal mock BetterThermostat instance for control_cooler tests."""
    mock_self = Mock()
    mock_self.hass = hass
    mock_self.bt_hvac_mode = bt_hvac_mode
    mock_self.cooler_entity_id = "climate.cooler"
    # The cooler of these cases is a device of its own, so the set of
    # controlled thermostats does not contain it.
    mock_self.real_trvs = {}
    # An attribute a Mock was never given is a truthy child mock, so
    # contact_open has to be pinned or every cycle reads as an airing.
    mock_self.contact_open = contact_open
    mock_self.context = None
    mock_self.cur_temp = cur_temp
    mock_self.bt_target_cooltemp = bt_target_cooltemp
    mock_self.bt_target_temp = bt_target_temp
    mock_self.tolerance = tolerance
    mock_self.last_sent_cooler_temp = last_sent_cooler_temp
    mock_self.last_sent_cooler_hvac_mode = last_sent_cooler_hvac_mode
    mock_self.last_sent_cooler_temp_ts = last_sent_cooler_temp_ts
    mock_self.last_sent_cooler_hvac_mode_ts = last_sent_cooler_hvac_mode_ts
    # A bare Mock() would never equal HVACMode.COOL, which silently sends every
    # hold-edge case down the switch-on branch, so the latch is pinned explicitly.
    mock_self.last_cooler_mode_decided = last_cooler_mode_decided
    mock_self.min_cooler_resend_interval_s = min_cooler_resend_interval_s
    return mock_self


def _make_cooler_state(state=HVACMode.COOL, temperature=None, target_temp_step=None):
    """Build a Home Assistant State for the cooler entity in control_cooler tests."""
    attributes = {"temperature": temperature}
    if target_temp_step is not None:
        attributes["target_temp_step"] = target_temp_step
    return State("climate.cooler", str(state), attributes)


class TestControlCooler:
    """Test control_cooler function."""

    @pytest.mark.asyncio
    async def test_off_mode_turns_cooler_off(self):
        """Test that OFF mode turns the cooler off.

        The current control_cooler sends set_temperature first (when the
        current temperature differs) and then set_hvac_mode.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=None
        )

        mock_self = _make_mock_self(mock_hass, bt_hvac_mode=HVACMode.OFF)

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        # Should call set_temperature (current is None != desired) then set_hvac_mode OFF
        assert len(calls) == 2
        assert calls[0].args[1] == "set_temperature"
        assert calls[1].args[1] == "set_hvac_mode"
        assert calls[1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_cooling_needed_above_target(self):
        """Test cooling turns on when temp >= target_cooltemp + tolerance AND > bt_target_temp."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.OFF, temperature=20.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=25.0
        )

        await control_cooler(mock_self)

        # Should call set_temperature and set_hvac_mode to COOL
        assert mock_hass.services.async_call.call_count == 2

        calls = mock_hass.services.async_call.call_args_list
        # First call: set_temperature
        assert calls[0].args[0] == "climate"
        assert calls[0].args[1] == "set_temperature"
        assert calls[0].args[2]["entity_id"] == "climate.cooler"
        assert calls[0].args[2]["temperature"] == 24.0

        # Second call: set_hvac_mode to COOL
        assert calls[1].args[0] == "climate"
        assert calls[1].args[1] == "set_hvac_mode"
        assert calls[1].args[2]["hvac_mode"] == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_unknown_cool_target_only_switches_the_cooler_off(self):
        """An unknown cool target switches a running cooler off and writes no setpoint.

        Without a cooling setpoint there is no value to send, and the mode
        decision falls through to OFF regardless of how warm the room is, so a
        cool target that never becomes known keeps a working air conditioner off.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=24.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_target_cooltemp=None,
            cur_temp=26.0,
            bt_target_temp=20.0,
            bt_hvac_mode=HVACMode.HEAT_COOL,
        )

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        assert len(calls) == 1
        assert calls[0].args[1] == "set_hvac_mode"
        assert calls[0].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_cooling_not_needed_when_temp_below_bt_target(self):
        """Test cooling doesn't turn on if cur_temp <= bt_target_temp.

        The condition requires BOTH cur_temp >= target_cooltemp + tolerance
        AND cur_temp > bt_target_temp. If cur_temp <= bt_target_temp, goes to else.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=20.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=20.0
        )

        await control_cooler(mock_self)

        # Should call set_temperature and set_hvac_mode to OFF (else branch)
        assert mock_hass.services.async_call.call_count == 2

        calls = mock_hass.services.async_call.call_args_list
        assert calls[1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_stop_cooling_below_threshold(self):
        """Test cooling stops when temp < target_cooltemp."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=20.0
        )

        # cur_temp (23.0) < bt_target_cooltemp (24.0), the hold edge
        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=23.0,
            last_cooler_mode_decided=HVACMode.COOL,
        )

        await control_cooler(mock_self)

        # Should call set_temperature and set_hvac_mode to OFF
        assert mock_hass.services.async_call.call_count == 2

        calls = mock_hass.services.async_call.call_args_list
        assert calls[1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_hysteresis_behavior(self):
        """Test hysteresis behavior between cooling thresholds.

        Temperature inside the band between target_cooltemp and
        (target_cooltemp + tolerance) and above bt_target_temp. The cooler
        reports OFF and BT holds no cooling decision, so the switch-on edge at
        24.5 has not been reached and the band must not be entered from below.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.OFF, temperature=20.0
        )

        # cur_temp (24.3) < (24.0 + 0.5 = 24.5) -> stays OFF
        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=24.3
        )

        await control_cooler(mock_self)

        mode_calls = [
            c
            for c in mock_hass.services.async_call.call_args_list
            if c.args[1] == "set_hvac_mode"
        ]
        # The cooler already reports OFF, so a decision of OFF sends nothing.
        assert mode_calls == []
        assert mock_self.last_cooler_mode_decided == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_context_passed_to_service_calls(self):
        """Test that context is properly passed to service calls."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_context = Mock()
        mock_context.id = "test_context_id"

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=20.0
        )

        mock_self = _make_mock_self(mock_hass, bt_hvac_mode=HVACMode.OFF)
        mock_self.context = mock_context

        await control_cooler(mock_self)

        # Verify context was passed
        call_kwargs = mock_hass.services.async_call.call_args[1]
        assert call_kwargs["context"] == mock_context

    @pytest.mark.asyncio
    async def test_blocking_true_for_all_calls(self):
        """Test that all service calls use blocking=True."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.OFF, temperature=20.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=25.0
        )

        await control_cooler(mock_self)

        # All calls should have blocking=True
        for call in mock_hass.services.async_call.call_args_list:
            assert call[1]["blocking"] is True

    @pytest.mark.asyncio
    async def test_edge_case_exactly_at_threshold(self):
        """Test behavior when temperature is exactly at threshold.

        cur_temp (24.5) >= (24.0 + 0.5 = 24.5) -> True
        cur_temp (24.5) > bt_target_temp (20.0) -> True
        -> first branch: COOL
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.OFF, temperature=20.0
        )

        # Exactly at target_cooltemp + tolerance AND above bt_target_temp
        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=24.5
        )

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        # cur_temp (24.5) >= 24.5 AND cur_temp (24.5) > 20.0 -> COOL
        assert calls[-1].args[2]["hvac_mode"] == HVACMode.COOL


class TestControlCoolerToleranceBand:
    """The cooling band [cool_target, cool_target + tolerance] and its latch."""

    @staticmethod
    async def _decide(
        cur_temp,
        *,
        tolerance=0.5,
        last_cooler_mode_decided=None,
        cooler_mode=HVACMode.OFF,
    ):
        """Run control_cooler once and report the decided mode.

        Returns the decided mode together with the mock instance. The reported
        cooler mode is a parameter because it seeds the hold edge while BT holds
        no decision; it defaults to OFF so the latch alone drives the band. A
        decision differing from the reported mode always produces a
        set_hvac_mode call, and no call means the cooler already reports it.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=cooler_mode, temperature=24.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=cur_temp,
            bt_target_cooltemp=24.0,
            bt_target_temp=20.0,
            tolerance=tolerance,
            last_cooler_mode_decided=last_cooler_mode_decided,
        )

        await control_cooler(mock_self)

        mode_call = next(
            (
                c
                for c in mock_hass.services.async_call.call_args_list
                if c.args[1] == "set_hvac_mode"
            ),
            None,
        )
        # No command means the cooler already reports the decided mode.
        decided = mode_call.args[2]["hvac_mode"] if mode_call else cooler_mode
        return decided, mock_self

    @pytest.mark.asyncio
    async def test_enters_band_at_cool_target_plus_tolerance(self):
        """The tolerance delays the switch-on to cool_target + tolerance."""
        mode, _ = await self._decide(24.5)
        assert mode == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_no_entry_just_below_the_switch_on_edge(self):
        """Just below the switch-on edge the cooler stays off."""
        mode, _ = await self._decide(24.49)
        assert mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_holds_inside_the_band_once_cooling(self):
        """Once COOL was decided, cooling continues down through the band."""
        mode, _ = await self._decide(24.2, last_cooler_mode_decided=HVACMode.COOL)
        assert mode == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_holds_at_the_cooling_target(self):
        """The cooling target itself is still inside the hold band."""
        mode, _ = await self._decide(24.0, last_cooler_mode_decided=HVACMode.COOL)
        assert mode == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_releases_below_the_cooling_target(self):
        """Below the cooling target the cooler is released — never cool past it."""
        mode, _ = await self._decide(23.9, last_cooler_mode_decided=HVACMode.COOL)
        assert mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_default_tolerance_switches_on_at_the_cooling_target(self):
        """With the default tolerance of 0.0 the switch-on edge sits on the target."""
        mode, _ = await self._decide(24.0, tolerance=0.0)
        assert mode == HVACMode.COOL

        mode, _ = await self._decide(23.99, tolerance=0.0)
        assert mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_latch_records_the_decision(self):
        """A COOL decision arms the latch so the next cycle uses the hold edge."""
        _, mock_self = await self._decide(24.5)
        assert mock_self.last_cooler_mode_decided == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_latch_is_released_by_an_off_decision(self):
        """An OFF decision clears the latch so the band is re-entered from above."""
        _, mock_self = await self._decide(23.9, last_cooler_mode_decided=HVACMode.COOL)
        assert mock_self.last_cooler_mode_decided == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_running_cooler_seeds_the_hold_edge(self):
        """A cooler already running keeps running while BT holds no decision.

        This is the state a restart or a config-entry reload leaves behind, and
        stopping the unit there would cost a compressor cycle and let the room
        warm back up to the switch-on edge.
        """
        mode, mock_self = await self._decide(24.3, cooler_mode=HVACMode.COOL)
        assert mode == HVACMode.COOL
        assert mock_self.last_cooler_mode_decided == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_running_cooler_is_still_released_below_the_hold_edge(self):
        """Seeding from the reported mode never cools past the cooling target."""
        mode, _ = await self._decide(23.9, cooler_mode=HVACMode.COOL)
        assert mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_own_decision_wins_over_the_reported_mode(self):
        """An externally started cooler does not reopen a band BT decided closed.

        Once the latch holds a decision, the reported mode is ignored: it lags a
        command by a state update and can be changed behind BT's back.
        """
        mode, _ = await self._decide(
            24.3, last_cooler_mode_decided=HVACMode.OFF, cooler_mode=HVACMode.COOL
        )
        assert mode == HVACMode.OFF

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reported_mode",
        [HVACMode.HEAT_COOL, HVACMode.AUTO, HVACMode.DRY, HVACMode.FAN_ONLY],
    )
    async def test_only_a_reported_cool_seeds_the_hold_edge(self, reported_mode):
        """A cooler not reporting cool is not on a run BT could take over.

        heat_cool, auto, dry and fan_only are all modes other than off, and
        none of them is a cooling run: at 24.1 — inside the band but below the
        switch-on edge at 24.5 — there is no hold edge to seed, so the band is
        entered from above like any fresh start.
        """
        mode, mock_self = await self._decide(24.1, cooler_mode=reported_mode)
        assert mode == HVACMode.OFF
        assert mock_self.last_cooler_mode_decided == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_a_reported_cool_seeds_the_hold_edge_at_the_same_temperature(self):
        """The very same room temperature holds once the cooler reports cool.

        The counterpart to the modes above: only cool distinguishes a running
        unit BT should keep running from one it should leave alone.
        """
        mode, mock_self = await self._decide(24.1, cooler_mode=HVACMode.COOL)
        assert mode == HVACMode.COOL
        assert mock_self.last_cooler_mode_decided == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_latch_survives_a_rejected_command(self):
        """A device rejecting every command must not unlatch the band.

        The latch tracks the decision, not the send, so the hold edge stays
        reachable while the cooler keeps raising errors.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("cooler offline")
        )
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.OFF, temperature=20.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=24.5
        )

        await control_cooler(mock_self)

        assert mock_self.last_sent_cooler_hvac_mode is None
        assert mock_self.last_cooler_mode_decided == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_heating_target_stays_a_hard_floor(self):
        """The heating target overrides the hold edge: cooling never fights the TRVs."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=20.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=24.0,
            bt_target_cooltemp=24.0,
            bt_target_temp=24.0,
            last_cooler_mode_decided=HVACMode.COOL,
        )

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        assert calls[-1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_none_value_guard_commands_off_and_clears_the_latch(self):
        """A missing input ends the run: it commands OFF and drops the hold edge.

        The guard stops the cooler outright, so the run is over and the way
        back in is the switch-on edge, not the hold edge the interrupted run
        was using.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=24.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=None,
            last_cooler_mode_decided=HVACMode.COOL,
        )

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        assert calls[-1].args[2]["hvac_mode"] == HVACMode.OFF
        assert mock_self.last_cooler_mode_decided == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_a_missed_reading_costs_one_band_excursion(self):
        """A blind cycle ends the run, and the room has to climb back to re-enter.

        The single OFF command the guard sends is the whole cost: the next
        cycles decide OFF again rather than flapping the cooler back on inside
        the band, and cooling resumes at the switch-on edge.
        """
        _, first = await self._decide(24.5)
        assert first.last_cooler_mode_decided == HVACMode.COOL

        blind_mode, blind = await self._decide(
            None,
            last_cooler_mode_decided=first.last_cooler_mode_decided,
            cooler_mode=HVACMode.COOL,
        )
        assert blind_mode == HVACMode.OFF
        assert blind.last_cooler_mode_decided == HVACMode.OFF

        # Back inside the band with the readings restored: the run does not
        # resume, because the hold edge went with the run that the guard ended.
        not_resumed, still_off = await self._decide(
            24.2, last_cooler_mode_decided=blind.last_cooler_mode_decided
        )
        assert not_resumed == HVACMode.OFF
        assert still_off.last_cooler_mode_decided == HVACMode.OFF

        # The switch-on edge is what lets it back in.
        resumed, _ = await self._decide(
            24.5, last_cooler_mode_decided=still_off.last_cooler_mode_decided
        )
        assert resumed == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_bt_switched_off_clears_the_latch(self):
        """BT going OFF is a decision of its own, so the next start is a fresh one.

        A deliberate stop leaves the cooler off and the room warming, which is
        the situation the switch-on edge is written for.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=24.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.OFF,
            cur_temp=24.3,
            last_cooler_mode_decided=HVACMode.COOL,
        )

        await control_cooler(mock_self)

        assert mock_self.last_cooler_mode_decided == HVACMode.OFF

        resumed, _ = await self._decide(
            24.2, last_cooler_mode_decided=mock_self.last_cooler_mode_decided
        )
        assert resumed == HVACMode.OFF

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tolerance", "switch_on", "lowest_holding"),
        [(0.3, 24.3, 24.0), (0.1, 24.1, 23.9)],
    )
    async def test_the_documented_example_decides_the_documented_edges(
        self, tolerance, switch_on, lowest_holding
    ):
        """The cooling example in docs/Configuration/configuration.md, executed.

        With a cooling target of 24.0, a tolerance of 0.3 starts cooling at 24.3
        and keeps cooling until the room is back below 24.0. A tolerance of 0.1
        is narrower than the minimum band, so the hold edge moves to 23.9 while
        the switch-on edge stays at 24.1.
        """
        entered, _ = await self._decide(switch_on, tolerance=tolerance)
        assert entered == HVACMode.COOL

        below_edge, _ = await self._decide(
            round(switch_on - 0.01, 2), tolerance=tolerance
        )
        assert below_edge == HVACMode.OFF

        holding, _ = await self._decide(
            lowest_holding, tolerance=tolerance, last_cooler_mode_decided=HVACMode.COOL
        )
        assert holding == HVACMode.COOL

        released, _ = await self._decide(
            round(lowest_holding - 0.01, 2),
            tolerance=tolerance,
            last_cooler_mode_decided=HVACMode.COOL,
        )
        assert released == HVACMode.OFF


class TestControlCoolerLatchOfAFreshThermostat:
    """The first cooling decision a newly constructed BetterThermostat makes.

    The tests above hand the latch to control_cooler; these take it from a real
    BetterThermostat instead, so the value its constructor installs is the one
    under test. That value decides what happens on the first cycle after a
    restart or a config-entry reload, when BT has made no decision yet and the
    only evidence about the cooler is what the cooler itself reports.
    """

    @staticmethod
    def _make_thermostat(hass, *, tolerance=0.3):
        """Build a real BetterThermostat and wire it for a cooling cycle.

        Everything control_cooler reads is set except the cooler latch, which
        keeps the value the constructor gave it.
        """
        thermostat = BetterThermostat(
            name="cooler band",
            heater_entity_id=[],
            sensor_entity_id=None,
            humidity_sensor_entity_id=None,
            window_id=None,
            window_delay=0,
            window_delay_after=0,
            door_id=None,
            door_delay=0,
            door_delay_after=0,
            weather_entity=None,
            outdoor_sensor=None,
            off_temperature=None,
            tolerance=tolerance,
            target_temp_min=None,
            target_temp_max=None,
            target_temp_step=None,
            model="generic",
            cooler_entity_id="climate.cooler",
            min_cooler_resend_interval=0,
            enabled_presets=None,
            unit=UnitOfTemperature.CELSIUS,
            unique_id="cooler_band",
            device_class=None,
            state_class=None,
        )
        thermostat.hass = hass
        thermostat.bt_hvac_mode = HVACMode.COOL
        thermostat.bt_target_cooltemp = 24.0
        thermostat.bt_target_temp = 20.0
        return thermostat

    @staticmethod
    def _make_hass(reported_mode):
        """Build a hass whose cooler reports a mode and BT's own setpoint."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=reported_mode, temperature=24.0
        )
        return mock_hass

    @staticmethod
    def _mode_calls(mock_hass):
        """Return the set_hvac_mode calls the cooler received."""
        return [
            call
            for call in mock_hass.services.async_call.call_args_list
            if call.args[1] == "set_hvac_mode"
        ]

    @pytest.mark.asyncio
    async def test_a_running_cooler_keeps_running_through_the_first_cycle(self):
        """A reload mid-band leaves a cooling unit cooling.

        At 24.1 the room is inside the band but below the switch-on edge at
        24.3, so the run only survives because the hold edge is seeded from the
        reported cool. A constructor that started the latch on a mode would make
        that first cycle read as a decision of BT's own, stop the unit and let
        the room warm back up to the switch-on edge.
        """
        mock_hass = self._make_hass(HVACMode.COOL)
        thermostat = self._make_thermostat(mock_hass)
        thermostat.cur_temp = 24.1

        await control_cooler(thermostat)

        assert thermostat.last_cooler_mode_decided == HVACMode.COOL
        assert self._mode_calls(mock_hass) == []

    @pytest.mark.asyncio
    async def test_a_stopped_cooler_stays_stopped_through_the_first_cycle(self):
        """The same first cycle starts nothing while the cooler reports off.

        Seeding follows the cooler, so at the same 24.1 — short of the switch-on
        edge — a unit that is not running is left alone until the room reaches
        it.
        """
        mock_hass = self._make_hass(HVACMode.OFF)
        thermostat = self._make_thermostat(mock_hass)
        thermostat.cur_temp = 24.1

        await control_cooler(thermostat)

        assert thermostat.last_cooler_mode_decided == HVACMode.OFF
        assert self._mode_calls(mock_hass) == []


class TestControlCoolerMinimumBand:
    """COOLER_MODE_HYSTERESIS_K keeps the decision band wide enough to be stable."""

    @staticmethod
    def _setup(*, tolerance, reported=HVACMode.COOL, latch=HVACMode.COOL, target=20.0):
        """Build a cooler that applies every mode it is commanded.

        The reported mode follows the commands, so a stable decision produces no
        writes at all and every flip of the decision produces one.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        state = {"mode": reported}
        mock_hass.states.get.side_effect = lambda _entity_id: _make_cooler_state(
            state=state["mode"], temperature=24.0
        )

        async def apply(_domain, service, data, **_kwargs):
            """Record a commanded mode as the cooler's reported mode."""
            if service == "set_hvac_mode":
                state["mode"] = data["hvac_mode"]

        mock_hass.services.async_call = AsyncMock(side_effect=apply)

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=24.0,
            bt_target_cooltemp=24.0,
            bt_target_temp=target,
            tolerance=tolerance,
            last_cooler_mode_decided=latch,
        )
        return mock_self, mock_hass

    @staticmethod
    def _mode_calls(mock_hass):
        """Return the set_hvac_mode calls the cooler received."""
        return [
            call
            for call in mock_hass.services.async_call.call_args_list
            if call.args[1] == "set_hvac_mode"
        ]

    @pytest.mark.asyncio
    async def test_a_tolerance_under_the_minimum_band_still_holds(self):
        """A room dithering one sensor step around the target must not be written.

        A changed desired mode bypasses the resend throttle by design, so a band
        only as wide as the tolerance turns every step across the cooling target
        into a genuine write, at whatever rate the room sensor updates. The
        cooling target is exactly where a working air conditioner parks the room.
        """
        mock_self, mock_hass = self._setup(tolerance=0.1)
        assert mock_self.tolerance < COOLER_MODE_HYSTERESIS_K

        for _ in range(5):
            for room_temp in (24.0, 24.1, 24.0, 23.9):
                mock_self.cur_temp = room_temp
                await control_cooler(mock_self)

        assert self._mode_calls(mock_hass) == []

    @pytest.mark.asyncio
    async def test_a_fahrenheit_tolerance_under_the_minimum_band_still_holds(self):
        """A tolerance configured in Fahrenheit reaches the band as Celsius.

        A configured 0.1 °F arrives as 0.0556 K, well inside the minimum band.
        """
        mock_self, mock_hass = self._setup(tolerance=round(0.1 * 5.0 / 9.0, 4))
        assert mock_self.tolerance < COOLER_MODE_HYSTERESIS_K

        for _ in range(5):
            for room_temp in (24.0, 24.1, 24.0, 23.9):
                mock_self.cur_temp = room_temp
                await control_cooler(mock_self)

        assert self._mode_calls(mock_hass) == []

    @pytest.mark.asyncio
    async def test_the_default_tolerance_borrows_the_hold_edge_from_below(self):
        """Without a configured tolerance the band collapses onto the target.

        The minimum band still applies below the target, so the decision is
        stable, but it buys decision stability and not a colder room: the
        switch-on edge stays on the cooling target.
        """
        mock_self, mock_hass = self._setup(
            tolerance=0.0, reported=HVACMode.OFF, latch=None
        )

        mock_self.cur_temp = 23.99
        await control_cooler(mock_self)
        assert self._mode_calls(mock_hass) == []

        mock_self.cur_temp = 24.0
        await control_cooler(mock_self)
        assert self._mode_calls(mock_hass)[-1].args[2]["hvac_mode"] == HVACMode.COOL

        # Inside the minimum band the hold edge is borrowed from below the target.
        mock_self.cur_temp = 23.9
        await control_cooler(mock_self)
        assert len(self._mode_calls(mock_hass)) == 1

        mock_self.cur_temp = 23.7
        await control_cooler(mock_self)
        mode_calls = self._mode_calls(mock_hass)
        assert len(mode_calls) == 2
        assert mode_calls[-1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_the_heating_target_ends_the_hold_inside_the_borrowed_band(self):
        """The heating target outranks the borrowed hold edge as well as the entry.

        The borrowed width reaches below the cooling target, so a heat/cool gap
        narrower than COOLER_MODE_HYSTERESIS_K puts the hold edge inside the
        range the TRVs are heating towards.
        """
        mock_self, mock_hass = self._setup(
            tolerance=0.0, reported=HVACMode.OFF, latch=None, target=23.9
        )

        mock_self.cur_temp = 24.0
        await control_cooler(mock_self)
        assert self._mode_calls(mock_hass)[-1].args[2]["hvac_mode"] == HVACMode.COOL

        # Still above the borrowed hold edge at 23.8, but no longer above the
        # heating target.
        mock_self.cur_temp = 23.85
        await control_cooler(mock_self)
        mode_calls = self._mode_calls(mock_hass)
        assert len(mode_calls) == 2
        assert mode_calls[-1].args[2]["hvac_mode"] == HVACMode.OFF


class TestControlCoolerSendCache:
    """Tests for the cooler send-cache, nil-guard and resend throttle."""

    @pytest.mark.asyncio
    async def test_nil_guard_skips_set_temperature_when_current_unknown_and_unchanged(
        self,
    ):
        """Skip set_temperature when current temp is unknown and desired is unchanged.

        No temperature command is sent when the reading is unavailable but the
        desired setpoint matches what was last sent to the cooler.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=None
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=25.0,
            bt_target_cooltemp=24.0,
            last_sent_cooler_temp=24.0,  # already sent this value
        )

        await control_cooler(mock_self)

        service_names = [
            c.args[1] for c in mock_hass.services.async_call.call_args_list
        ]
        assert "set_temperature" not in service_names

    @pytest.mark.asyncio
    async def test_nil_guard_sends_set_temperature_when_current_unknown_but_temp_changed(
        self,
    ):
        """Send set_temperature when current temp is unknown but desired changed.

        set_temperature is called when the reading is unavailable and the desired
        setpoint differs from what was last sent.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=None
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=25.0,
            bt_target_cooltemp=24.0,
            last_sent_cooler_temp=23.0,  # previously sent a different value
        )

        await control_cooler(mock_self)

        service_names = [
            c.args[1] for c in mock_hass.services.async_call.call_args_list
        ]
        assert "set_temperature" in service_names
        temp_call = next(
            c
            for c in mock_hass.services.async_call.call_args_list
            if c.args[1] == "set_temperature"
        )
        assert temp_call.args[2]["temperature"] == 24.0

    @pytest.mark.asyncio
    async def test_resend_interval_suppresses_identical_command_within_window(self):
        """Suppress an identical set_temperature within the rate-limit window.

        When the same setpoint was already sent recently and the cooler state has
        not yet caught up (cloud lag), the command is suppressed until the interval
        expires.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL,
            temperature=23.5,  # lagging behind desired 24.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=25.0,
            bt_target_cooltemp=24.0,
            last_sent_cooler_temp=24.0,  # same as desired — already sent
            last_sent_cooler_temp_ts=monotonic() - 5,  # sent 5 s ago
            min_cooler_resend_interval_s=60,  # suppress for 60 s
        )

        await control_cooler(mock_self)

        service_names = [
            c.args[1] for c in mock_hass.services.async_call.call_args_list
        ]
        assert "set_temperature" not in service_names

    @pytest.mark.asyncio
    async def test_service_failure_is_caught_and_not_cached(self):
        """A failing service call is caught without priming the send-cache.

        When the cooler service raises HomeAssistantError, control_cooler does not
        propagate it, and it must not record the values as sent — otherwise the
        nil-guard would suppress the retry on the next cycle.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("cooler offline")
        )
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.OFF, temperature=20.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=25.0
        )

        # Should not raise despite every service call failing.
        await control_cooler(mock_self)

        # Nothing is cached, so the next cycle retries both commands.
        assert mock_self.last_sent_cooler_temp is None
        assert mock_self.last_sent_cooler_hvac_mode is None
        assert mock_self.last_sent_cooler_temp_ts is None
        assert mock_self.last_sent_cooler_hvac_mode_ts is None


class TestControlCoolerFahrenheit:
    """Unit handling in the redundant-send dedup on Fahrenheit systems."""

    @pytest.mark.asyncio
    async def test_reported_temp_matching_target_in_fahrenheit_is_not_resent(self):
        """A cooler reporting the target in °F triggers no set_temperature.

        The reported setpoint is resolved to Celsius before the dedup
        comparison; without that, the raw °F value never equals the Celsius
        desired setpoint and a redundant set_temperature fires every cycle.
        """
        mock_hass = Mock()
        mock_hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        # 75.2 °F == 24.0 °C, the desired cooling setpoint.
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=75.2
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=25.0,
            bt_target_cooltemp=24.0,
        )

        await control_cooler(mock_self)

        service_names = [
            c.args[1] for c in mock_hass.services.async_call.call_args_list
        ]
        assert "set_temperature" not in service_names

    @pytest.mark.asyncio
    async def test_reported_temp_within_the_device_step_is_not_resent(self):
        """A setpoint snapped onto the device's °F step is unchanged.

        The device step is a °F interval, so it is worth 0.56 K: 75 °F is
        23.89 °C, which is the grid position the commanded 24.0 °C snaps to
        and not a setpoint of its own.
        """
        mock_hass = Mock()
        mock_hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=75, target_temp_step=1
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=25.0,
            bt_target_cooltemp=24.0,
        )

        await control_cooler(mock_self)

        service_names = [
            c.args[1] for c in mock_hass.services.async_call.call_args_list
        ]
        assert "set_temperature" not in service_names

    @pytest.mark.asyncio
    async def test_setpoint_change_beyond_the_device_step_is_written(self):
        """A setpoint the device cannot be holding is written immediately.

        The step tolerance only absorbs the device's own snapping; a genuine
        change has to reach the cooler on the first cycle.
        """
        mock_hass = Mock()
        mock_hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=75, target_temp_step=1
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=25.0,
            bt_target_cooltemp=22.0,
        )

        await control_cooler(mock_self)

        payload = next(
            c.args[2]
            for c in mock_hass.services.async_call.call_args_list
            if c.args[1] == "set_temperature"
        )
        assert payload == {"entity_id": "climate.cooler", "temperature": 71.6}


_ATTRIBUTE_ABSENT = object()


def _make_range_cooler_state(
    state=HVACMode.COOL,
    target_temp_high=None,
    target_temp_low=None,
    supported_features=ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
    temperature=_ATTRIBUTE_ABSENT,
    target_temp_step=None,
):
    """Build a cooler State that advertises a target range.

    ``temperature`` defaults to an absent attribute; pass None to model a
    dual-feature cooler running in range mode and a value to model one
    driving its single setpoint.
    """
    attributes = {
        "target_temp_high": target_temp_high,
        "target_temp_low": target_temp_low,
        "supported_features": int(supported_features),
    }
    if temperature is not _ATTRIBUTE_ABSENT:
        attributes["temperature"] = temperature
    if target_temp_step is not None:
        attributes["target_temp_step"] = target_temp_step
    return State("climate.cooler", str(state), attributes)


class TestControlCoolerTargetRange:
    """Payload selection for coolers that only accept a target range."""

    @staticmethod
    def _hass(unit=UnitOfTemperature.CELSIUS):
        mock_hass = Mock()
        mock_hass.config.units.temperature_unit = unit
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        return mock_hass

    @staticmethod
    def _set_temperature_payload(mock_hass):
        for call in mock_hass.services.async_call.call_args_list:
            if call.args[1] == "set_temperature":
                return call.args[2]
        return None

    @pytest.mark.asyncio
    async def test_range_only_cooler_receives_both_bounds(self):
        """A range-only cooler is written via target_temp_high/low.

        Home Assistant rejects a "temperature" payload for such an entity, so
        it would never receive a setpoint at all.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=28.0, target_temp_low=19.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=20.0
        )

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 20.0,
        }

    @pytest.mark.asyncio
    async def test_low_bound_never_exceeds_the_high_bound(self):
        """A heating target above the cooling target is capped at it."""
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=28.0, target_temp_low=19.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=26.0
        )

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload["target_temp_low"] == payload["target_temp_high"] == 24.0

    @pytest.mark.asyncio
    async def test_cooler_supporting_both_features_keeps_single_setpoint(self):
        """A dual-feature cooler driving "temperature" gets the single payload."""
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            temperature=28.0,
            target_temp_high=None,
            target_temp_low=None,
            supported_features=ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
        )

        mock_self = _make_mock_self(mock_hass, bt_target_cooltemp=24.0)

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload == {"entity_id": "climate.cooler", "temperature": 24.0}

    @pytest.mark.asyncio
    async def test_cooler_supporting_both_features_in_range_mode_gets_both_bounds(self):
        """A dual-feature cooler in range mode is written via both bounds.

        It publishes the channel it does not drive as None, so the setpoint is
        read from target_temp_high and the write has to follow that channel;
        writing "temperature" leaves the two sides permanently out of sync.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            temperature=None,
            target_temp_high=28.0,
            target_temp_low=19.0,
            supported_features=ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=20.0
        )

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 20.0,
        }

    @pytest.mark.asyncio
    async def test_cooler_without_feature_flags_keeps_single_setpoint(self):
        """Without advertised features the single-setpoint payload is used."""
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_cooler_state(temperature=28.0)

        mock_self = _make_mock_self(mock_hass, bt_target_cooltemp=24.0)

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload == {"entity_id": "climate.cooler", "temperature": 24.0}

    @pytest.mark.asyncio
    async def test_range_payload_is_converted_to_fahrenheit(self):
        """Both bounds are converted on a °F system."""
        mock_hass = self._hass(UnitOfTemperature.FAHRENHEIT)
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=82.4, target_temp_low=66.2
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=20.0
        )

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload["target_temp_high"] == 75.2  # 24.0 °C
        assert payload["target_temp_low"] == 68.0  # 20.0 °C

    @pytest.mark.asyncio
    async def test_matching_range_setpoint_is_not_resent(self):
        """The dedup reads the range key, so no redundant write is sent."""
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=24.0, target_temp_low=20.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=20.0
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) is None

    @pytest.mark.asyncio
    async def test_changed_lower_bound_alone_triggers_a_send(self):
        """A heating target that moved is written even when cooling is unchanged.

        Both bounds travel in one call, so a lower bound left behind on the
        device would persist until the cooling target happens to change.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=24.0, target_temp_low=19.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=21.0
        )

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 21.0,
        }

    @pytest.mark.asyncio
    async def test_matching_bounds_are_not_resent(self):
        """Both bounds in sync means nothing is written."""
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=24.0, target_temp_low=20.0
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=20.0
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) is None

    @pytest.mark.asyncio
    async def test_lower_bound_within_read_tolerance_is_not_resent(self):
        """A bound that only differs by the read-back grid is unchanged.

        The cooler advertises no step, so the comparison falls back to the
        base tolerance: the device reports on convert_to_float's 0.01 grid
        while BT holds the raw value, and exact inequality would resend on
        every cycle.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=24.0, target_temp_low=19.99
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=20.0
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) is None

    @pytest.mark.asyncio
    async def test_lower_bound_within_the_device_step_is_not_resent(self):
        """A lower bound snapped onto the device's step is unchanged.

        The cooler holds its bounds on a 0.5 K grid, so 20.5 is where the
        commanded 20.3 lands and rewriting it would change nothing.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=24.0, target_temp_low=20.5, target_temp_step=0.5
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=20.3
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) is None

    @pytest.mark.asyncio
    async def test_lower_bound_drift_is_not_throttled_as_a_resend(self):
        """A lower bound left behind is written despite the resend interval.

        last_sent_cooler_temp tracks the upper bound alone, so a payload armed
        by the lower bound is not an identical resend and the interval must
        not delay the user's heating-target change.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=24.0, target_temp_low=19.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_target_cooltemp=24.0,
            bt_target_temp=21.0,
            last_sent_cooler_temp=24.0,
            last_sent_cooler_temp_ts=monotonic(),
            min_cooler_resend_interval_s=300,
        )

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 21.0,
        }

    @pytest.mark.asyncio
    async def test_lower_bound_is_ignored_for_single_setpoint_coolers(self):
        """A single-setpoint cooler has no lower bound to keep in sync."""
        mock_hass = self._hass()
        mock_hass.states.get.return_value = State(
            "climate.cooler",
            str(HVACMode.COOL),
            {"temperature": 24.0, "target_temp_low": 15.0},
        )

        mock_self = _make_mock_self(
            mock_hass, bt_target_cooltemp=24.0, bt_target_temp=20.0
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) is None


class TestControlCoolerOnADualRoleEntity:
    """A cooler that is also one of the controlled thermostats.

    A reversible air conditioner named as both holds one HVAC mode and one
    setpoint for both channels, so a cycle that drives it from both leaves the
    last write standing. The cooling channel writes only while it wants to
    cool, and every other cycle belongs to the heating channel.
    """

    SHARED_ID = "climate.reversible_ac"

    @staticmethod
    def _hass():
        mock_hass = Mock()
        mock_hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        return mock_hass

    @classmethod
    def _make_shared_self(cls, hass, **kwargs):
        """Build a mock whose cooler is also a controlled thermostat."""
        mock_self = _make_mock_self(hass, **kwargs)
        mock_self.cooler_entity_id = cls.SHARED_ID
        mock_self.real_trvs = {
            cls.SHARED_ID: Trv.from_legacy_dict(
                cls.SHARED_ID,
                {
                    "hvac_modes": [
                        HVACMode.OFF,
                        HVACMode.HEAT,
                        HVACMode.COOL,
                        HVACMode.HEAT_COOL,
                    ],
                    "min_temp": 16.0,
                    "max_temp": 30.0,
                    "target_temp_received": True,
                    "system_mode_received": True,
                },
            )
        }
        return mock_self

    @classmethod
    def _make_device_state(cls, state, temperature):
        return State(cls.SHARED_ID, str(state), {"temperature": temperature})

    @pytest.mark.asyncio
    async def test_shared_entity_writes_nothing_while_the_heating_channel_owns(self):
        """A heating cycle on a shared device produces no cooling write.

        The room is cold, so the cooling decision is OFF and the heating
        channel drives the device through control_trv(). A cooling write here
        would set the device to the cooling target and switch it off, which
        the heating channel then overwrites — the mode and setpoint
        oscillation a shared device shows on every cycle.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = self._make_device_state(HVACMode.HEAT, 30.0)

        mock_self = self._make_shared_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=18.0,
            bt_target_temp=30.0,
            bt_target_cooltemp=24.0,
        )

        await control_cooler(mock_self)

        assert mock_hass.services.async_call.call_args_list == []

    @pytest.mark.asyncio
    async def test_shared_entity_still_records_the_decision_when_it_stands_down(self):
        """Standing down still latches the decision that made it stand down.

        The latch carries the cooling hysteresis band, so a cycle the cooling
        channel sits out must leave the band where the decision put it.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = self._make_device_state(HVACMode.HEAT, 30.0)

        mock_self = self._make_shared_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=18.0,
            bt_target_temp=30.0,
            bt_target_cooltemp=24.0,
            last_cooler_mode_decided=HVACMode.COOL,
        )

        await control_cooler(mock_self)

        assert mock_self.last_cooler_mode_decided == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_shared_entity_drops_the_resend_timestamps_when_it_stands_down(self):
        """The send cache stops describing a device the heating channel took.

        Keeping the timestamps would let the resend throttle suppress the first
        write of the next cooling period as a repeat of one the heating channel
        has since replaced.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = self._make_device_state(HVACMode.HEAT, 30.0)

        mock_self = self._make_shared_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=18.0,
            bt_target_temp=30.0,
            bt_target_cooltemp=24.0,
            last_sent_cooler_temp=24.0,
            last_sent_cooler_hvac_mode=HVACMode.COOL,
            last_sent_cooler_temp_ts=monotonic(),
            last_sent_cooler_hvac_mode_ts=monotonic(),
        )

        await control_cooler(mock_self)

        assert mock_self.last_sent_cooler_temp_ts is None
        assert mock_self.last_sent_cooler_hvac_mode_ts is None

    @pytest.mark.asyncio
    async def test_shared_entity_writes_as_a_cooler_when_the_cooling_channel_owns(self):
        """A cooling cycle on a shared device writes the cooling channel."""
        mock_hass = self._hass()
        mock_hass.states.get.return_value = self._make_device_state(HVACMode.HEAT, 30.0)

        mock_self = self._make_shared_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=26.0,
            bt_target_temp=20.0,
            bt_target_cooltemp=24.0,
        )

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        assert [call.args[1] for call in calls] == ["set_temperature", "set_hvac_mode"]
        assert calls[0].args[2]["temperature"] == 24.0
        assert calls[1].args[2]["hvac_mode"] == HVACMode.COOL
        assert mock_self.last_cooler_mode_decided == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_shared_entity_releases_the_heating_watchdogs_when_cooling_takes_over(
        self,
    ):
        """Taking the device over releases the heating channel's confirmations.

        Those watchdogs wait on a setpoint and a mode this write supersedes,
        and while they are outstanding the inbound handler declines every
        reported setpoint as unconfirmed.
        """
        mock_hass = self._hass()
        mock_hass.states.get.return_value = self._make_device_state(HVACMode.HEAT, 30.0)

        mock_self = self._make_shared_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=26.0,
            bt_target_temp=20.0,
            bt_target_cooltemp=24.0,
        )
        shared_trv = mock_self.real_trvs[self.SHARED_ID]
        shared_trv.target_temp_received = False
        shared_trv.system_mode_received = False

        await control_cooler(mock_self)

        assert shared_trv.target_temp_received is True
        assert shared_trv.system_mode_received is True

    @pytest.mark.asyncio
    async def test_a_distinct_cooler_writes_the_setpoint_and_the_off_mode(self):
        """A cooler of its own is untouched by the dual-role handling."""
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=30.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=18.0,
            bt_target_temp=30.0,
            bt_target_cooltemp=24.0,
        )
        mock_self.real_trvs = {"climate.radiator": Mock()}

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        assert [call.args[1] for call in calls] == ["set_temperature", "set_hvac_mode"]
        assert calls[1].args[2]["hvac_mode"] == HVACMode.OFF


class TestControlCoolerOpenContact:
    """An open window or door stops the cooler the way it stops the TRVs."""

    @pytest.mark.asyncio
    async def test_an_open_contact_stops_a_cooler_that_would_otherwise_run(self):
        """The room cannot reach its target, so the unit is switched off."""
        mock_hass = Mock()
        mock_hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=24.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=26.0,
            bt_target_temp=20.0,
            bt_target_cooltemp=24.0,
            contact_open=True,
        )

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        assert [call.args[1] for call in calls] == ["set_hvac_mode"]
        assert calls[0].args[2]["hvac_mode"] == HVACMode.OFF
        assert mock_self.last_cooler_mode_decided == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_an_open_contact_leaves_the_units_own_setpoint_alone(self):
        """The unit is held OFF, so a setpoint would only overwrite its dial."""
        mock_hass = Mock()
        mock_hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        # The unit's own dial sits at 18.0 while the cooling target is 24.0, so
        # a cycle without the contact would write the difference out.
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=18.0
        )

        # A cooling period ran before the airing, so the send cache holds the
        # setpoint that reached the unit and the moment it did.
        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=26.0,
            bt_target_temp=20.0,
            bt_target_cooltemp=24.0,
            contact_open=True,
            last_sent_cooler_temp=22.0,
            last_sent_cooler_temp_ts=1000.0,
        )

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        assert [call.args[1] for call in calls] == ["set_hvac_mode"]
        # A cycle that attempted nothing recorded nothing, so the cache still
        # describes the last setpoint the unit actually received and the
        # resend throttle keeps pacing the channel the suppression resumes.
        assert mock_self.last_sent_cooler_temp == 22.0
        assert mock_self.last_sent_cooler_temp_ts == 1000.0

    @pytest.mark.asyncio
    async def test_a_shut_contact_still_writes_the_cooling_setpoint(self):
        """The suppression lifts on the cycle the contact shuts."""
        mock_hass = Mock()
        mock_hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=18.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=26.0,
            bt_target_temp=20.0,
            bt_target_cooltemp=24.0,
            contact_open=False,
        )

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        assert [call.args[1] for call in calls] == ["set_temperature"]
        assert calls[0].args[2]["temperature"] == 24.0

    @pytest.mark.asyncio
    async def test_an_open_contact_leaves_a_shared_device_to_the_heating_channel(self):
        """The channel that switches a device off for an airing keeps it.

        A shared device is handed over by the cooling decision, so a cooling
        channel that kept running would also keep the heating channel — the
        only one that reads the contact — out of the cycle, and the unit would
        cool into an open window for as long as the room stayed warm.
        """
        mock_hass = Mock()
        mock_hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        shared_id = TestControlCoolerOnADualRoleEntity.SHARED_ID
        mock_hass.states.get.return_value = State(
            shared_id, str(HVACMode.COOL), {"temperature": 24.0}
        )

        mock_self = TestControlCoolerOnADualRoleEntity._make_shared_self(
            mock_hass,
            bt_hvac_mode=HVACMode.HEAT_COOL,
            cur_temp=26.0,
            bt_target_temp=20.0,
            bt_target_cooltemp=24.0,
            contact_open=True,
        )

        await control_cooler(mock_self)

        assert mock_hass.services.async_call.call_args_list == []
        assert mock_self.last_cooler_mode_decided == HVACMode.OFF
        assert cooling_owns_dual_role_device(mock_self, shared_id) is False
