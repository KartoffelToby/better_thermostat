"""The grid a valve number entity publishes, and an opening scaled onto it."""

from __future__ import annotations

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters.valve_entity import (
    ValveEntityRange,
    valve_entity_range,
    valve_entity_value,
)

VALVE = "number.valve"


class TestValveEntityRange:
    """The range is read from the entity's own attributes."""

    def test_the_published_attributes_are_read(self):
        """Min, max and step come from the entity, as numbers."""
        state = State(VALVE, "0", {"min": "10", "max": 255, "step": 0.5})
        assert valve_entity_range(state) == ValveEntityRange(10.0, 255.0, 0.5)

    def test_an_entity_publishing_none_spans_a_plain_percentage(self):
        """Without attributes the entity is taken as 0 to 100 in steps of 1."""
        assert valve_entity_range(State(VALVE, "0")) == ValveEntityRange(
            0.0, 100.0, 1.0
        )

    def test_a_non_numeric_attribute_raises(self):
        """A bound that is no number gives no range."""
        with pytest.raises(ValueError):
            valve_entity_range(State(VALVE, "0", {"max": "open"}))


class TestValveEntityValue:
    """An opening is mapped onto the range and its step grid."""

    @pytest.mark.parametrize(
        ("valve_range", "percent", "expected"),
        [
            pytest.param(ValveEntityRange(0, 100, 1), 37, 37, id="percentage"),
            pytest.param(ValveEntityRange(0, 255, 1), 50, 128, id="0-255"),
            pytest.param(ValveEntityRange(0, 99, 1), 100, 99, id="0-99"),
            pytest.param(ValveEntityRange(0, 100, 20), 30, 40, id="step-20"),
            pytest.param(ValveEntityRange(5, 105, 10), 32, 35, id="grid-from-min"),
            pytest.param(ValveEntityRange(10, 100, 1), 0, 10, id="closed-at-min"),
            pytest.param(ValveEntityRange(0, 100, 0), 33.3, 33.3, id="no-grid"),
            pytest.param(ValveEntityRange(0, 100, 30), 100, 90, id="top-on-grid"),
            pytest.param(ValveEntityRange(0, 100, 1), 150, 100, id="above-100"),
            pytest.param(ValveEntityRange(0, 100, 1), -20, 0, id="below-0"),
            pytest.param(ValveEntityRange(0, 100, 30), 150, 90, id="above-100-on-grid"),
        ],
    )
    def test_an_opening_lands_on_a_value_the_entity_offers(
        self, valve_range, percent, expected
    ):
        """The result lies on the grid from the minimum and within the bounds."""
        assert valve_entity_value(valve_range, percent) == pytest.approx(expected)
