"""A per-TRV boolean option reads the way the options flow saves it."""

import pytest

from custom_components.better_thermostat.utils.advanced_flags import (
    advanced_flag,
    as_bool,
)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (True, True),
        (False, False),
        (None, False),
        ("true", True),
        ("True", True),
        (" On ", True),
        ("yes", True),
        ("false", False),
        ("False", False),
        ("off", False),
        ("no", False),
        ("0", False),
        ("", False),
        (1, True),
        (0, False),
    ],
)
def test_a_stored_value_reads_as_the_boolean_it_spells(stored, expected):
    assert advanced_flag({"valve_maintenance": stored}, "valve_maintenance") is (
        expected
    )


def test_a_missing_option_takes_the_default():
    assert advanced_flag({}, "homematicip") is False
    assert advanced_flag({}, "homematicip", True) is True


def test_a_missing_mapping_takes_the_default():
    assert advanced_flag(None, "child_lock") is False
    assert advanced_flag(None, "child_lock", True) is True


def test_a_none_value_takes_the_default():
    assert as_bool(None, True) is True
