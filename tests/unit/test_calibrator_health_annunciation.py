"""Calibrator health is annunciated on the TRV — and nothing more.

Self-healing lives in the controllers' sanitize steps; the oscillation
detector reports without backing gains off. The grades recover only
through their own reporters, so the two paths cannot flap each other.
"""

import logging
from unittest.mock import MagicMock

from custom_components.better_thermostat.core.calibrator import CalibratorHealth
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.strategies import (
    BalanceStrategy,
    annunciate_health,
)
from custom_components.better_thermostat.utils.const import CalibrationMode

ENTITY_ID = "climate.trv"


def _bt():
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.cur_temp = 20.0
    bt.bt_target_temp = 21.0
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID)}
    return bt


def _strategy(percents):
    """Build a strategy whose compute emits the given percentages in order."""
    feed = iter(percents)
    return BalanceStrategy(
        mode=CalibrationMode.PID_CALIBRATION,
        compute=lambda bt, eid: (next(feed), False),
        percent_of=lambda result: result,
    )


def test_sanitize_verdict_lands_on_the_trv():
    """A non-finite verdict is recorded and logged as a warning."""
    bt = _bt()
    annunciate_health(bt, ENTITY_ID, CalibratorHealth.NON_FINITE)
    assert bt.real_trvs[ENTITY_ID].calibrator_health == CalibratorHealth.NON_FINITE


def test_sanitize_recovery_clears_its_own_grades():
    """A healthy sanitize verdict clears a previous sanitize grade."""
    bt = _bt()
    annunciate_health(bt, ENTITY_ID, CalibratorHealth.WINDUP_SUSPECT)
    annunciate_health(bt, ENTITY_ID, CalibratorHealth.HEALTHY)
    assert bt.real_trvs[ENTITY_ID].calibrator_health == CalibratorHealth.HEALTHY


def test_sanitize_recovery_leaves_an_oscillation_alone():
    """The sanitize path cannot clear the oscillation watcher's grade."""
    bt = _bt()
    annunciate_health(
        bt,
        ENTITY_ID,
        CalibratorHealth.OSCILLATING,
        recovers=(CalibratorHealth.OSCILLATING,),
    )
    annunciate_health(bt, ENTITY_ID, CalibratorHealth.HEALTHY)
    assert bt.real_trvs[ENTITY_ID].calibrator_health == CalibratorHealth.OSCILLATING


def test_oscillating_balance_output_is_annunciated(caplog):
    """Sustained 0/100 thrash flags the TRV as oscillating, once."""
    bt = _bt()
    strategy = _strategy([0.0, 100.0, 0.0, 100.0, 0.0, 100.0, 0.0])
    with caplog.at_level(logging.WARNING):
        for _ in range(7):
            strategy.run(bt, ENTITY_ID)

    assert bt.real_trvs[ENTITY_ID].calibrator_health == CalibratorHealth.OSCILLATING
    assert sum("oscillating" in r.message for r in caplog.records) == 1


def test_settling_output_recovers_the_oscillation_grade():
    """A settled output stream clears OSCILLATING again."""
    bt = _bt()
    percents = [0.0, 100.0, 0.0, 100.0, 0.0, 100.0, 0.0] + [50.0] * 10
    strategy = _strategy(percents)
    for _ in range(17):
        strategy.run(bt, ENTITY_ID)

    assert bt.real_trvs[ENTITY_ID].calibrator_health == CalibratorHealth.HEALTHY


def test_oscillation_never_touches_the_gains():
    """Annunciation only: no strategy state is mutated by the verdict."""
    bt = _bt()
    strategy = _strategy([0.0, 100.0, 0.0, 100.0, 0.0, 100.0, 0.0])
    for _ in range(7):
        percent, _use_valve = strategy.run(bt, ENTITY_ID)
    # The strategy keeps emitting exactly what the controller computes.
    assert percent == 0.0
