"""Logbook annunciation (helpers.async_fire_logbook_entry).

The entry carries the translated message for *key* when the translation
catalogue can be read, and the caller-supplied default otherwise. Either way
the event is fired, and a catalogue that cannot be read is recorded at debug
level.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.utils.helpers import async_fire_logbook_entry

_HELPERS = "custom_components.better_thermostat.utils.helpers"
_TRANSLATIONS = "homeassistant.helpers.translation.async_get_translations"
_KEY = "component.better_thermostat.entity.sensor.logbook.state.window_open"


def _bt() -> MagicMock:
    """Build the caller surface async_fire_logbook_entry reads."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.hass.config.language = "de"
    bt.entity_id = "climate.test_bt"
    bt.name = "Test BT"
    return bt


def _fired(bt: MagicMock) -> dict:
    """Return the payload of the single fired logbook event."""
    bt.hass.bus.async_fire.assert_called_once()
    return bt.hass.bus.async_fire.call_args[0][1]


@pytest.mark.asyncio
async def test_translated_message_is_used():
    """A catalogue hit replaces the default message."""
    bt = _bt()
    with patch(_TRANSLATIONS, AsyncMock(return_value={_KEY: "Fenster offen"})):
        await async_fire_logbook_entry(bt, "window_open", "Window open")
    assert _fired(bt)["message"] == "Fenster offen"


@pytest.mark.asyncio
async def test_missing_translation_keeps_the_default():
    """A catalogue without the key keeps the default message."""
    bt = _bt()
    with patch(_TRANSLATIONS, AsyncMock(return_value={})):
        await async_fire_logbook_entry(bt, "window_open", "Window open")
    assert _fired(bt)["message"] == "Window open"


@pytest.mark.asyncio
async def test_unreadable_catalogue_is_traced_and_entry_still_fires(caplog):
    """A catalogue that raises still yields an entry with the default message."""
    bt = _bt()
    with (
        caplog.at_level(logging.DEBUG, logger=_HELPERS),
        patch(_TRANSLATIONS, AsyncMock(side_effect=RuntimeError("no catalogue"))),
    ):
        await async_fire_logbook_entry(bt, "window_open", "Window open")
    assert _fired(bt)["message"] == "Window open"
    assert "logbook translation for window_open unavailable" in caplog.text
    assert any(record.exc_info for record in caplog.records)
