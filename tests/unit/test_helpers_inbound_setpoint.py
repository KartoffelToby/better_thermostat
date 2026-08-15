"""Tests for the shared inbound-setpoint boundary in utils/helpers.py."""

from decimal import Decimal
import logging
from unittest.mock import Mock

from homeassistant.const import UnitOfTemperature
from homeassistant.core import Context, State
from homeassistant.util.unit_conversion import TemperatureDeltaConverter
import pytest

from custom_components.better_thermostat.utils.helpers import (
    COOLER_SETPOINT_KEYS,
    SETPOINT_MATCH_TOLERANCE,
    TRV_SETPOINT_KEYS,
    device_setpoint_step,
    normalize_step,
    read_setpoint_celsius,
    reported_setpoint_step_celsius,
    resolve_inbound_setpoint,
    resolve_state_change_event,
    setpoint_echo_window,
)

ENTITY_ID = "climate.device"
HELPERS_LOGGER = "custom_components.better_thermostat.utils.helpers"


def _fake_self(unit=UnitOfTemperature.CELSIUS):
    """Create a minimal BetterThermostat mock for the inbound helpers."""
    mock_self = Mock()
    mock_self.device_name = "test_thermostat"
    mock_self.hass.config.units.temperature_unit = unit
    mock_self.bt_min_temp = 5.0
    mock_self.bt_max_temp = 30.0
    mock_self.context = Context()
    return mock_self


def _state(attributes, entity_id=ENTITY_ID, state="heat"):
    return State(entity_id, state, attributes)


class TestReadSetpointCelsius:
    """Reading a setpoint from a foreign climate state."""

    def test_missing_state_returns_none(self):
        """A missing state holds no setpoint."""
        assert read_setpoint_celsius(_fake_self(), None, TRV_SETPOINT_KEYS, "t") is None

    def test_first_key_wins(self):
        """The single-setpoint key takes precedence over the range key."""
        state = _state({"temperature": 21.0, "target_temp_low": 19.0})
        assert (
            read_setpoint_celsius(_fake_self(), state, TRV_SETPOINT_KEYS, "t") == 21.0
        )

    def test_empty_first_key_falls_through(self):
        """A present-but-empty key does not hide the next one."""
        state = _state({"temperature": None, "target_temp_low": 19.0})
        assert (
            read_setpoint_celsius(_fake_self(), state, TRV_SETPOINT_KEYS, "t") == 19.0
        )

    def test_cooler_keys_read_the_upper_bound(self):
        """The cooler is driven towards the upper bound of a range."""
        state = _state({"temperature": None, "target_temp_high": 26.0})
        assert (
            read_setpoint_celsius(_fake_self(), state, COOLER_SETPOINT_KEYS, "t")
            == 26.0
        )

    def test_unusable_value_falls_through(self):
        """A non-numeric value does not stop the next key from being read."""
        state = _state({"temperature": "unavailable", "target_temp_low": 19.0})
        assert (
            read_setpoint_celsius(_fake_self(), state, TRV_SETPOINT_KEYS, "t") == 19.0
        )

    def test_value_is_converted_from_the_system_unit(self):
        """On a °F system the reported value is converted to °C."""
        mock_self = _fake_self(UnitOfTemperature.FAHRENHEIT)
        state = _state({"temperature": 68.0})
        assert read_setpoint_celsius(mock_self, state, TRV_SETPOINT_KEYS, "t") == 20.0


class TestNormalizeStep:
    """Coercing a reported step to a usable value."""

    def test_valid_step_is_kept(self):
        """A positive step passes through."""
        assert normalize_step(0.1) == 0.1

    def test_none_falls_back(self):
        """A missing step falls back."""
        assert normalize_step(None) == 0.5

    def test_zero_and_negative_fall_back(self):
        """A non-positive step cannot separate user input from an echo."""
        assert normalize_step(0) == 0.5
        assert normalize_step(-1.0) == 0.5

    def test_unconvertible_falls_back(self):
        """A non-numeric step falls back."""
        assert normalize_step("unavailable") == 0.5

    def test_custom_fallback_is_used(self):
        """The caller can supply its own fallback."""
        assert normalize_step(None, fallback=1.0) == 1.0

    def test_non_finite_falls_back(self):
        """NaN and infinity pass a ``<= 0`` test but cannot separate setpoints."""
        assert normalize_step(float("nan")) == 0.5
        assert normalize_step(float("inf")) == 0.5
        assert normalize_step(float("-inf")) == 0.5


