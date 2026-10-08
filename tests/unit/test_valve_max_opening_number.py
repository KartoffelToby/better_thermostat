"""Tests for the maximum-valve-opening number entity.

Home Assistant validates a ``number.set_value`` payload with ``value <
min_value or value > max_value``. That rejects the infinities but not
``NaN``, for which both comparisons are false. A value restored from the
store or set from inside the integration passes no such check at all, so
all three can reach the entity. Clamping one against ``0..100`` does not
remove it, it disguises it as a plausible limit, which is why the entity
has to recognise it as unusable itself.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.calibration import _get_trv_max_opening
from custom_components.better_thermostat.number import (
    BetterThermostatValveMaxOpeningNumber,
)
from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn

_TRV_ID = "climate.living_room"


@pytest.fixture(autouse=True)
def _detached_state_tracking():
    """Let the entity subscribe to the thermostat's state without a hass."""
    with (
        patch(
            "custom_components.better_thermostat.entity.async_track_state_change_event",
            MagicMock(),
        ),
        patch(
            "custom_components.better_thermostat.entity.async_dispatcher_connect",
            MagicMock(),
        ),
    ):
        yield


def _make_entity() -> tuple[
    BetterThermostatValveMaxOpeningNumber, Trv, ThermostatStandIn
]:
    """Return the cap entity wired to one real ``Trv`` it can write to."""
    trv = Trv(entity_id=_TRV_ID)
    bt_climate = ThermostatStandIn()
    bt_climate.unique_id = "test_bt"
    bt_climate.device_name = "Test BT"
    bt_climate.real_trvs = {_TRV_ID: trv}
    entity = BetterThermostatValveMaxOpeningNumber(
        bt_climate, _TRV_ID, show_trv_name=False
    )
    entity.async_write_ha_state = MagicMock()
    return entity, trv, bt_climate


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
async def test_non_finite_cap_leaves_the_configured_limit_in_place(
    caplog, payload: float
) -> None:
    """A cap that is not a number keeps the one the user set, and says so.

    Clamped instead, ``NaN`` and ``+inf`` would come out as 100 % — the cap
    would stop limiting anything — and ``-inf`` as 0 %, holding the valve
    shut, both without a trace.
    """
    entity, trv, bt_climate = _make_entity()
    await entity.async_set_native_value(40.0)

    with caplog.at_level(logging.DEBUG):
        await entity.async_set_native_value(payload)

    assert trv.valve_max_opening == 40.0
    assert entity.native_value == 40.0
    assert _get_trv_max_opening(bt_climate, _TRV_ID) == 40.0
    reports = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(reports) == 1
    assert _TRV_ID in reports[0].getMessage()


@pytest.mark.parametrize(
    ("payload", "stored"),
    [
        pytest.param(40.0, 40.0, id="inside-the-range"),
        pytest.param(150.0, 100.0, id="above-the-range"),
        pytest.param(-5.0, 0.0, id="below-the-range"),
    ],
)
async def test_finite_cap_is_clamped_into_the_percent_range(
    caplog, payload: float, stored: float
) -> None:
    """A number keeps being clamped into ``0..100`` and reported by the entity."""
    entity, trv, _ = _make_entity()

    with caplog.at_level(logging.DEBUG):
        await entity.async_set_native_value(payload)

    assert trv.valve_max_opening == stored
    assert entity.native_value == stored
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_restored_state_that_is_not_a_number_keeps_the_default(caplog) -> None:
    """A saved state that parses to ``NaN`` leaves the entity at its default."""
    entity, trv, _ = _make_entity()
    last_state = MagicMock()
    last_state.state = "nan"
    entity.async_get_last_state = AsyncMock(return_value=last_state)
    entity.async_get_last_extra_data = AsyncMock(return_value=None)

    with caplog.at_level(logging.DEBUG):
        await entity.async_added_to_hass()

    assert trv.valve_max_opening == 100.0
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


async def test_restored_state_that_is_no_number_keeps_the_cap() -> None:
    """A saved state that does not parse leaves the cap the head holds."""
    entity, trv, _ = _make_entity()
    trv.valve_max_opening = 60.0
    entity.async_get_last_state = AsyncMock(
        return_value=State("number.bt_valve_max_opening", "open")
    )
    entity.async_get_last_extra_data = AsyncMock(return_value=None)

    await entity.async_added_to_hass()

    assert trv.valve_max_opening == 60.0
    assert entity.native_value == 60.0


async def test_the_cap_of_a_head_not_built_is_the_full_opening() -> None:
    """A head the thermostat could not build shows no cap and takes none.

    Its entity stays registered so it comes back once the head is built.
    """
    entity, _, bt_climate = _make_entity()
    bt_climate.real_trvs = {}

    await entity.async_set_native_value(40.0)

    assert entity.native_value == 100.0
    entity.async_write_ha_state.assert_called_once()
