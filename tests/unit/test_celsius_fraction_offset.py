"""A calibration number that counts fractions of a degree Celsius.

A Z-Wave configuration parameter publishes its raw value in the unit the
parameter defines, and Home Assistant converts none of it. The Eurotronic
Spirit's temperature offset counts tenths of a degree from -128 to 50 in
whole steps, and -128 stands for "use the external sensor" rather than an
offset. Better Thermostat reads and writes offsets in Kelvin, so ten counts
of that unit are one Kelvin, and a reserved value is never one.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters import generic, zwave_js
from custom_components.better_thermostat.adapters.base import (
    OffsetScale,
    offset_scale,
    offset_to_published,
    published_bound,
    published_reading,
    published_step,
    published_step_to_offset,
    published_to_offset,
    reserved_published_values,
)
from custom_components.better_thermostat.utils import controlling
from custom_components.better_thermostat.utils.const import CalibrationOutput
from custom_components.better_thermostat.utils.helpers import celsius_unit_divisor
from tests.factories import ThermostatStandIn, trv_record

ENTITY_ID = "climate.spirit"
PARAMETER = "number.spirit_temperature_offset"
SPIRIT_RESERVED = {-128: "Ext temp sensor be used for regul"}


def _parameter(
    value="0.0",
    *,
    unit="0.1 °C",
    minimum=-128,
    maximum=50,
    step=1,
    reserved=SPIRIT_RESERVED,
) -> State:
    """Return the state Home Assistant publishes for the Spirit's parameter 8."""
    attributes = {
        "min": minimum,
        "max": maximum,
        "step": step,
        "mode": "box",
        "unit_of_measurement": unit,
    }
    if reserved is not None:
        attributes["reserved_values"] = reserved
    return State(PARAMETER, value, attributes)


def _host(state: State) -> ThermostatStandIn:
    """Return a thermostat whose calibration entity is ``state``."""
    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.context = None
    host.hass = MagicMock()
    host.hass.states.get = lambda requested: (
        state
        if requested == PARAMETER
        else State(ENTITY_ID, "heat", {"temperature": 20.0})
    )
    host.hass.services.async_call = AsyncMock()
    trv = trv_record(
        entity_id=ENTITY_ID,
        calibration=CalibrationOutput.LOCAL_BASED,
        calibration_received=True,
        local_temperature_calibration_entity=PARAMETER,
        local_calibration_step=0.1,
    )
    trv.last_hvac_mode = "off"
    host.real_trvs = {ENTITY_ID: trv}
    return host


def _written(host: ThermostatStandIn) -> float:
    """Return the value the last ``number.set_value`` carried."""
    call = host.hass.services.async_call.await_args
    assert call.args[:2] == ("number", "set_value")
    return call.args[2]["value"]


class TestTheUnitNamesTheCountsPerDegree:
    """The unit string says how many counts make one degree Celsius."""

    @pytest.mark.parametrize(
        ("unit", "divisor"),
        [("°C", 1), ("0.1 °C", 10), ("0.1°C", 10), (" 0.1 °C ", 10), ("0.01 °C", 100)],
    )
    def test_a_celsius_unit_is_recognised(self, unit, divisor):
        assert celsius_unit_divisor(unit) == divisor

    @pytest.mark.parametrize(
        "unit", [None, "", "%", "°F", "K", "0.5 °C", "0.1 °F", "C", 0.1]
    )
    def test_any_other_unit_is_none(self, unit):
        assert celsius_unit_divisor(unit) is None

    @pytest.mark.parametrize(
        ("unit", "scale"),
        [
            ("0.1 °C", OffsetScale.CELSIUS_TENTHS),
            ("0.01 °C", OffsetScale.CELSIUS_HUNDREDTHS),
            ("°C", OffsetScale.KELVIN),
            (None, OffsetScale.KELVIN),
        ],
    )
    def test_the_scale_follows_the_unit(self, unit, scale):
        assert offset_scale(_parameter(unit=unit)) is scale


class TestTheScaleConvertsBothWays:
    """Ten or a hundred counts make a Kelvin, in both directions."""

    @pytest.mark.parametrize(
        ("scale", "published", "kelvin"),
        [
            (OffsetScale.CELSIUS_TENTHS, 7.0, 0.7),
            (OffsetScale.CELSIUS_TENTHS, -5.0, -0.5),
            (OffsetScale.CELSIUS_TENTHS, 50.0, 5.0),
            (OffsetScale.CELSIUS_HUNDREDTHS, 250.0, 2.5),
        ],
    )
    def test_a_value_reads_and_writes_as_the_same_offset(
        self, scale, published, kelvin
    ):
        assert published_to_offset(scale, published) == pytest.approx(kelvin)
        assert offset_to_published(scale, kelvin) == pytest.approx(published)

    def test_a_count_is_a_tenth_of_a_kelvin(self):
        assert published_step_to_offset(OffsetScale.CELSIUS_TENTHS, 1.0) == 0.1
        assert published_step_to_offset(OffsetScale.CELSIUS_HUNDREDTHS, 5.0) == 0.05