class TestReportedSetpointStepCelsius:
    """The shared unit rule behind both setpoint-step readers."""

    def test_missing_attribute_and_explicit_none_are_the_same_case(self, caplog):
        """Neither shape publishes a step, and neither is worth logging."""
        with caplog.at_level(logging.DEBUG, logger=HELPERS_LOGGER):
            missing = reported_setpoint_step_celsius(_state({}), "bt", "°C", "t")
            explicit = reported_setpoint_step_celsius(
                _state({"target_temp_step": None}), "bt", "°C", "t"
            )
        assert missing is None
        assert explicit is None
        assert caplog.records == []

    def test_non_positive_step_is_returned_unjudged(self):
        """The rule reads the unit only; sign belongs to the caller."""
        assert reported_setpoint_step_celsius(
            _state({"target_temp_step": 0}), "bt", "°C", "t"
        ) == pytest.approx(0.0)
        assert reported_setpoint_step_celsius(
            _state({"target_temp_step": -1}), "bt", "°C", "t"
        ) == pytest.approx(-1.0)

    def test_fahrenheit_step_is_scaled_as_a_delta(self):
        """A °F step is a temperature difference, not an absolute reading."""
        step = reported_setpoint_step_celsius(
            _state({"target_temp_step": 2.0}), "bt", UnitOfTemperature.FAHRENHEIT, "t"
        )
        assert step == round(2.0 * 5.0 / 9.0, 4)

    def test_unit_is_read_from_the_same_attributes_as_the_step(self):
        """A state that names its own unit is read in that unit."""
        state = _state({"target_temp_step": 2.0, "temperature_unit": "°F"})
        step = reported_setpoint_step_celsius(
            state, "bt", UnitOfTemperature.CELSIUS, "t"
        )
        assert step == round(2.0 * 5.0 / 9.0, 4)

    def test_boolean_does_not_convert(self):
        """A boolean is not a step; stringifying it keeps it out."""
        for raw in (True, False):
            state = _state({"target_temp_step": raw})
            assert reported_setpoint_step_celsius(state, "bt", "°C", "t") is None

    def test_decimal_converts(self):
        """A Decimal published by an integration is a usable step."""
        state = _state({"target_temp_step": Decimal("0.5")})
        assert reported_setpoint_step_celsius(state, "bt", "°C", "t") == 0.5

    def test_log_source_names_the_caller(self, caplog):
        """An unconvertible step is logged against the site that read it."""
        with caplog.at_level(logging.DEBUG, logger=HELPERS_LOGGER):
            result = reported_setpoint_step_celsius(
                _state({"target_temp_step": "abc"}), "bt", "°C", "my_caller()"
            )
        assert result is None
        assert "my_caller()" in caplog.text


class TestDeviceSetpointStep:
    """Reading a controlled device's own setpoint step as a Celsius delta."""

    def _self(self, unit=UnitOfTemperature.CELSIUS, bt_step=0.5):
        """Build a BetterThermostat mock with a known configured step."""
        mock_self = _fake_self(unit)
        mock_self.bt_target_temp_step = bt_step
        return mock_self

    def test_celsius_step_is_taken_as_reported(self):
        """On a Celsius system the reported step already is a Celsius delta."""
        state = _state({"target_temp_step": 1.0})
        assert device_setpoint_step(self._self(), state, "test") == 1.0

    def test_fahrenheit_step_is_scaled_to_a_celsius_delta(self):
        """A step reported by a Fahrenheit device is a °F delta, not a °C one."""
        state = _state({"target_temp_step": 2.0})
        step = device_setpoint_step(
            self._self(UnitOfTemperature.FAHRENHEIT), state, "test"
        )
        assert step == round(2.0 * 5.0 / 9.0, 4)

    def test_explicit_unit_attribute_beats_the_system_unit(self):
        """A device that names its own unit is read in that unit."""
        state = _state({"target_temp_step": 2.0, "temperature_unit": "°F"})
        step = device_setpoint_step(
            self._self(UnitOfTemperature.CELSIUS), state, "test"
        )
        assert step == round(2.0 * 5.0 / 9.0, 4)

    def test_missing_step_falls_back_to_the_configured_step(self):
        """A device that publishes no step leaves only BT's own."""
        state = _state({"temperature": 21.0})
        assert device_setpoint_step(self._self(bt_step=0.1), state, "test") == 0.1

    @pytest.mark.parametrize("raw", [0, -1.0, "unavailable"])
    def test_unusable_step_falls_back_to_the_configured_step(self, raw):
        """A non-positive or non-numeric step cannot separate two setpoints."""
        state = _state({"target_temp_step": raw})
        assert device_setpoint_step(self._self(bt_step=0.1), state, "test") == 0.1


