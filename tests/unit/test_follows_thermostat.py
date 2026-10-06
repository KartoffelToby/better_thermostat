"""An entity that reads the thermostat is published whenever the values change."""

from unittest.mock import MagicMock, patch

from custom_components.better_thermostat.entity import (
    LEARNED_STATE_SIGNAL,
    FollowsThermostat,
)
from tests.factories import ThermostatStandIn


def _following(entity_id: str | None):
    bt_climate = ThermostatStandIn()
    bt_climate.unique_id = "test_bt"
    bt_climate.entity_id = entity_id
    entity = FollowsThermostat()
    entity._bt_climate = bt_climate
    entity.hass = MagicMock()
    entity.async_on_remove = MagicMock()
    with (
        patch(
            "custom_components.better_thermostat.entity.async_track_state_change_event"
        ) as track,
        patch(
            "custom_components.better_thermostat.entity.async_dispatcher_connect"
        ) as connect,
    ):
        entity._follow_thermostat()
    return entity, track, connect


def test_the_entity_is_not_polled():
    assert FollowsThermostat().should_poll is False


def test_the_entity_follows_the_thermostat_state_and_its_announcements():
    entity, track, connect = _following("climate.bt")

    assert track.call_args.args[1] == ["climate.bt"]
    assert connect.call_args.args[1] == LEARNED_STATE_SIGNAL.format("test_bt")
    assert entity.async_on_remove.call_count == 2


def test_a_thermostat_without_an_entity_id_still_announces_to_the_entity():
    """Before the thermostat is registered only its announcements can reach it."""
    entity, track, connect = _following(None)

    track.assert_not_called()
    assert connect.call_args.args[1] == LEARNED_STATE_SIGNAL.format("test_bt")
    assert entity.async_on_remove.call_count == 1
