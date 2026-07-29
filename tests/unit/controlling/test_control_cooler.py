"""Tests for control_cooler function in utils/controlling.py."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.snapshot import HvacMode as CoreHvacMode
from custom_components.better_thermostat.utils.controlling import (
    COOLER_FAILURE_BACKOFF_BASE_S,
    COOLER_FAILURE_BACKOFF_MAX_RUN,
    COOLER_FAILURE_BACKOFF_MAX_S,
    COOLER_RESEND_INTERVAL_S,
    control_cooler,
    last_sent_cooler_temperature,
)
from tests.factories import make_snapshot


def _mock_cooler_state(state=HVACMode.COOL):
    """Build a cooler state whose attributes read like a real entity's."""
    mock_cooler_state = Mock()
    mock_cooler_state.state = state
    mock_cooler_state.attributes = {"temperature": None}
    return mock_cooler_state


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
        mock_hass.states.get.return_value = _mock_cooler_state(HVACMode.OFF)

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
        mock_hass.states.get.return_value = _mock_cooler_state()

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
        mock_hass.states.get.return_value = _mock_cooler_state()

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
        mock_hass.states.get.return_value = _mock_cooler_state(HVACMode.OFF)

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

        mock_hass.states.get.return_value = _mock_cooler_state()

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
        mock_hass.states.get.return_value = _mock_cooler_state()

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
        mock_hass.states.get.return_value = _mock_cooler_state(HVACMode.OFF)

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


_ABSENT = object()


def _range_attributes(
    target_temp_high=None,
    target_temp_low=None,
    supported_features=ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
    temperature=_ABSENT,
    target_temp_step=_ABSENT,
):
    """Build the attributes of a cooler that advertises a target range.

    A climate entity only publishes the attributes of the features it
    advertises, so ``temperature`` and ``target_temp_step`` stay out of the
    dict unless a caller asks for them.
    """
    attributes = {
        "target_temp_high": target_temp_high,
        "target_temp_low": target_temp_low,
        "supported_features": int(supported_features),
    }
    if temperature is not _ABSENT:
        attributes["temperature"] = temperature
    if target_temp_step is not _ABSENT:
        attributes["target_temp_step"] = target_temp_step
    return attributes