class TestReservedValuesAreNoOffset:
    """A value the parameter reserves is neither read nor written as an offset."""

    @pytest.mark.parametrize(
        "reserved", [{-128: "external"}, {"-128": "external"}], ids=["int", "str"]
    )
    def test_the_keys_read_alike_as_numbers_and_as_text(self, reserved):
        """Home Assistant publishes integers; a state through JSON carries text."""
        assert reserved_published_values(_parameter(reserved=reserved)) == {-128.0}

    @pytest.mark.parametrize("reserved", [None, {}, ["-128"], "-128"])
    def test_an_entity_without_a_mapping_reserves_nothing(self, reserved):
        assert reserved_published_values(_parameter(reserved=reserved)) == set()

    def test_a_key_that_is_no_number_is_left_out(self):
        state = _parameter(reserved={"auto": "automatic", -128: "external"})

        assert reserved_published_values(state) == {-128.0}

    def test_the_reserved_value_reads_as_no_offset(self):
        assert published_reading(_parameter("-128.0")) is None

    def test_the_value_next_to_it_is_an_offset(self):
        assert published_reading(_parameter("-127.0")) == pytest.approx(-12.7)

    def test_a_reserved_minimum_moves_up_by_one_step(self):
        assert published_bound(_parameter(), "min") == -127.0

    def test_a_reserved_maximum_moves_down_by_one_step(self):
        state = _parameter(reserved={50: "keep", 49: "also"})

        assert published_bound(state, "max") == 48.0

    def test_a_bound_that_is_no_reserved_value_is_kept(self):
        assert published_bound(_parameter(), "max") == 50.0

    def test_a_bound_that_is_no_number_is_none(self):
        assert published_bound(_parameter(minimum="low"), "min") is None

    def test_a_reserved_bound_moves_by_the_published_step(self):
        state = _parameter(minimum=-130, step=2, reserved={-130: "external"})

        assert published_bound(state, "min") == -128.0


class TestTheStepIsAUsableGrid:
    """A step that cannot space a grid is read as one count."""

    @pytest.mark.parametrize(
        ("step", "expected"),
        [(1, 1.0), (5, 5.0), (-2, 2.0), (0, 1.0), ("x", 1.0), ("nan", 1.0)],
    )
    def test_the_step_is_positive_and_finite(self, step, expected):
        assert published_step(_parameter(step=step)) == expected

    def test_a_missing_step_is_one_count(self):
        state = _parameter()
        attributes = dict(state.attributes)
        del attributes["step"]

        assert published_step(State(PARAMETER, "0.0", attributes)) == 1.0


class TestTheGenericAdapterReadsTheParameter:
    """Every read an adapter answers is in Kelvin."""

    @pytest.mark.parametrize("adapter", [generic, zwave_js])
    @pytest.mark.asyncio
    async def test_the_reported_count_is_read_as_its_offset(self, adapter):
        host = _host(_parameter("7.0"))

        assert await adapter.get_calibration_offset(host, ENTITY_ID) == pytest.approx(
            0.7
        )

    @pytest.mark.parametrize("adapter", [generic, zwave_js])
    @pytest.mark.asyncio
    async def test_the_reserved_value_reads_as_zero(self, adapter):
        """-128 is the external sensor, not a correction of -12.8 K."""
        host = _host(_parameter("-128.0"))

        assert await adapter.get_calibration_offset(host, ENTITY_ID) == 0.0

    @pytest.mark.parametrize("adapter", [generic, zwave_js])
    @pytest.mark.asyncio
    async def test_the_range_and_step_are_read_in_kelvin(self, adapter):
        host = _host(_parameter())

        assert await adapter.get_min_calibration_offset(
            host, ENTITY_ID
        ) == pytest.approx(-12.7)
        assert await adapter.get_max_calibration_offset(
            host, ENTITY_ID
        ) == pytest.approx(5.0)
        assert await adapter.get_calibration_offset_step(
            host, ENTITY_ID
        ) == pytest.approx(0.1)


