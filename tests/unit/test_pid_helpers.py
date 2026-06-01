"""Tests for the shared PID-key helpers (resolve_unique_id, bucket helpers)."""

from types import SimpleNamespace

from custom_components.better_thermostat.utils.calibration.pid import (
    format_bucket,
    resolve_unique_id,
    round_to_bucket,
)


class TestResolveUniqueId:
    """resolve_unique_id prefers unique_id, then _unique_id, then 'bt'."""

    def test_uses_public_unique_id(self):
        """The public unique_id wins."""
        obj = SimpleNamespace(unique_id="pub", _unique_id="priv")
        assert resolve_unique_id(obj) == "pub"

    def test_falls_back_to_private(self):
        """A missing/empty unique_id falls back to _unique_id."""
        obj = SimpleNamespace(unique_id=None, _unique_id="priv")
        assert resolve_unique_id(obj) == "priv"

    def test_falls_back_to_bt(self):
        """With neither present, the literal 'bt' is used."""
        assert resolve_unique_id(SimpleNamespace()) == "bt"


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
