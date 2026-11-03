"""Tests for the keys the learned controller state is stored under."""

import math

import pytest

from custom_components.better_thermostat.utils.calibration.mpc import (
    build_mpc_group_key,
    build_mpc_key,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    build_pid_key,
    build_pid_loop_key,
)
from custom_components.better_thermostat.utils.calibration.state_key import (
    GROUP_KEY_SEGMENT,
    UNKNOWN_BUCKET_TAG,
    StateKey,
    bucket_tag_of,
    format_bucket,
    is_bucket_tag,
    parse_bucket_tag,
    round_to_bucket,
)
from custom_components.better_thermostat.utils.calibration.tpi import build_tpi_key
from tests.factories import ThermostatStandIn


def _keyed_thermostat(target: float, unique_id: str | None) -> ThermostatStandIn:
    """Build a thermostat stand-in carrying what the state keys are built from."""
    bt = ThermostatStandIn()
    bt.heat_target_temperature = target
    bt._unique_id = unique_id
    return bt


class TestStoredForm:
    """The stored key strings keep their form, or the stored state is orphaned."""

    def test_bucket_keys_of_every_controller(self):
        """MPC, TPI and PID key a TRV's state as ``uid:entity_id:t<bucket>``."""
        bt = _keyed_thermostat(21.3, "entry_1")
        expected = "entry_1:climate.trv:t21.5"
        assert build_mpc_key(bt, "climate.trv") == expected
        assert build_tpi_key(bt, "climate.trv") == expected
        assert build_pid_key(bt, "climate.trv") == expected

    def test_group_key(self):
        """The room-wide MPC state is keyed ``uid:group:t<bucket>``."""
        bt = _keyed_thermostat(19.0, "entry_1")
        assert build_mpc_group_key(bt) == "entry_1:group:t19.0"

    def test_pid_loop_key(self):
        """A TRV's PID loop entry is keyed ``uid:entity_id`` without a bucket."""
        bt = _keyed_thermostat(21.0, "entry_1")
        assert build_pid_loop_key(bt, "climate.trv") == "entry_1:climate.trv"

    @pytest.mark.parametrize("target", [math.nan, math.inf, -math.inf])
    def test_a_target_that_is_not_finite_keys_the_unknown_bucket(self, target):
        """Every controller keys a non-finite target as ``tunknown``."""
        bt = _keyed_thermostat(target, "entry_1")
        expected = "entry_1:climate.trv:tunknown"
        assert build_mpc_key(bt, "climate.trv") == expected
        assert build_tpi_key(bt, "climate.trv") == expected
        assert build_pid_key(bt, "climate.trv") == expected
        assert build_mpc_group_key(bt) == "entry_1:group:tunknown"

    def test_a_thermostat_without_a_unique_id_keys_under_bt(self):
        """No unique id keys the state under ``bt``."""
        bt = _keyed_thermostat(-0.5, None)
        assert build_mpc_key(bt, "climate.trv") == "bt:climate.trv:t-0.5"


class TestBuckets:
    """A target falls into the 0.5 °C bucket its tag names."""

    @pytest.mark.parametrize(
        ("target", "bucket"), [(21.2, 21.0), (21.3, 21.5), (21.5, 21.5), (20, 20.0)]
    )
    def test_round_to_bucket(self, target, bucket):
        """A target snaps to the nearest half degree."""
        assert round_to_bucket(target) == bucket

    def test_format_bucket(self):
        """A bucket centre renders as a one-decimal t-tag."""
        assert format_bucket(21.0) == "t21.0"
        assert format_bucket(21.5) == "t21.5"

    def test_bucket_tag_of_a_target(self):
        """A numeric target maps to its bucket's tag."""
        assert bucket_tag_of(22.24) == "t22.0"

    @pytest.mark.parametrize("tag", [None, "", "t", "x21.0", UNKNOWN_BUCKET_TAG])
    def test_a_tag_without_a_number_parses_to_none(self, tag):
        """Only a ``t`` followed by a number names a bucket centre."""
        assert parse_bucket_tag(tag) is None

    def test_a_tag_with_a_number_parses(self):
        """``t21.5`` is the 21.5 °C bucket."""
        assert parse_bucket_tag("t21.5") == 21.5

    @pytest.mark.parametrize(
        ("part", "expected"),
        [("t21.0", True), ("t-0.5", True), (UNKNOWN_BUCKET_TAG, True)]
        + [("reid", False), ("climate.trv", False), ("t", False)],
    )
    def test_is_bucket_tag(self, part, expected):
        """The unknown tag and ``t`` with a number are bucket tags."""
        assert is_bucket_tag(part) is expected


class TestParse:
    """A stored key parses into the parts it was built from."""

    @pytest.mark.parametrize(
        "key",
        [
            "entry_1:climate.trv:t21.0",
            "entry_1:climate.trv:tunknown",
            f"entry_1:{GROUP_KEY_SEGMENT}:t21.0",
            "entry_1:climate.trv",
        ],
    )
    def test_round_trip(self, key):
        """Parsing and rendering a stored key gives the same string back."""
        parsed = StateKey.parse(key)
        assert parsed is not None
        assert str(parsed) == key

    def test_parts_of_a_bucket_key(self):
        """A bucket key yields unique id, segment, tag and bucket centre."""
        parsed = StateKey.parse("entry_1:climate.trv:t21.5")
        assert parsed == StateKey("entry_1", "climate.trv", "t21.5")
        assert StateKey("entry_1", "climate.trv", "t21.5").bucket_temperature == 21.5

    def test_parts_of_a_loop_key(self):
        """A PID loop key has no bucket."""
        parsed = StateKey.parse("entry_1:climate.trv")
        assert parsed == StateKey("entry_1", "climate.trv")
        assert StateKey("entry_1", "climate.trv").bucket_temperature is None

    def test_segment_is_read_from_the_right(self):
        """A colon inside the unique id stays in the unique id."""
        parsed = StateKey.parse("with:colons:climate.trv:t21.0")
        assert parsed == StateKey("with:colons", "climate.trv", "t21.0")

    @pytest.mark.parametrize(
        "key", ["entry_1:reid", "entry_1:group", "entry_1:t21.0", "climate.trv"]
    )
    def test_keys_that_name_no_thermostat(self, key):
        """The shared re-ID key and malformed keys parse to None."""
        assert StateKey.parse(key) is None


class TestIsBucketOf:
    """Bucket keys of one thermostat are recognised across targets."""

    def test_another_bucket_of_the_same_thermostat(self):
        """A different bucket of the same unique id and segment matches."""
        own = StateKey("entry_1", "climate.trv", "t21.0")
        assert StateKey("entry_1", "climate.trv", "t19.5").is_bucket_of(own)

    @pytest.mark.parametrize(
        "other",
        [
            StateKey("entry_2", "climate.trv", "t21.0"),
            StateKey("entry_1", "climate.other", "t21.0"),
            StateKey("entry_1", "climate.trv"),
        ],
    )
    def test_other_thermostats_and_loop_keys_do_not_match(self, other):
        """Another unique id, another segment or a loop key is no bucket of it."""
        own = StateKey("entry_1", "climate.trv", "t21.0")
        assert not other.is_bucket_of(own)