class TestSetpointEchoWindow:
    """The distance below which a setpoint difference is grid noise."""

    def test_window_is_a_step_less_the_read_grid(self):
        """A reported value carries the read grid, so the window shrinks by it."""
        assert setpoint_echo_window(0.5) == 0.5 - SETPOINT_MATCH_TOLERANCE

    def test_window_stays_positive_for_a_tiny_step(self):
        """A step at or below the read grid still separates two setpoints."""
        assert setpoint_echo_window(0.005) == SETPOINT_MATCH_TOLERANCE


class TestResolveInboundSetpoint:
    """Clamping and echo detection on a reported setpoint."""

    def test_missing_value_returns_none(self):
        """A state without a usable setpoint resolves to nothing."""
        state = _state({"current_temperature": 20.0})
        assert (
            resolve_inbound_setpoint(
                _fake_self(),
                state,
                keys=TRV_SETPOINT_KEYS,
                known_values=(),
                step=0.5,
                log_source="t",
            )
            is None
        )

    def test_value_inside_range_is_untouched(self):
        """A value inside the configured range is passed through."""
        result = resolve_inbound_setpoint(
            _fake_self(),
            _state({"temperature": 21.0}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(),
            step=0.5,
            log_source="t",
        )
        assert (result.raw, result.value, result.clamped) == (21.0, 21.0, False)

    def test_value_above_range_is_clamped_and_raw_kept(self):
        """Clamping records the reported value, which callers still need."""
        result = resolve_inbound_setpoint(
            _fake_self(),
            _state({"temperature": 35.0}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(),
            step=0.5,
            log_source="t",
        )
        assert (result.raw, result.value, result.clamped) == (35.0, 30.0, True)

    def test_value_below_range_is_clamped(self):
        """A value below the minimum is raised to it."""
        result = resolve_inbound_setpoint(
            _fake_self(),
            _state({"temperature": 2.0}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(),
            step=0.5,
            log_source="t",
        )
        assert (result.raw, result.value, result.clamped) == (2.0, 5.0, True)

    def test_value_within_a_step_of_a_known_value_is_an_echo(self):
        """A device settling BT's write on its own grid is not user input."""
        result = resolve_inbound_setpoint(
            _fake_self(),
            _state({"temperature": 21.4}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(None, 21.5),
            step=0.5,
            log_source="t",
        )
        assert result.is_echo is True

    def test_a_full_step_away_is_user_input(self):
        """A change of at least one step is adopted."""
        result = resolve_inbound_setpoint(
            _fake_self(),
            _state({"temperature": 22.0}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(21.5,),
            step=0.5,
            log_source="t",
        )
        assert result.is_echo is False

    def test_a_full_step_off_a_non_dyadic_grid_is_user_input(self):
        """One press on a 2 °F device lands a hair below a full step.

        21.11 °C and 22.22 °C are the read-back values of 70 °F and 72 °F, and
        their difference is 1.1099999999999994 against a step of 1.1111.
        """
        result = resolve_inbound_setpoint(
            _fake_self(),
            _state({"temperature": 22.22}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(21.11,),
            step=1.1111,
            log_source="t",
        )
        assert result.is_echo is False

    def test_every_fahrenheit_step_is_user_input(self):
        """On a °F system one press of the up button is never an echo.

        The reported values pass through convert_to_float's 0.01 grid while
        the step sits on the device grid, so a genuine single-step move can
        land a hair below one full step.
        """
        mock_self = _fake_self(UnitOfTemperature.FAHRENHEIT)
        # The whole grid has to stay inside the configured range, so that the
        # clamp cannot pull a reported value onto the previous one.
        mock_self.bt_max_temp = 35.0
        step = round(
            TemperatureDeltaConverter.convert(
                1.0, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
            ),
            4,
        )
        for fahrenheit in range(50, 90):
            known = read_setpoint_celsius(
                mock_self,
                _state({"temperature": float(fahrenheit)}),
                TRV_SETPOINT_KEYS,
                "t",
            )
            result = resolve_inbound_setpoint(
                mock_self,
                _state({"temperature": float(fahrenheit + 1)}),
                keys=TRV_SETPOINT_KEYS,
                known_values=(known,),
                step=step,
                log_source="t",
            )
            assert result.is_echo is False, f"{fahrenheit} °F -> {fahrenheit + 1} °F"

    def test_non_numeric_known_values_are_ignored(self):
        """Uninitialised known values do not raise."""
        result = resolve_inbound_setpoint(
            _fake_self(),
            _state({"temperature": 22.0}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(None, "unset"),
            step=0.5,
            log_source="t",
        )
        assert result.is_echo is False

    def test_unknown_bounds_do_not_raise(self):
        """A range BT does not know yet cannot clamp, but must not crash."""
        mock_self = _fake_self()
        mock_self.bt_min_temp = None
        mock_self.bt_max_temp = None
        result = resolve_inbound_setpoint(
            mock_self,
            _state({"temperature": 21.0}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(),
            step=0.5,
            log_source="t",
        )
        assert (result.value, result.clamped) == (21.0, False)

    def test_known_bound_is_still_enforced_alone(self):
        """One known bound clamps even while the other is unknown."""
        mock_self = _fake_self()
        mock_self.bt_max_temp = None
        result = resolve_inbound_setpoint(
            mock_self,
            _state({"temperature": 2.0}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(),
            step=0.5,
            log_source="t",
        )
        assert (result.value, result.clamped) == (5.0, True)

    def test_inverted_range_never_yields_a_value_above_the_maximum(self):
        """Non-overlapping heater and cooler ranges still clamp to the maximum.

        A configuration whose members do not overlap leaves bt_min_temp above
        bt_max_temp, and a value the lower bound raises must not end up above
        the upper one.
        """
        mock_self = _fake_self()
        mock_self.bt_min_temp = 25.0
        mock_self.bt_max_temp = 20.0
        result = resolve_inbound_setpoint(
            mock_self,
            _state({"temperature": 18.0}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(),
            step=0.5,
            log_source="t",
        )
        assert (result.value, result.clamped) == (20.0, True)

    def test_echo_is_judged_after_clamping(self):
        """A value the clamp pulls onto a known value is an echo, not input."""
        result = resolve_inbound_setpoint(
            _fake_self(),
            _state({"temperature": 32.0}),
            keys=TRV_SETPOINT_KEYS,
            known_values=(30.0,),
            step=0.5,
            log_source="t",
        )
        assert (result.value, result.is_echo) == (30.0, True)


class TestResolveStateChangeEvent:
    """The shared guard prologue of the device event handlers."""

    @staticmethod
    def _event(mock_self, old_state, new_state, context=None):
        event = Mock()
        event.data = {
            "old_state": old_state,
            "new_state": new_state,
            "entity_id": ENTITY_ID,
        }
        event.context = context if context is not None else Context()
        return event

    def test_actionable_event_returns_states(self):
        """A complete foreign event yields both states and the entity id."""
        mock_self = _fake_self()
        old_state = _state({"temperature": 20.0})
        new_state = _state({"temperature": 21.0})
        resolved = resolve_state_change_event(
            mock_self, self._event(mock_self, old_state, new_state), "TRV"
        )
        assert resolved == (old_state, new_state, ENTITY_ID)

    def test_missing_new_state_is_skipped(self):
        """An event without a new state carries nothing to act on."""
        mock_self = _fake_self()
        event = self._event(mock_self, _state({"temperature": 20.0}), None)
        assert resolve_state_change_event(mock_self, event, "TRV") is None

    def test_missing_old_state_is_skipped(self):
        """An event without an old state carries nothing to compare against."""
        mock_self = _fake_self()
        event = self._event(mock_self, None, _state({"temperature": 20.0}))
        assert resolve_state_change_event(mock_self, event, "TRV") is None

    def test_non_state_payload_is_skipped(self):
        """A payload that is not a State is skipped."""
        mock_self = _fake_self()
        event = self._event(mock_self, _state({"temperature": 20.0}), "not-a-state")
        assert resolve_state_change_event(mock_self, event, "TRV") is None

    def test_event_without_entity_id_is_skipped(self):
        """Without an entity id there is no device to attribute the change to."""
        mock_self = _fake_self()
        event = self._event(
            mock_self, _state({"temperature": 20.0}), _state({"temperature": 21.0})
        )
        event.data["entity_id"] = None
        assert resolve_state_change_event(mock_self, event, "TRV") is None

    def test_own_context_is_skipped(self):
        """An event caused by BT's own service call is not user input."""
        mock_self = _fake_self()
        event = self._event(
            mock_self,
            _state({"temperature": 20.0}),
            _state({"temperature": 21.0}),
            context=mock_self.context,
        )
        assert resolve_state_change_event(mock_self, event, "TRV") is None
