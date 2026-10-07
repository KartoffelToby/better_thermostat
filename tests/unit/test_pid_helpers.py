"""Tests for the shared PID-key helpers (resolve_unique_id, bucket helpers)."""

from types import SimpleNamespace

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.utils.calibration.pid import (
    format_bucket,
    resolve_unique_id,
    round_to_bucket,
)


class TestResolveUniqueId:
    """resolve_unique_id keys state by the entity's unique id, else ``bt``."""

    def test_uses_the_unique_id(self):
        """An entity with a unique id keys its state under it."""
        assert resolve_unique_id(SimpleNamespace(unique_id="entry_1")) == "entry_1"

    @pytest.mark.parametrize("unique_id", [None, ""])
    def test_an_entity_without_a_unique_id_falls_back_to_bt(self, unique_id):
        """No unique id, or an empty one, keys state under ``bt``."""
        assert resolve_unique_id(SimpleNamespace(unique_id=unique_id)) == "bt"

    def test_a_thermostat_keys_by_its_config_entry(self):
        """The thermostat's unique id is the one its constructor received."""
        bt = BetterThermostat.__new__(BetterThermostat)
        bt._unique_id = "entry_1"
        assert resolve_unique_id(bt) == "entry_1"


class TestBucketHelpers:
    """round_to_bucket snaps to 0.5 °C; format_bucket renders the tag."""

    def test_round_down(self):
        """21.2 snaps to 21.0."""
        assert round_to_bucket(21.2) == 21.0

    def test_round_up(self):
        """21.3 snaps to 21.5."""
        assert round_to_bucket(21.3) == 21.5

    def test_round_exact(self):
        """An already-aligned value is unchanged."""
        assert round_to_bucket(21.5) == 21.5

    def test_round_accepts_numeric_string(self):
        """A numeric string is coerced before rounding."""
        assert round_to_bucket("21.4") == 21.5

    def test_format(self):
        """format_bucket renders a one-decimal t-tag."""
        assert format_bucket(21.0) == "t21.0"
        assert format_bucket(21.5) == "t21.5"