class TestTheGenericAdapterWritesWholeCounts:
    """The parameter receives the count the offset stands for, exactly."""

    @pytest.mark.parametrize(
        ("kelvin", "count"),
        [
            (0.8, 8.0),
            (0.7 - 0.1, 6.0),
            (0.1 + 0.2, 3.0),
            (-0.3, -3.0),
            (0.84, 8.0),
            (0.0, 0.0),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_count_is_on_the_step_grid(self, kelvin, count):
        """An offset computed to 0.6 K reaches the device as 6 counts.

        Multiplied out it is 5.999999999999999, which a device taking whole
        counts truncates to 5.
        """
        host = _host(_parameter())

        await zwave_js.set_calibration_offset(host, ENTITY_ID, kelvin)

        assert _written(host) == count

    @pytest.mark.asyncio
    async def test_a_request_below_the_range_stops_short_of_the_reserved_value(self):
        host = _host(_parameter())

        await zwave_js.set_calibration_offset(host, ENTITY_ID, -20.0)

        assert _written(host) == -127.0
        assert host.real_trvs[ENTITY_ID].last_calibration == pytest.approx(-12.7)

    @pytest.mark.asyncio
    async def test_a_request_above_the_range_reaches_its_top(self):
        host = _host(_parameter())

        await zwave_js.set_calibration_offset(host, ENTITY_ID, 9.0)

        assert _written(host) == 50.0

    @pytest.mark.parametrize(
        ("kelvin", "count"), [(0.5, 4.0), (-0.5, -4.0)], ids=["above", "below"]
    )
    def test_a_reserved_value_inside_the_range_gives_way_towards_zero(
        self, kelvin, count
    ):
        state = _parameter(reserved={5: "reserved", -5: "reserved"})

        assert generic._published_offset(state, kelvin) == count

    def test_a_reserved_value_with_no_free_neighbour_towards_zero_takes_the_other(self):
        state = _parameter(reserved={5: "reserved", 4: "reserved"})

        assert generic._published_offset(state, 0.5) == 6.0

    def test_a_step_of_several_counts_is_kept(self):
        """The grid counts from the published minimum: -128 + 28 * 5 = 12."""
        state = _parameter(step=5, reserved=None)

        assert generic._published_offset(state, 1.2) == 12.0

    def test_a_grid_that_misses_zero_counts_from_the_minimum(self):
        """With -128 and a step of 3 the entity takes ..., 1, 4, 7, never 6."""
        state = _parameter(step=3, reserved=None)

        assert generic._published_offset(state, 0.6) == 7.0

    def test_a_maximum_off_the_grid_is_not_written(self):
        """51 counts round up to 52, past the maximum of 50; 49 is the top value."""
        state = _parameter(step=3, reserved=None)

        assert generic._published_offset(state, 5.1) == 49.0

    def test_a_range_reserved_throughout_writes_what_was_asked(self):
        """With no free value inside the bounds there is nothing better to take."""
        state = _parameter(reserved={4: "a", 5: "b", 6: "c"})

        assert generic._nearest_unreserved(state, 5.0, 1.0, 4.0, 6.0) == 5.0

    def test_a_kelvin_number_is_not_put_on_its_grid(self):
        """Only a parameter counting fractions takes whole counts."""
        state = _parameter(unit=None, minimum=-12, maximum=12, step=1, reserved=None)

        assert generic._published_offset(state, 1.3) == 1.3


class TestTheReconcilerReadsTheParameterLikeTheWritePath:
    """What the reconciler calls a divergence is what a control cycle writes."""

    def test_a_count_matching_the_command_has_not_diverged(self):
        host = _host(_parameter("25.0"))
        trv = host.real_trvs[ENTITY_ID]
        trv.last_calibration = 2.5

        assert controlling._offset_diverges(host, trv) is False

    def test_a_count_off_the_command_has_diverged(self):
        host = _host(_parameter("2.5"))
        trv = host.real_trvs[ENTITY_ID]
        trv.last_calibration = 2.5

        assert controlling._offset_diverges(host, trv) is True

    @pytest.mark.parametrize(
        ("command", "diverged"), [(0.0, False), (2.5, True)], ids=["zero", "offset"]
    )
    def test_the_reserved_value_compares_as_zero(self, command, diverged):
        """A control cycle reads -128 as 0, so the reconciler does too.

        Read as -12.8 K it would queue a cycle on every tick that the cycle
        then finds nothing to write for.
        """
        host = _host(_parameter("-128.0"))
        trv = host.real_trvs[ENTITY_ID]
        trv.last_calibration = command

        assert controlling._offset_diverges(host, trv) is diverged
