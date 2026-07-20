"""Tests for control_cooler function in utils/controlling.py."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.snapshot import HvacMode as CoreHvacMode
from custom_components.better_thermostat.utils.controlling import (
    COOLER_RESEND_INTERVAL_S,
    control_cooler,
)
from tests.factories import make_snapshot


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
        mock_self.real_trvs = {}
        mock_self.clock = FakeClock()
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
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
    async def test_given_snapshot_is_used_without_a_rebuild(self):
        """Ensure a provided snapshot is consumed without rebuilding.

        Notes
        -----
        The control queue passes its cycle snapshot into ``control_cooler``.
        This test verifies ``build_snapshot`` is not called in that path.
        """
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()
        mock_cooler_state = Mock()
        mock_cooler_state.state = HVACMode.COOL
        mock_cooler_state.attributes = {"temperature": 24.0}
        mock_hass.states.get.return_value = mock_cooler_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.tolerance = 0.5
        mock_self.context = None

        snapshot = make_snapshot(
            hvac_mode=CoreHvacMode.OFF, target_cooltemp=24.0, tolerance=0.5
        )
        with patch(
            "custom_components.better_thermostat.utils.controlling.build_snapshot"
        ) as build:
            await control_cooler(mock_self, snapshot)

        build.assert_not_called()
        calls = mock_hass.services.async_call.call_args_list
        assert calls[-1].args[1] == "set_hvac_mode"
        assert calls[-1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_cooling_needed_above_target(self):
        """Test cooling turns on when temp >= target_cooltemp - tolerance AND > bt_target_temp."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.real_trvs = {}
        mock_self.clock = FakeClock()
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
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
        mock_self.real_trvs = {}
        mock_self.clock = FakeClock()
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
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
        mock_self.real_trvs = {}
        mock_self.clock = FakeClock()
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
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
        mock_self.real_trvs = {}
        mock_self.clock = FakeClock()
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
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
        mock_self.real_trvs = {}
        mock_self.clock = FakeClock()
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
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
        mock_self.real_trvs = {}
        mock_self.clock = FakeClock()
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
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
        mock_self.real_trvs = {}
        mock_self.clock = FakeClock()
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
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


def _make_cooler_setup(
    cooler_state=HVACMode.COOL,
    cooler_temp_attr=24.0,
    system_unit=UnitOfTemperature.CELSIUS,
    cur_temp=25.0,
    target_cooltemp=24.0,
):
    """Build a mock BT instance with a cooler in COOL demand conditions."""
    mock_hass = Mock()
    mock_hass.services = Mock()
    mock_hass.services.async_call = AsyncMock()
    mock_hass.config.units.temperature_unit = system_unit

    mock_cooler_state = Mock()
    mock_cooler_state.state = cooler_state
    mock_cooler_state.attributes = {"temperature": cooler_temp_attr}
    mock_hass.states.get.return_value = mock_cooler_state

    mock_self = Mock()
    mock_self.hass = mock_hass
    mock_self.real_trvs = {}
    mock_self.clock = FakeClock()
    mock_self.outdoor_sensor = None
    mock_self.weather_entity = None
    mock_self.bt_hvac_mode = HVACMode.COOL
    mock_self.cooler_entity_id = "climate.cooler"
    mock_self.context = None
    mock_self.cur_temp = cur_temp
    mock_self.bt_target_cooltemp = target_cooltemp
    mock_self.bt_target_temp = 20.0
    mock_self.tolerance = 0.5
    mock_self._cooler_last_sent = None
    return mock_self, mock_hass, mock_cooler_state


def _service_calls(mock_hass, service):
    return [
        call
        for call in mock_hass.services.async_call.call_args_list
        if call.args[1] == service
    ]


