"""Pure tests for the condition tables the decision kernel ranks."""

from custom_components.better_thermostat.core.conditions import (
    EFFECTS,
    RANK_ABOVE_MODE_OFF,
    RANK_BELOW_MODE_OFF,
    Action,
    Condition,
    Meaning,
    strongest,
)
from custom_components.better_thermostat.core.desired import Suppression


def test_each_meaning_has_an_effect_and_one_rank():
    """Each meaning can rule, so each needs an effect and a place in the rank."""
    ranked = RANK_ABOVE_MODE_OFF + RANK_BELOW_MODE_OFF
    assert set(EFFECTS) == set(Meaning)
    assert sorted(ranked) == sorted(Meaning)


def test_releases_outrank_the_users_mode_choice():
    """Startup and maintenance own the valves, so mode OFF cannot pre-empt them."""
    assert {EFFECTS[meaning].action for meaning in RANK_ABOVE_MODE_OFF} == {
        Action.RELEASE
    }
    assert {EFFECTS[meaning].action for meaning in RANK_BELOW_MODE_OFF} == {Action.OFF}


def test_higher_rank_rules_over_collection_order():
    """A lower-ranked condition collected first does not rule."""
    demand = Condition(Meaning.NO_CALL_FOR_HEAT, Suppression.NO_CALL_FOR_HEAT)
    window = Condition(Meaning.WINDOW_OPEN, Suppression.WINDOW)
    assert strongest([demand, window], RANK_BELOW_MODE_OFF) == window


def test_first_collected_source_rules_within_a_meaning():
    """Two sources of one meaning: the one collected first carries its reason."""
    window = Condition(Meaning.WINDOW_OPEN, Suppression.WINDOW)
    door = Condition(Meaning.WINDOW_OPEN, Suppression.DOOR)
    assert strongest([window, door], RANK_BELOW_MODE_OFF) == window
    assert strongest([door, window], RANK_BELOW_MODE_OFF) == door


def test_conditions_outside_the_rank_group_are_not_considered():
    """A rank group only picks among the meanings it lists."""
    startup = Condition(Meaning.STARTUP)
    assert strongest([startup], RANK_BELOW_MODE_OFF) is None
    assert strongest([startup], RANK_ABOVE_MODE_OFF) == startup
