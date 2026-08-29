"""Every configuration gets a five-minute availability tick.

Two jobs live in the recurring handlers. One is the control recompute the
balance and calibration modes need, and only some modes need it. The other
is advancing the degradation ladder and re-checking the critical entities,
and every configuration needs that: the ladder commits a downgrade after a
120-second debounce and an upgrade after 300 seconds of stability, so a
periodic evaluation slower than those windows leaves transitions pending.

The event handlers advance the ladder too, but the case the ladder exists
for is a sensor that stopped reporting, and a sensor that stopped reporting
produces no events. What is left is the periodic path, which is why it has
to exist for every configuration rather than for the modes that happen to
want a recompute as well.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.utils.const import (
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
)
from tests.unit.test_climate_startup_registration import (
    _run_finalize_startup,
    _startup_bt,
)

_CLIMATE = "custom_components.better_thermostat.climate"

# Both handlers that carry the ladder on a five-minute interval. Which one a
# configuration gets depends on whether it also wants the recompute.
_LADDER_TICKS = ("_trigger_time", "_availability_tick")


def _five_minute_ladder_tick(bt, registered):
    """Return the name of the ladder-advancing five-minute tick, or None."""
    for name in _LADDER_TICKS:
        if (getattr(bt, name), timedelta(minutes=5)) in registered.intervals:
            return name
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [m.value for m in CalibrationMode])
async def test_every_calibration_mode_gets_a_five_minute_ladder_tick(mode):
    """No mode is left with the hourly weather tick as its only ladder step.

    The recompute is gated and stays gated. What this claims is only that
    something advances the ladder every five minutes, whichever of the two
    handlers a mode ends up with.
    """
    bt = _startup_bt(advanced={"calibration_mode": mode})

    registered = await _run_finalize_startup(bt)

    assert _five_minute_ladder_tick(bt, registered) is not None


@pytest.mark.asyncio
async def test_the_default_calibration_mode_gets_one_too():
    """The mode a fresh installation lands on, named rather than assumed.

    ``CalibrationMode.DEFAULT`` and ``DEFAULT_CALIBRATION_MODE`` are two
    different values, and the second one is what the config flow writes.
    """
    bt = _startup_bt(advanced={"calibration_mode": DEFAULT_CALIBRATION_MODE.value})

    registered = await _run_finalize_startup(bt)

    assert _five_minute_ladder_tick(bt, registered) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (CalibrationMode.PID_CALIBRATION.value, "_trigger_time"),
        (CalibrationMode.MPC_CALIBRATION.value, "_trigger_time"),
        (CalibrationMode.HEATING_POWER_CALIBRATION.value, "_availability_tick"),
        (CalibrationMode.NO_CALIBRATION.value, "_availability_tick"),
        (CalibrationMode.AGGRESIVE_CALIBRATION.value, "_availability_tick"),
    ],
)
async def test_only_the_recomputing_modes_get_the_recomputing_tick(mode, expected):
    """The gate decides which of the two handlers runs.

    A mode that does not recompute must not start queueing a control cycle
    every five minutes: that is radio traffic to a battery device, and it is
    what the gate exists to prevent.
    """
    bt = _startup_bt(advanced={"calibration_mode": mode})

    registered = await _run_finalize_startup(bt)

    assert _five_minute_ladder_tick(bt, registered) == expected


@pytest.mark.asyncio
async def test_the_availability_tick_advances_the_ladder_and_rechecks_entities():
    """What the tick does when it fires, in the order the ladder needs.

    The ladder steps before the critical-entity check, so it keeps stepping
    while an unreachable valve would abort a handler that checked first.
    """
    bt = MagicMock()
    bt.device_name = "Test BT"
    calls = []
    degraded = AsyncMock(side_effect=lambda _self: calls.append("ladder"))
    critical = AsyncMock(side_effect=lambda _self: calls.append("critical"))

    with (
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", degraded),
        patch(f"{_CLIMATE}.check_critical_entities", critical),
    ):
        await BetterThermostat._availability_tick(bt)

    assert calls == ["ladder", "critical"]


@pytest.mark.asyncio
async def test_the_availability_tick_queues_no_control_cycle():
    """It is the half of the recurring work that touches no device.

    A mode without the recompute gets this tick precisely so that the
    ladder keeps stepping without the writes the recompute brings.
    """
    bt = MagicMock()
    bt.device_name = "Test BT"
    request = MagicMock()

    with (
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.request_control_cycle", request),
    ):
        await BetterThermostat._availability_tick(bt)

    request.assert_not_called()


@pytest.mark.asyncio
async def test_an_unreachable_valve_does_not_stop_the_ladder():
    """The check reporting failure is not a reason to skip the ladder.

    A room sensor lost while a valve is offline is the combined case the
    ladder has to keep stepping through.
    """
    bt = MagicMock()
    bt.device_name = "Test BT"
    degraded = AsyncMock()

    with (
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", degraded),
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=False)),
    ):
        await BetterThermostat._availability_tick(bt)

    degraded.assert_awaited_once_with(bt)


class _TrvMapThatCannotBeRead(dict):
    """A TRV map whose iteration fails, as a corrupted cache would."""

    def values(self):
        """Raise instead of yielding, the way a broken cache reads."""
        raise RuntimeError("TRV cache is unreadable")


@pytest.mark.asyncio
async def test_an_unreadable_trv_map_still_gets_the_ladder_tick():
    """The tick every configuration needs cannot depend on reading the modes.

    Deciding which of the two five-minute handlers to register means reading
    each head's balance and calibration mode. That read failing says nothing
    about the ladder, which still has to step, so the run falls back to the
    availability tick rather than to no tick at all.
    """
    bt = _startup_bt()
    bt.real_trvs = _TrvMapThatCannotBeRead()

    registered = await _run_finalize_startup(bt)

    assert _five_minute_ladder_tick(bt, registered) == "_availability_tick"


@pytest.mark.asyncio
async def test_an_unreadable_maintenance_list_leaves_the_other_ticks_standing():
    """Valve maintenance is the one tick a failed read may cost.

    It is orthogonal to the ladder: a head list that cannot be read names no
    valve to exercise, and the startup carries on with the ticks that do not
    depend on it.
    """
    bt = _startup_bt()

    with patch(
        f"{_CLIMATE}.collect_maintenance_trvs",
        MagicMock(side_effect=RuntimeError("TRV cache is unreadable")),
    ):
        registered = await _run_finalize_startup(bt)

    assert (bt._maintenance_tick, timedelta(minutes=5)) not in registered.intervals
    assert _five_minute_ladder_tick(bt, registered) is not None
