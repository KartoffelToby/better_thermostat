"""Tests for control_cooler function in utils/controlling.py."""

from time import monotonic
from unittest.mock import AsyncMock, Mock

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.utils.controlling import control_cooler


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
    min_cooler_resend_interval_s=0,
):
    """Build a minimal mock BetterThermostat instance for control_cooler tests."""
    mock_self = Mock()
    mock_self.hass = hass
    mock_self.bt_hvac_mode = bt_hvac_mode
    mock_self.cooler_entity_id = "climate.cooler"
    mock_self.context = None
    mock_self.cur_temp = cur_temp
    mock_self.bt_target_cooltemp = bt_target_cooltemp
    mock_self.bt_target_temp = bt_target_temp
    mock_self.tolerance = tolerance
    mock_self.last_sent_cooler_temp = last_sent_cooler_temp
    mock_self.last_sent_cooler_hvac_mode = last_sent_cooler_hvac_mode
    mock_self.last_sent_cooler_temp_ts = last_sent_cooler_temp_ts
    mock_self.last_sent_cooler_hvac_mode_ts = last_sent_cooler_hvac_mode_ts
    mock_self.min_cooler_resend_interval_s = min_cooler_resend_interval_s
    return mock_self


def _make_cooler_state(state=HVACMode.COOL, temperature=None):
    s = Mock()
    s.state = state
    s.attributes = {"temperature": temperature}
    return s


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

        # Provide a cooler state so the unavailable guard is not triggered
        mock_cooler_state = Mock()
        mock_cooler_state.state = HVACMode.COOL  # currently cooling
        mock_cooler_state.attributes = {"temperature": None}
        mock_hass.states.get.return_value = mock_cooler_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.OFF
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.bt_target_cooltemp = 24.0
        mock_self.context = None

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        # Should call set_temperature (current is None != desired) then set_hvac_mode OFF
        assert len(calls) == 2
        assert calls[0].args[1] == "set_temperature"
        assert calls[1].args[1] == "set_hvac_mode"
        assert calls[1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_cooling_needed_above_target(self):
        """Test cooling turns on when temp >= target_cooltemp - tolerance AND > bt_target_temp."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.cur_temp = 25.0
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

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
    async def test_cooling_not_needed_when_temp_below_bt_target(self):
        """Test cooling doesn't turn on if cur_temp <= bt_target_temp.

        The condition requires BOTH cur_temp >= target_cooltemp - tolerance
        AND cur_temp > bt_target_temp. If cur_temp <= bt_target_temp, goes to else.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.cur_temp = 20.0  # Equal to bt_target_temp
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        await control_cooler(mock_self)

        # Should call set_temperature and set_hvac_mode to OFF (else branch)
        assert mock_hass.services.async_call.call_count == 2

        calls = mock_hass.services.async_call.call_args_list
        assert calls[1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_stop_cooling_below_threshold(self):
        """Test cooling stops when temp <= target_cooltemp - tolerance."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.cur_temp = 23.0  # Below target_cooltemp - tolerance
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        # cur_temp (23.0) <= bt_target_cooltemp (24.0) - tolerance (0.5) = 23.5

        await control_cooler(mock_self)

        # Should call set_temperature and set_hvac_mode to OFF
        assert mock_hass.services.async_call.call_count == 2

        calls = mock_hass.services.async_call.call_args_list
        assert calls[1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_hysteresis_behavior(self):
        """Test hysteresis behavior between cooling thresholds.

        Temperature in the zone between (target_cooltemp - tolerance) and
        target_cooltemp, but still above bt_target_temp. The first condition
        requires cur_temp >= (target_cooltemp - tolerance), so at 23.7 >= 23.5,
        AND cur_temp > bt_target_temp (23.7 > 20.0), so it should COOL.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        # cur_temp (23.7) >= (24.0 - 0.5 = 23.5) AND cur_temp (23.7) > 20.0
        # -> first branch: COOL
        mock_self.cur_temp = 23.7

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        assert calls[-1].args[2]["hvac_mode"] == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_context_passed_to_service_calls(self):
        """Test that context is properly passed to service calls."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_context = Mock()
        mock_context.id = "test_context_id"

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.OFF
        mock_self.cooler_entity_id = "climate.cooler"
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

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.cur_temp = 25.0
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        await control_cooler(mock_self)

        # All calls should have blocking=True
        for call in mock_hass.services.async_call.call_args_list:
            assert call[1]["blocking"] is True

    @pytest.mark.asyncio
    async def test_edge_case_exactly_at_threshold(self):
        """Test behavior when temperature is exactly at threshold.

        cur_temp (23.5) >= (24.0 - 0.5 = 23.5) -> True
        cur_temp (23.5) > bt_target_temp (20.0) -> True
        -> first branch: COOL
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        # Exactly at target_cooltemp - tolerance AND above bt_target_temp
        mock_self.cur_temp = 23.5

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        # cur_temp (23.5) >= 23.5 AND cur_temp (23.5) > 20.0 -> COOL
        assert calls[-1].args[2]["hvac_mode"] == HVACMode.COOL


class TestControlCoolerSendCache:
    """Tests for the send-cache and nil-guard added in fix/cooler-equality-checks."""

    @pytest.mark.asyncio
    async def test_nil_guard_skips_set_temperature_when_current_unknown_and_unchanged(self):
        """Nil-guard: do not call set_temperature when current temp is unknown
        but the desired temp matches the last sent value."""
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
            last_sent_cooler_temp=24.0,   # already sent this value
        )

        await control_cooler(mock_self)

        service_names = [c.args[1] for c in mock_hass.services.async_call.call_args_list]
        assert "set_temperature" not in service_names

    @pytest.mark.asyncio
    async def test_nil_guard_sends_set_temperature_when_current_unknown_but_temp_changed(self):
        """Nil-guard: call set_temperature when current temp is unknown
        and the desired temp differs from the last sent value."""
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
            last_sent_cooler_temp=23.0,   # previously sent a different value
        )

        await control_cooler(mock_self)

        service_names = [c.args[1] for c in mock_hass.services.async_call.call_args_list]
        assert "set_temperature" in service_names
        temp_call = next(c for c in mock_hass.services.async_call.call_args_list if c.args[1] == "set_temperature")
        assert temp_call.args[2]["temperature"] == 24.0

    @pytest.mark.asyncio
    async def test_resend_interval_suppresses_identical_command_within_window(self):
        """Resend interval: skip set_temperature when the same value was already
        sent recently (cloud lag — current state hasn't caught up yet)."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=23.5  # lagging behind desired 24.0
        )

        mock_self = _make_mock_self(
            mock_hass,
            bt_hvac_mode=HVACMode.COOL,
            cur_temp=25.0,
            bt_target_cooltemp=24.0,
            last_sent_cooler_temp=24.0,           # same as desired — already sent
            last_sent_cooler_temp_ts=monotonic() - 5,  # sent 5 s ago
            min_cooler_resend_interval_s=60,       # suppress for 60 s
        )

        await control_cooler(mock_self)

        service_names = [c.args[1] for c in mock_hass.services.async_call.call_args_list]
        assert "set_temperature" not in service_names