def _make_cooler_setup(
    cooler_state=HVACMode.COOL,
    cooler_temp_attr=24.0,
    system_unit=UnitOfTemperature.CELSIUS,
    cur_temp=25.0,
    target_cooltemp=24.0,
    target_temp=20.0,
    cooler_attributes=None,
):
    """Build a mock BT instance with a cooler in COOL demand conditions."""
    mock_hass = Mock()
    mock_hass.services = Mock()
    mock_hass.services.async_call = AsyncMock()
    mock_hass.config.units.temperature_unit = system_unit

    mock_cooler_state = Mock()
    mock_cooler_state.state = cooler_state
    mock_cooler_state.attributes = (
        {"temperature": cooler_temp_attr}
        if cooler_attributes is None
        else cooler_attributes
    )
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
    mock_self.bt_target_temp = target_temp
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
    async def test_fahrenheit_reported_temp_within_the_device_step_is_not_resent(self):
        """A setpoint snapped onto the device's °F step is unchanged.

        The device step is a °F interval worth 0.56 K: 75 °F is 23.89 °C,
        the grid position the commanded 24.0 °C snaps to and not a setpoint
        of its own.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes={"temperature": 75.0, "target_temp_step": 1.0},
            system_unit=UnitOfTemperature.FAHRENHEIT,
            target_cooltemp=24.0,
        )

        await control_cooler(mock_self)

        assert _service_calls(mock_hass, "set_temperature") == []

    @pytest.mark.asyncio
    async def test_fahrenheit_setpoint_beyond_the_device_step_is_written(self):
        """A setpoint the device cannot be holding is written immediately.

        The step tolerance only absorbs the device's own snapping; a genuine
        change has to reach the cooler on the first cycle.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes={"temperature": 75.0, "target_temp_step": 1.0},
            system_unit=UnitOfTemperature.FAHRENHEIT,
            target_cooltemp=22.0,
        )

        await control_cooler(mock_self)

        payload = _service_calls(mock_hass, "set_temperature")[0].args[2]
        assert payload == {"entity_id": "climate.cooler", "temperature": 71.6}

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
    async def test_unknown_reading_with_an_unchanged_target_sends_nothing(self):
        """A cooler that reports no setpoint is not commanded again.

        Without a reading there is nothing to compare against, so the send
        cache alone decides: the desired value is the one already written.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_temp_attr=None, target_cooltemp=24.0
        )
        mock_self._cooler_last_sent = {"temperature": (24.0, 0.0)}

        await control_cooler(mock_self)

        assert _service_calls(mock_hass, "set_temperature") == []

    @pytest.mark.asyncio
    async def test_unknown_reading_with_a_changed_target_is_written(self):
        """A missing reading does not hold back a new setpoint.

        The send cache holds a different value, so the desired one has never
        reached the cooler and goes out despite the unreadable state.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_temp_attr=None, target_cooltemp=24.0
        )
        mock_self._cooler_last_sent = {"temperature": (23.0, 0.0)}

        await control_cooler(mock_self)

        payload = _service_calls(mock_hass, "set_temperature")[0].args[2]
        assert payload == {"entity_id": "climate.cooler", "temperature": 24.0}

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
        """A failed command leaves the cache empty and is retried once paced.

        The retry follows the failure backoff rather than the resend
        throttle, so it goes out one backoff base later.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(cooler_temp_attr=20.0)

        mock_hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("device rejected the command")
        )
        await control_cooler(mock_self)

        assert last_sent_cooler_temperature(mock_self) is None

        mock_hass.services.async_call = AsyncMock()
        mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_BASE_S / 2
        await control_cooler(mock_self)
        assert _service_calls(mock_hass, "set_temperature") == []

        mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_BASE_S / 2
        await control_cooler(mock_self)

        assert len(_service_calls(mock_hass, "set_temperature")) == 1
        assert last_sent_cooler_temperature(mock_self) == 24.0

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
    async def test_run_of_failures_is_paced_by_an_exponential_backoff(self):
        """A device rejecting every write is retried ever more slowly.

        A rejected command primes no send timestamp, so the resend throttle
        cannot pace it. Without a backoff of its own the retry would go out
        on every control cycle — hammering exactly the cloud device whose
        rate limit caused the rejection in the first place.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=20.0
        )
        accepted = set()
        attempts: dict[str, list[float]] = {"set_temperature": [], "set_hvac_mode": []}

        async def accept_once_then_fail(domain, service, data, **kwargs):
            attempts[service].append(mock_self.clock.monotonic_value)
            if service in accepted:
                raise ConnectionError("cloud rate limit reached")
            accepted.add(service)

        mock_hass.services.async_call = AsyncMock(side_effect=accept_once_then_fail)

        # Two hours: long enough for the backoff to reach its ceiling and hold
        # there for a full wait, so the ceiling is measured rather than assumed.
        elapsed = 0.0
        while elapsed < 7200.0:
            mock_self.clock.monotonic_value = elapsed
            await control_cooler(mock_self)
            elapsed += 5.0

        # 1440 cycles in the two hours. The resend throttle alone would pace
        # the first of them and nothing after that: its timestamp advances on
        # a successful send only, so once the device starts rejecting it stops
        # moving and every later cycle finds the window open — 1428 calls per
        # channel, measured with the backoff disabled.
        for service, stamps in attempts.items():
            assert len(stamps) <= 12, service
            gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
            assert min(gaps) >= COOLER_FAILURE_BACKOFF_BASE_S, service
            # The run is long enough to be pinned at the ceiling.
            assert max(gaps) == COOLER_FAILURE_BACKOFF_MAX_S, service
            # The spacing grows rather than staying at the base.
            assert gaps[-1] > gaps[0], service

    @pytest.mark.asyncio
    async def test_one_transient_failure_does_not_defer_the_cooler_off(self):
        """A hiccup on the way to OFF is retried well inside a resend interval.

        The resend interval respects the compressor's protection window, and
        a rejected command never reached the compressor: the backoff paces
        the retry only so a rate-limited endpoint is not hammered, which is a
        much shorter wait.
        """
        assert COOLER_FAILURE_BACKOFF_BASE_S < COOLER_RESEND_INTERVAL_S

        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_state=HVACMode.COOL, cooler_temp_attr=24.0, cur_temp=20.0
        )
        mock_self.bt_hvac_mode = HVACMode.OFF

        mock_hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("device rejected the command")
        )
        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_hvac_mode")) == 1

        mock_hass.services.async_call = AsyncMock()
        mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_BASE_S
        await control_cooler(mock_self)

        mode_calls = _service_calls(mock_hass, "set_hvac_mode")
        assert len(mode_calls) == 1
        assert mode_calls[0].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_a_changed_target_only_waits_the_backoff_base(self):
        """A new setpoint is a new command and skips the run's grown wait.

        It does not skip the base, though: a channel whose last attempt was
        rejected keeps its floor whatever the payload says, so a desired
        value alternating between two rejected commands cannot buy itself a
        write on every cycle.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(cooler_temp_attr=20.0)

        mock_hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("device rejected the command")
        )
        for _ in range(4):
            mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_MAX_S
            await control_cooler(mock_self)

        # A run of four: the same setpoint would now wait eight bases.
        mock_hass.services.async_call = AsyncMock()
        mock_self.bt_target_cooltemp = 23.0
        await control_cooler(mock_self)
        assert _service_calls(mock_hass, "set_temperature") == []

        mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_BASE_S
        await control_cooler(mock_self)

        temp_calls = _service_calls(mock_hass, "set_temperature")
        assert len(temp_calls) == 1
        assert temp_calls[0].args[2]["temperature"] == 23.0

    @pytest.mark.asyncio
    async def test_a_different_rejected_command_starts_its_own_run(self):
        """The counter counts the run the gate prices.

        A command replacing the rejected one resets the run: the gate prices
        the retry of that command as the first of a run, so a counter that
        kept adding to the run before it would describe something else.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(cooler_temp_attr=20.0)
        mock_hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("device rejected the command")
        )

        for _ in range(4):
            mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_MAX_S
            await control_cooler(mock_self)

        # A different setpoint is rejected once: a run of one, not of five.
        mock_self.bt_target_cooltemp = 23.0
        mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_BASE_S
        await control_cooler(mock_self)

        mock_hass.services.async_call = AsyncMock()
        mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_BASE_S
        await control_cooler(mock_self)

        assert len(_service_calls(mock_hass, "set_temperature")) == 1

    @pytest.mark.asyncio
    async def test_a_long_run_of_failures_cannot_overflow_the_backoff(self):
        """The run stops counting before the exponent leaves float range.

        The control queue swallows whatever control_cooler raises, so an
        arithmetic error here would take cooler control out until the next
        reload — and a device that rejects every write reaches the exponent
        that overflows in about three weeks.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(cooler_temp_attr=20.0)
        mock_hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("device rejected the command")
        )

        attempts = 1100
        for _ in range(attempts):
            mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_MAX_S
            await control_cooler(mock_self)

        assert len(_service_calls(mock_hass, "set_temperature")) == attempts
        assert (
            mock_self._cooler_last_sent["temperature_failed"][0]
            == COOLER_FAILURE_BACKOFF_MAX_RUN
        )

    @pytest.mark.asyncio
    async def test_a_successful_send_clears_the_failure_backoff(self):
        """Recovery restores the plain resend pacing on the next cycle."""
        mock_self, mock_hass, _ = _make_cooler_setup(cooler_temp_attr=20.0)

        mock_hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("device rejected the command")
        )
        for _ in range(4):
            mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_MAX_S
            await control_cooler(mock_self)

        mock_hass.services.async_call = AsyncMock()
        mock_self.clock.monotonic_value += COOLER_FAILURE_BACKOFF_MAX_S
        await control_cooler(mock_self)
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)

        assert len(_service_calls(mock_hass, "set_temperature")) == 2

    @pytest.mark.asyncio
    async def test_failed_mode_command_is_paced_by_its_own_backoff(self):
        """The mode channel carries the same backoff as the temperature one."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=24.0
        )

        attempts: list[float] = []

        async def fail_mode(domain, service, data, **kwargs):
            if service == "set_hvac_mode":
                attempts.append(mock_self.clock.monotonic_value)
                raise ConnectionError("cloud rate limit reached")

        mock_hass.services.async_call = AsyncMock(side_effect=fail_mode)

        # Long enough for the backoff to double several times from its base,
        # and stepped far finer than the shortest wait.
        window = COOLER_FAILURE_BACKOFF_BASE_S * 64
        step = COOLER_FAILURE_BACKOFF_BASE_S / 5
        elapsed = 0.0
        while elapsed < window:
            mock_self.clock.monotonic_value = elapsed
            await control_cooler(mock_self)
            elapsed += step

        # The mode channel never gets a command through, so the resend
        # throttle never starts: without the backoff every one of those
        # cycles would carry a command.
        assert 1 < len(attempts) < window / COOLER_FAILURE_BACKOFF_BASE_S
        gaps = [b - a for a, b in zip(attempts, attempts[1:], strict=False)]
        assert min(gaps) >= COOLER_FAILURE_BACKOFF_BASE_S
        assert gaps[-1] > gaps[0]

    @pytest.mark.asyncio
    async def test_cancellation_during_service_call_propagates(self):
        """CancelledError is not swallowed by the per-call error isolation."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=20.0
        )

        mock_hass.services.async_call = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await control_cooler(mock_self)


class TestControlCoolerModeHysteresis:
    """The COOL/OFF decision spans a band rather than a single threshold."""

    # target_cooltemp - tolerance for the values _make_cooler_setup uses.
    SWITCH_ON_AT = 23.5

    @staticmethod
    def _make_compliant(mock_hass, cooler_state):
        """Let the fake cooler apply whatever it is commanded."""

        async def apply(domain, service, data, **kwargs):
            if service == "set_hvac_mode":
                cooler_state.state = data["hvac_mode"]
            else:
                cooler_state.attributes = {"temperature": data["temperature"]}

        mock_hass.services.async_call = AsyncMock(side_effect=apply)

    @pytest.mark.asyncio
    async def test_room_temp_resting_on_the_threshold_is_not_written_every_cycle(self):
        """A hundredth of a degree of sensor noise must not drive the cooler.

        A changed desired mode bypasses the resend throttle by design, so a
        single threshold turns every flip into a write — and a fully
        compliant device gets commanded at the whole control-cycle rate.
        """
        mock_self, mock_hass, cooler_state = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=24.0, target_cooltemp=24.0
        )
        self._make_compliant(mock_hass, cooler_state)

        cycles = 0
        elapsed = 0.0
        while elapsed < 3600.0:
            mock_self.clock.monotonic_value = elapsed
            mock_self.cur_temp = self.SWITCH_ON_AT + (
                0.005 if cycles % 2 == 0 else -0.005
            )
            await control_cooler(mock_self)
            elapsed += 5.0
            cycles += 1

        assert cycles == 720
        assert len(_service_calls(mock_hass, "set_hvac_mode")) == 1

    @pytest.mark.asyncio
    async def test_cooling_stops_once_the_room_leaves_the_band(self):
        """The band delays the switch-off, it does not prevent it."""
        mock_self, mock_hass, cooler_state = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=24.0, target_cooltemp=24.0
        )
        self._make_compliant(mock_hass, cooler_state)

        await control_cooler(mock_self)
        assert (
            _service_calls(mock_hass, "set_hvac_mode")[-1].args[2]["hvac_mode"]
            == HVACMode.COOL
        )

        # Just below the switch-on point but still inside the band.
        mock_self.cur_temp = self.SWITCH_ON_AT - 0.1
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_hvac_mode")) == 1

        # Below the band.
        mock_self.cur_temp = self.SWITCH_ON_AT - 0.25
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)

        mode_calls = _service_calls(mock_hass, "set_hvac_mode")
        assert len(mode_calls) == 2
        assert mode_calls[-1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_the_band_does_not_delay_the_switch_on(self):
        """The band widens the COOL state, it does not narrow the way into it.

        A band applied in both directions would hold the cooler off until the
        room had climbed a further band width past the switch-on point, which
        is not the deadband the tolerance asks for.
        """
        mock_self, mock_hass, cooler_state = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=24.0, target_cooltemp=24.0
        )
        self._make_compliant(mock_hass, cooler_state)

        # Well below the band, so the decision the band reads is OFF.
        mock_self.cur_temp = self.SWITCH_ON_AT - 1.0
        await control_cooler(mock_self)
        assert _service_calls(mock_hass, "set_hvac_mode") == []

        # Exactly on the switch-on point.
        mock_self.cur_temp = self.SWITCH_ON_AT
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)

        mode_calls = _service_calls(mock_hass, "set_hvac_mode")
        assert len(mode_calls) == 1
        assert mode_calls[0].args[2]["hvac_mode"] == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_an_alternating_decision_is_not_written_every_cycle(self):
        """A cooler in a mode BT never commands must not defeat the band.

        ``dry`` is neither COOL nor OFF, so the device differs from whatever
        BT decides and every cycle wants to write. A band that read the last
        successful send would never move on a device that accepts nothing:
        the room resting on the switch-on point would flip the decision every
        cycle, and each flip would reach the retry gate as a new command
        rather than as a retry.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_state="dry", cooler_temp_attr=24.0, target_cooltemp=24.0
        )
        attempts: list[float] = []

        async def always_fail(domain, service, data, **kwargs):
            if service == "set_hvac_mode":
                attempts.append(mock_self.clock.monotonic_value)
            raise ConnectionError("cloud rate limit reached")

        mock_hass.services.async_call = AsyncMock(side_effect=always_fail)

        cycles = 0
        elapsed = 0.0
        while elapsed < 3600.0:
            mock_self.clock.monotonic_value = elapsed
            mock_self.cur_temp = self.SWITCH_ON_AT + (
                0.005 if cycles % 2 == 0 else -0.005
            )
            await control_cooler(mock_self)
            elapsed += 5.0
            cycles += 1

        assert cycles == 720
        # Paced by the failure backoff instead of by the control-cycle rate.
        assert len(attempts) < 10
        gaps = [b - a for a, b in zip(attempts, attempts[1:], strict=False)]
        assert min(gaps) >= COOLER_FAILURE_BACKOFF_BASE_S

    @pytest.mark.asyncio
    async def test_the_band_follows_the_decided_mode_not_the_reported_one(self):
        """An externally switched-off cooler is put back into the band's state.

        The device's reported mode lags a command and can be changed from
        outside Better Thermostat, so reading the band's state off it would
        abandon cooling inside the band.
        """
        mock_self, mock_hass, cooler_state = _make_cooler_setup(
            cooler_state=HVACMode.OFF, cooler_temp_attr=24.0, target_cooltemp=24.0
        )
        self._make_compliant(mock_hass, cooler_state)

        await control_cooler(mock_self)

        cooler_state.state = HVACMode.OFF
        mock_self.cur_temp = self.SWITCH_ON_AT - 0.1
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)

        mode_calls = _service_calls(mock_hass, "set_hvac_mode")
        assert len(mode_calls) == 2
        assert mode_calls[-1].args[2]["hvac_mode"] == HVACMode.COOL