class TestControlCoolerSendCache:
    """Unit-correct dedup, resend throttle, and per-call error isolation."""

    @pytest.mark.asyncio
    async def test_fahrenheit_reported_temp_matching_target_is_not_resent(self):
        """A cooler reporting the target in °F triggers no set_temperature."""
        # 75.2 °F == 24.0 °C, the desired cooling setpoint.
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_temp_attr=75.2, system_unit=UnitOfTemperature.FAHRENHEIT
        )

        await control_cooler(mock_self)

        assert _service_calls(mock_hass, "set_temperature") == []

    @pytest.mark.asyncio
    async def test_identical_repeat_within_interval_is_throttled(self):
        """An identical command is not re-sent while feedback lags."""
        # The cooler keeps reporting a stale setpoint, so the naive
        # compare would re-send on every cycle.
        mock_self, mock_hass, _ = _make_cooler_setup(cooler_temp_attr=20.0)

        await control_cooler(mock_self)
        await control_cooler(mock_self)

        assert len(_service_calls(mock_hass, "set_temperature")) == 1

    @pytest.mark.asyncio
    async def test_identical_repeat_after_interval_is_resent(self):
        """After the resend interval an unconfirmed command goes out again."""
        mock_self, mock_hass, _ = _make_cooler_setup(cooler_temp_attr=20.0)

        await control_cooler(mock_self)
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)

        assert len(_service_calls(mock_hass, "set_temperature")) == 2

    @pytest.mark.asyncio
    async def test_changed_target_sends_immediately(self):
        """A changed desired value bypasses the resend interval."""
        mock_self, mock_hass, _ = _make_cooler_setup(cooler_temp_attr=20.0)

        await control_cooler(mock_self)
        mock_self.bt_target_cooltemp = 23.0
        await control_cooler(mock_self)

        temp_calls = _service_calls(mock_hass, "set_temperature")
        assert len(temp_calls) == 2
        assert temp_calls[1].args[2]["temperature"] == 23.0

    @pytest.mark.asyncio
    async def test_quantized_device_reading_is_accepted_without_resend(self):
        """A device that snaps the setpoint onto its own grid gets one send.

        The device answers a desired 22.22 with a reported 22.0. That settled
        reading counts as convergence, so the identical command is not re-sent
        even after the resend interval expires.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_temp_attr=22.0, target_cooltemp=22.22
        )

        await control_cooler(mock_self)
        # Post-send cycle observes the device's quantized reading.
        mock_self.clock.monotonic_value += 1.0
        await control_cooler(mock_self)
        # Nothing changed after the resend interval: still converged.
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)

        assert len(_service_calls(mock_hass, "set_temperature")) == 1

    @pytest.mark.asyncio
    async def test_reported_drift_after_settling_triggers_resend(self):
        """A reported value that moves off its settled reading is corrected."""
        mock_self, mock_hass, mock_cooler_state = _make_cooler_setup(
            cooler_temp_attr=22.0, target_cooltemp=22.22
        )

        await control_cooler(mock_self)
        mock_self.clock.monotonic_value += 1.0
        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_temperature")) == 1

        # The device leaves its settled reading (external change).
        mock_cooler_state.attributes = {"temperature": 24.0}
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)

        temp_calls = _service_calls(mock_hass, "set_temperature")
        assert len(temp_calls) == 2
        assert temp_calls[1].args[2]["temperature"] == 22.22

    @pytest.mark.asyncio
    async def test_changed_target_overrides_quantization_acceptance(self):
        """A new desired value sends immediately despite a settled reading."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_temp_attr=22.0, target_cooltemp=22.22
        )

        await control_cooler(mock_self)
        mock_self.clock.monotonic_value += 1.0
        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_temperature")) == 1

        mock_self.bt_target_cooltemp = 23.0
        await control_cooler(mock_self)

        temp_calls = _service_calls(mock_hass, "set_temperature")
        assert len(temp_calls) == 2
        assert temp_calls[1].args[2]["temperature"] == 23.0

    @pytest.mark.asyncio
    async def test_failed_set_temperature_still_attempts_hvac_mode(self):
        """A failing set_temperature does not suppress set_hvac_mode."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=20.0
        )

        async def _fail_set_temperature(domain, service, *args, **kwargs):
            if service == "set_temperature":
                raise HomeAssistantError("device rejected the command")

        mock_hass.services.async_call = AsyncMock(side_effect=_fail_set_temperature)

        await control_cooler(mock_self)

        mode_calls = _service_calls(mock_hass, "set_hvac_mode")
        assert len(mode_calls) == 1
        assert mode_calls[0].args[2]["hvac_mode"] == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_failed_send_does_not_prime_the_send_cache(self):
        """A failed command is retried on the next cycle despite the throttle."""
        mock_self, mock_hass, _ = _make_cooler_setup(cooler_temp_attr=20.0)

        mock_hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("device rejected the command")
        )
        await control_cooler(mock_self)

        mock_hass.services.async_call = AsyncMock()
        await control_cooler(mock_self)

        assert len(_service_calls(mock_hass, "set_temperature")) == 1

    @pytest.mark.asyncio
    async def test_connection_error_on_set_temperature_still_attempts_hvac_mode(self):
        """A raw ConnectionError from set_temperature does not skip set_hvac_mode.

        Cloud integrations can propagate non-HomeAssistantError exceptions
        through the service call; one failing channel must not abort the other.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=20.0
        )

        async def _fail_set_temperature(domain, service, *args, **kwargs):
            if service == "set_temperature":
                raise ConnectionError("cloud endpoint unreachable")

        mock_hass.services.async_call = AsyncMock(side_effect=_fail_set_temperature)

        await control_cooler(mock_self)

        mode_calls = _service_calls(mock_hass, "set_hvac_mode")
        assert len(mode_calls) == 1
        assert mode_calls[0].args[2]["hvac_mode"] == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_timeout_error_on_hvac_mode_call_does_not_propagate(self):
        """A raw TimeoutError from set_hvac_mode is logged, not raised."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=20.0
        )

        async def _fail_set_hvac_mode(domain, service, *args, **kwargs):
            if service == "set_hvac_mode":
                raise TimeoutError("cloud call timed out")

        mock_hass.services.async_call = AsyncMock(side_effect=_fail_set_hvac_mode)

        await control_cooler(mock_self)

        assert len(_service_calls(mock_hass, "set_hvac_mode")) == 1

    @pytest.mark.asyncio
    async def test_cancellation_during_service_call_propagates(self):
        """CancelledError is not swallowed by the per-call error isolation."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=20.0
        )

        mock_hass.services.async_call = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await control_cooler(mock_self)
