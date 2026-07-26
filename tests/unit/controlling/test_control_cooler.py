"""Tests for control_cooler function in utils/controlling.py."""

from time import monotonic
from unittest.mock import AsyncMock, Mock

from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
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
    """Build a Home Assistant State for the cooler entity in control_cooler tests."""
    return State("climate.cooler", str(state), {"temperature": temperature})


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
        """Test cooling turns on when temp >= target_cooltemp - tolerance AND > bt_target_temp."""
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
    async def test_cooling_not_needed_when_temp_below_bt_target(self):
        """Test cooling doesn't turn on if cur_temp <= bt_target_temp.

        The condition requires BOTH cur_temp >= target_cooltemp - tolerance
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
        """Test cooling stops when temp <= target_cooltemp - tolerance."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.COOL, temperature=20.0
        )

        # cur_temp (23.0) <= bt_target_cooltemp (24.0) - tolerance (0.5) = 23.5
        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=23.0
        )

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

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.OFF, temperature=20.0
        )

        # cur_temp (23.7) >= (24.0 - 0.5 = 23.5) AND cur_temp (23.7) > 20.0
        # -> first branch: COOL
        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=23.7
        )

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

        cur_temp (23.5) >= (24.0 - 0.5 = 23.5) -> True
        cur_temp (23.5) > bt_target_temp (20.0) -> True
        -> first branch: COOL
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_hass.states.get.return_value = _make_cooler_state(
            state=HVACMode.OFF, temperature=20.0
        )

        # Exactly at target_cooltemp - tolerance AND above bt_target_temp
        mock_self = _make_mock_self(
            mock_hass, bt_hvac_mode=HVACMode.COOL, cur_temp=23.5
        )

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        # cur_temp (23.5) >= 23.5 AND cur_temp (23.5) > 20.0 -> COOL
        assert calls[-1].args[2]["hvac_mode"] == HVACMode.COOL


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


def _make_range_cooler_state(
    state=HVACMode.COOL,
    target_temp_high=None,
    target_temp_low=None,
    supported_features=ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
):
    """Build a cooler State that advertises a target range."""
    return State(
        "climate.cooler",
        str(state),
        {
            "target_temp_high": target_temp_high,
            "target_temp_low": target_temp_low,
            "supported_features": int(supported_features),
        },
    )


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
        """A cooler that also accepts "temperature" keeps the single payload."""
        mock_hass = self._hass()
        mock_hass.states.get.return_value = _make_range_cooler_state(
            target_temp_high=28.0,
            target_temp_low=19.0,
            supported_features=ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
        )

        mock_self = _make_mock_self(mock_hass, bt_target_cooltemp=24.0)

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload == {"entity_id": "climate.cooler", "temperature": 24.0}

    @pytest.mark.asyncio
    async def test_cooler_without_feature_flags_keeps_single_setpoint(self):
        """Without advertised features the established payload is used."""
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
