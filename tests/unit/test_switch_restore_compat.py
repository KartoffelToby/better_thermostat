"""Startup child-lock restoration works with either Home Assistant cache API."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.better_thermostat import switch as switch_module

BT_ID = "bt_compat"
TRV_ID = "climate.trv"


@pytest.mark.parametrize("public_api", [True, False], ids=["ha-2026.11", "legacy"])
@pytest.mark.parametrize("saved", [STATE_ON, STATE_OFF, STATE_UNKNOWN, None])
def test_startup_reads_the_saved_child_lock_with_either_cache_api(
    hass, public_api, saved
):
    entity = er.async_get(hass).async_get_or_create(
        "switch",
        "better_thermostat",
        f"{BT_ID}_{TRV_ID}_child_lock",
        suggested_object_id="renamed_switch",
    )
    stored = (
        SimpleNamespace(state=State(entity.entity_id, saved), extra_data=None)
        if saved is not None
        else None
    )
    # No last_states on the new cache: a direct legacy read must fail.
    lookup = Mock(return_value=stored)
    cache = (
        SimpleNamespace(async_get_stored_state=lookup)
        if public_api
        else SimpleNamespace(last_states={entity.entity_id: stored})
    )
    with patch.object(switch_module.restore_state, "async_get", return_value=cache):
        actual = switch_module.restored_child_lock(
            hass, BT_ID, TRV_ID, configured=False
        )

    expected = saved == STATE_ON if saved in (STATE_ON, STATE_OFF) else None
    assert actual is expected
    if public_api:
        lookup.assert_called_once_with(entity.entity_id)
    else:
        lookup.assert_not_called()
