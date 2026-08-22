"""Local-calibration quirks that read the room temperature and the setpoint.

Four quirk modules compare ``cur_temp`` against ``bt_target_temp`` before
they adjust the offset they were handed. Both members are optional on the
host: the entity carries no room temperature until the startup sequence
resolves one, and the DEFAULT calibration mode reaches these functions
without a setpoint, because ``calculate_calibration_local`` only demands a
setpoint for the modes whose traits declare they need it.

The comparison therefore has to survive either value being absent. Each
module answers with the branch it takes when the room is not known to be
below the setpoint: BHT-002-GCLZB still rounds the offset to an integer,
which is the whole point of that module, and the three that only nudge the
offset conditionally hand it back untouched.
"""

from importlib import import_module
from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.model_fixes import (
    TS0601 as ts0601_quirk,
    TS0601_thermostat as ts0601_thermostat_quirk,
)
from custom_components.better_thermostat.trv import Trv

# Two of the modules carry a hyphen in the device name they are called
# after, so they are reached by name rather than by an import statement.
BHT_002_GCLZB = import_module(
    "custom_components.better_thermostat.model_fixes.BHT-002-GCLZB"
)
SEA801 = import_module(
    "custom_components.better_thermostat.model_fixes.SEA801-Zigbee_SEA802-Zigbee"
)

ENTITY_ID = "climate.trv"

# The three modules that share one shape: compare, nudge conditionally,
# and otherwise return the offset unchanged.
CONDITIONAL_NUDGE_QUIRKS = [ts0601_quirk, ts0601_thermostat_quirk, SEA801]


def _thermostat(cur_temp, bt_target_temp):
    """Build a host reporting the given room temperature and setpoint."""
    bt = MagicMock()
    bt.cur_temp = cur_temp
    bt.bt_target_temp = bt_target_temp
    bt.device_name = "test"
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID)}
    return bt


@pytest.mark.parametrize(
    ("cur_temp", "bt_target_temp"), [(None, 21.0), (20.0, None), (None, None)]
)
@pytest.mark.parametrize("quirk", CONDITIONAL_NUDGE_QUIRKS)
def test_conditional_nudge_returns_offset_when_a_reading_is_missing(
    quirk, cur_temp, bt_target_temp
):
    """Without both readings there is nothing to compare, so nothing changes."""
    bt = _thermostat(cur_temp, bt_target_temp)

    assert quirk.fix_local_calibration(bt, ENTITY_ID, 1.3) == 1.3


@pytest.mark.parametrize("quirk", CONDITIONAL_NUDGE_QUIRKS)
def test_conditional_nudge_still_adjusts_with_both_readings(quirk):
    """A room at or above the setpoint keeps the documented +0.5 nudge."""
    bt = _thermostat(cur_temp=21.0, bt_target_temp=20.0)

    assert quirk.fix_local_calibration(bt, ENTITY_ID, 1.3) == 1.8


@pytest.mark.parametrize(
    ("cur_temp", "bt_target_temp"), [(None, 21.0), (20.0, None), (None, None)]
)
def test_bht_002_rounds_down_when_a_reading_is_missing(cur_temp, bt_target_temp):
    """The heating direction is unknown, so the sanitizing round-down applies."""
    bt = _thermostat(cur_temp, bt_target_temp)

    assert BHT_002_GCLZB.fix_local_calibration(bt, ENTITY_ID, 1.7) == 1.0


def test_bht_002_rounds_up_while_the_room_is_below_the_setpoint():
    """A room below its setpoint keeps the ceiling direction."""
    bt = _thermostat(cur_temp=19.0, bt_target_temp=21.0)

    assert BHT_002_GCLZB.fix_local_calibration(bt, ENTITY_ID, 1.2) == 2.0


def test_bht_002_rounds_down_once_the_room_reached_the_setpoint():
    """A room at or above its setpoint keeps the floor direction."""
    bt = _thermostat(cur_temp=21.0, bt_target_temp=21.0)

    assert BHT_002_GCLZB.fix_local_calibration(bt, ENTITY_ID, 1.7) == 1.0