class TestControlCoolerTargetRange:
    """Payload selection for coolers that only accept a target range."""

    @staticmethod
    def _set_temperature_payload(mock_hass):
        calls = _service_calls(mock_hass, "set_temperature")
        return calls[0].args[2] if calls else None

    @pytest.mark.asyncio
    async def test_range_only_cooler_receives_both_bounds(self):
        """A range-only cooler is written via target_temp_high/low.

        Home Assistant rejects a "temperature" payload for such an entity, so
        it would never receive a setpoint at all.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=28.0, target_temp_low=19.0
            ),
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 20.0,
        }

    @pytest.mark.asyncio
    async def test_low_bound_never_exceeds_the_high_bound(self):
        """A heating target above the cooling target is capped at it."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=28.0, target_temp_low=19.0
            ),
            target_cooltemp=24.0,
            target_temp=26.0,
        )

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload["target_temp_low"] == payload["target_temp_high"] == 24.0

    @pytest.mark.asyncio
    async def test_cooler_supporting_both_features_keeps_single_setpoint(self):
        """A dual-feature cooler driving "temperature" gets the single payload.

        The reading comes from the single-setpoint channel, so a present
        upper bound alone must not divert the write onto the range channel.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                temperature=28.0,
                target_temp_high=28.0,
                target_temp_low=19.0,
                supported_features=ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
            ),
            target_cooltemp=24.0,
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) == {
            "entity_id": "climate.cooler",
            "temperature": 24.0,
        }

    @pytest.mark.asyncio
    async def test_cooler_supporting_both_features_follows_the_range_channel(self):
        """A dual-feature cooler with an empty "temperature" gets both bounds.

        Such a cooler is driving its range, and the reading was taken from the
        upper bound; a single-setpoint payload would write a channel the
        device does not drive, so the two would never converge.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                temperature=None,
                target_temp_high=28.0,
                target_temp_low=19.0,
                supported_features=ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE,
            ),
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 20.0,
        }

    @pytest.mark.asyncio
    async def test_cooler_without_feature_flags_keeps_single_setpoint(self):
        """Without advertised features the single-setpoint payload is used."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_temp_attr=28.0, target_cooltemp=24.0
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) == {
            "entity_id": "climate.cooler",
            "temperature": 24.0,
        }

    @pytest.mark.asyncio
    async def test_range_payload_is_converted_to_fahrenheit(self):
        """Both bounds are converted on a °F system."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=82.4, target_temp_low=66.2
            ),
            system_unit=UnitOfTemperature.FAHRENHEIT,
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)

        payload = self._set_temperature_payload(mock_hass)
        assert payload["target_temp_high"] == 75.2  # 24.0 °C
        assert payload["target_temp_low"] == 68.0  # 20.0 °C

    @pytest.mark.asyncio
    async def test_matching_bounds_are_not_resent(self):
        """Both bounds in sync means nothing is written.

        The dedup resolves the reported setpoint from the range key, so an
        applied upper bound is recognised as such.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=24.0, target_temp_low=20.0
            ),
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) is None

    @pytest.mark.asyncio
    async def test_changed_lower_bound_alone_triggers_a_send(self):
        """A heating target that moved is written even when cooling is unchanged.

        Both bounds travel in one call, so a lower bound left behind on the
        device would persist until the cooling target happens to change.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=24.0, target_temp_low=19.0
            ),
            target_cooltemp=24.0,
            target_temp=21.0,
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 21.0,
        }

    @pytest.mark.asyncio
    async def test_lower_bound_within_the_base_tolerance_is_not_resent(self):
        """A cooler reporting no step still gets the base reconcile tolerance.

        Without a reported step there is no grid to derive a tolerance from,
        so the bound is accepted within RECONCILE_TOLERANCE_K; a stricter
        comparison would rewrite the bound on every cycle forever.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=24.0, target_temp_low=20.04
            ),
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) is None

    @pytest.mark.asyncio
    async def test_lower_bound_within_half_a_device_step_is_not_resent(self):
        """A bound the device snapped onto its own grid is left alone.

        The device answers a written bound at most half its step away, so the
        comparison carries that step; a tighter one could never be satisfied
        and would rewrite the bound on every cycle forever.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=24.0, target_temp_low=20.4, target_temp_step=1.0
            ),
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) is None

    @pytest.mark.asyncio
    async def test_quantized_upper_bound_does_not_mask_a_drifted_lower_bound(self):
        """A device grid on the upper bound leaves the lower one correctable.

        The device answers the upper bound on its own grid, so the temperature
        channel stays unconverged while the lower bound is still behind. The
        settled reading covers the upper bound alone and must not null the
        write the lower bound needs.
        """
        mock_self, mock_hass, mock_cooler_state = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=28.0, target_temp_low=19.0
            ),
            target_cooltemp=22.22,
            target_temp=20.0,
        )

        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_temperature")) == 1

        # The device snapped the upper bound onto its own grid and left the
        # lower one behind.
        mock_cooler_state.attributes = _range_attributes(
            target_temp_high=22.0, target_temp_low=19.0
        )
        mock_self.clock.monotonic_value += 1.0
        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_temperature")) == 1

        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)

        temp_calls = _service_calls(mock_hass, "set_temperature")
        assert len(temp_calls) == 2
        assert temp_calls[1].args[2] == {
            "entity_id": "climate.cooler",
            "target_temp_high": pytest.approx(22.22),
            "target_temp_low": pytest.approx(20.0),
        }

    @pytest.mark.asyncio
    async def test_lower_bound_is_ignored_for_single_setpoint_coolers(self):
        """A single-setpoint cooler has no lower bound to keep in sync."""
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes={"temperature": 24.0, "target_temp_low": 15.0},
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)

        assert self._set_temperature_payload(mock_hass) is None

    @pytest.mark.asyncio
    async def test_quantizing_range_cooler_is_written_as_rarely_as_a_single_one(self):
        """The lower bound carries the same quantization latch as the setpoint.

        A cooler that snaps what it receives onto its own grid reports no
        step to derive a tolerance from, so its bound never matches the
        written one exactly. Without a latch on the bound the whole payload
        goes out on every resend interval for as long as the cooler is
        configured, while a single-setpoint cooler quantizing by the same
        amount is written once.
        """
        quantization = 0.3
        resend_intervals = 20

        async def _writes(cooler_attributes):
            mock_self, mock_hass, _ = _make_cooler_setup(
                cooler_attributes=cooler_attributes,
                target_cooltemp=24.0,
                target_temp=20.0,
            )
            for _ in range(resend_intervals):
                await control_cooler(mock_self)
                mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
            return len(_service_calls(mock_hass, "set_temperature"))

        single_setpoint = await _writes({"temperature": 24.0 - quantization})
        target_range = await _writes(
            _range_attributes(
                target_temp_high=24.0 - quantization,
                target_temp_low=20.0 - quantization,
            )
        )

        assert single_setpoint == 1
        assert target_range == single_setpoint

    @pytest.mark.asyncio
    async def test_settled_lower_bound_does_not_swallow_a_moved_heating_target(self):
        """The latch answers for the bound BT wrote, not for a new one.

        A heating target the user moved makes the bound a new command, so it
        reaches the cooler on the next cycle regardless of what the device
        settled at.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=23.7, target_temp_low=19.7
            ),
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)
        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_temperature")) == 1

        mock_self.bt_target_temp = 21.0
        await control_cooler(mock_self)

        temp_calls = _service_calls(mock_hass, "set_temperature")
        assert len(temp_calls) == 2
        assert temp_calls[1].args[2] == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 21.0,
        }

    @pytest.mark.asyncio
    async def test_drifted_lower_bound_outlives_quantization_acceptance(self):
        """A settled upper bound does not vouch for the lower one.

        The settled reading tracks the temperature channel alone, so a lower
        bound the device never applied stays correctable. The resend throttle
        still paces the retry.
        """
        mock_self, mock_hass, mock_cooler_state = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=28.0, target_temp_low=19.0
            ),
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_temperature")) == 1

        # The device applied the upper bound and left the lower one behind.
        mock_cooler_state.attributes = _range_attributes(
            target_temp_high=24.0, target_temp_low=19.0
        )
        mock_self.clock.monotonic_value += 1.0
        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_temperature")) == 1

        mock_self.clock.monotonic_value += COOLER_RESEND_INTERVAL_S
        await control_cooler(mock_self)

        temp_calls = _service_calls(mock_hass, "set_temperature")
        assert len(temp_calls) == 2
        assert temp_calls[1].args[2] == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 20.0,
        }

    @pytest.mark.asyncio
    async def test_lower_bound_drift_is_not_throttled_as_a_resend(self):
        """A lower bound outside the send cache is a new payload.

        The cache holds the temperature channel only, so the bound this
        payload carries was never written. Pacing it as a resend would hold
        the user's heating-target change back for a whole resend interval.
        """
        mock_self, mock_hass, _ = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=24.0, target_temp_low=19.0
            ),
            target_cooltemp=24.0,
            target_temp=21.0,
        )
        mock_self._cooler_last_sent = {"temperature": (24.0, 0.0)}
        mock_self.clock.monotonic_value = 1.0

        await control_cooler(mock_self)

        temp_calls = _service_calls(mock_hass, "set_temperature")
        assert len(temp_calls) == 1
        assert temp_calls[0].args[2] == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 21.0,
        }

    @pytest.mark.asyncio
    async def test_heating_target_change_is_written_inside_the_resend_window(self):
        """A moved heating target reaches the cooler on the next cycle.

        The cooling target is unchanged and the device answered both bounds,
        so only the lower bound differs from what the cache holds. That makes
        the payload new rather than a retry, and the throttle lets it through.
        """
        mock_self, mock_hass, mock_cooler_state = _make_cooler_setup(
            cooler_attributes=_range_attributes(
                target_temp_high=28.0, target_temp_low=19.0
            ),
            target_cooltemp=24.0,
            target_temp=20.0,
        )

        await control_cooler(mock_self)
        assert len(_service_calls(mock_hass, "set_temperature")) == 1

        # The device applied both bounds; the user then raises the heating
        # target well inside the resend window.
        mock_cooler_state.attributes = _range_attributes(
            target_temp_high=24.0, target_temp_low=20.0
        )
        mock_self.bt_target_temp = 21.0
        mock_self.clock.monotonic_value += 1.0
        await control_cooler(mock_self)

        temp_calls = _service_calls(mock_hass, "set_temperature")
        assert len(temp_calls) == 2
        assert temp_calls[1].args[2] == {
            "entity_id": "climate.cooler",
            "target_temp_high": 24.0,
            "target_temp_low": 21.0,
        }
