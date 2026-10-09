"""The timing constants hold the relations their comments claim.

Better Thermostat's timing is spread over constants in several modules and
over the intervals ``_finalize_startup`` registers. Many of them only work
relative to another one: a tick that has to come round within a debounce
window, a backoff that has to stay below the tick that retries anyway, a
budget that has to outlast the retry ladder it wraps. The comment next to
each constant states the relation, and nothing else checks it, so changing
one side leaves the other silently wrong.

Each test pins one such relation and names the comment that claims it. The
intervals are read from the registrations a real ``_finalize_startup`` run
makes, not from the constants, because a constant that is right and a
registration that uses a different one is the failure this module exists
for.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.adapters.delegate import _write_on_channel
from custom_components.better_thermostat.climate import STARTUP_CONTROL_BUDGET_S
from custom_components.better_thermostat.core.fsm.control_mode import LadderParams
from custom_components.better_thermostat.core.watchdog import (
    CONTROL_TICK_S,
    WATCHDOG_MAX_AGE_S,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.dob import (
    DobParams,
)
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.controlling import (
    COOLER_FAILURE_BACKOFF_BASE_S,
    COOLER_FAILURE_BACKOFF_MAX_S,
    COOLER_RESEND_INTERVAL_S,
    FAILED_CYCLE_BACKOFF_MAX_S,
    FAILED_CYCLE_WARNING_INTERVAL_S,
    TRV_STATE_SETTLE_S,
)
from custom_components.better_thermostat.utils.state_manager import (
    COPY_RETRY_FIRST_S,
    COPY_RETRY_MAX_S,
)
from tests.unit.test_climate_startup_registration import (
    _run_finalize_startup,
    _startup_bt,
)

_RETRY = "custom_components.better_thermostat.utils.retry"


def _interval_s(bt, registered, name):
    """The interval, in seconds, ``name`` was registered on.

    Fails when the tick is missing or registered on more than one interval,
    since either would make the relation below meaningless.
    """
    target = getattr(bt, name)
    intervals = [
        interval for callback, interval in registered.intervals if callback is target
    ]
    assert len(intervals) == 1, f"{name} registered on {intervals}"
    return intervals[0].total_seconds()


async def _registered(advanced):
    bt = _startup_bt(advanced=advanced)
    return bt, await _run_finalize_startup(bt)


@pytest.fixture
async def recomputing():
    """A configuration whose calibration mode runs the five-minute recompute."""
    return await _registered({"calibration_mode": "pid_calibration"})


@pytest.fixture
async def not_recomputing():
    """A configuration without a recompute, the default install among them."""
    return await _registered({})


# ---------------------------------------------------------------------------
# The degradation ladder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_registered_ladder_tick_comes_round_within_both_windows(
    recomputing, not_recomputing
):
    """``control_mode.LADDER_TICK_S``: "shorter than both windows".

    A sensor that goes quiet produces no events, so the ladder tick supplies
    the evaluation that commits a rung. A tick as long as a window lets that
    commit land almost a whole further window late.
    """
    params = LadderParams()
    for bt, registered in (recomputing, not_recomputing):
        tick = _interval_s(bt, registered, "_availability_tick")

        assert tick < min(params.down_debounce_seconds, params.up_stability_seconds)


# ---------------------------------------------------------------------------
# The control watchdog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_cycle_driving_tick_comes_round_within_the_watchdog_age(
    recomputing, not_recomputing
):
    """``watchdog.WATCHDOG_MAX_AGE_S`` against the ticks that queue cycles.

    The watchdog reports a stall once no control cycle has completed for
    its age. A periodic tick that queues cycles and comes round less often
    than that would make a healthy loop look stalled between two firings.
    """
    bt, registered = recomputing
    assert _interval_s(bt, registered, "_trigger_time") < WATCHDOG_MAX_AGE_S
    for bt, registered in (recomputing, not_recomputing):
        assert _interval_s(bt, registered, "_reconcile_tick") < WATCHDOG_MAX_AGE_S


# ---------------------------------------------------------------------------
# The recompute tick and what reads it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        CalibrationMode.MPC_V2_CALIBRATION.value,
        CalibrationMode.MPC_CALIBRATION.value,
        CalibrationMode.PID_CALIBRATION.value,
        CalibrationMode.TPI_CALIBRATION.value,
    ],
)
async def test_a_controller_mode_recomputes_at_least_once_per_control_tick(mode):
    """``watchdog.CONTROL_TICK_S`` and ``dob.DobParams.max_reading_interval_s``.

    Both comments say a controller mode sees a recompute at least once per
    ``CONTROL_TICK_S``. MPC v2 takes a longer gap between two readings as a
    pause of the controller rather than a disturbance, so a slower tick
    would make every ordinary interval look like a pause.
    """
    bt, registered = await _registered({"calibration_mode": mode})

    assert _interval_s(bt, registered, "_trigger_time") <= CONTROL_TICK_S
    assert DobParams().max_reading_interval_s > CONTROL_TICK_S


# ---------------------------------------------------------------------------
# Cooler pacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cooler_resend_interval_sits_in_the_compressor_band_below_the_ticks(
    recomputing,
):
    """``controlling.COOLER_RESEND_INTERVAL_S``.

    Its comment places it between the three minutes manufacturers state
    and the five a dedicated thermostat holds the compressor off, and
    strictly below the periodic ticks that drive a control cycle. At one
    tick period the pacing would depend on scheduling jitter.
    """
    bt, registered = recomputing
    ticks = (
        _interval_s(bt, registered, "_reconcile_tick"),
        _interval_s(bt, registered, "_trigger_time"),
        WATCHDOG_MAX_AGE_S,
    )

    assert 180.0 <= COOLER_RESEND_INTERVAL_S <= 300.0
    assert COOLER_RESEND_INTERVAL_S < min(ticks)


def test_the_cooler_failure_backoff_starts_below_and_ends_above_the_resend():
    """``controlling.COOLER_FAILURE_BACKOFF_BASE_S`` and ``_MAX_S``.

    A rejected command never reached the device, so its first retry does
    not wait out a compressor window; a channel that keeps being rejected
    is paced well beyond it.
    """
    assert COOLER_FAILURE_BACKOFF_BASE_S < COOLER_RESEND_INTERVAL_S
    assert COOLER_FAILURE_BACKOFF_MAX_S > COOLER_RESEND_INTERVAL_S


# ---------------------------------------------------------------------------
# Failed control cycles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_failed_cycle_backoff_tops_out_at_the_reconcile_tick(not_recomputing):
    """``controlling.FAILED_CYCLE_BACKOFF_MAX_S``.

    The reconcile tick queues a cycle for a device that does not hold what
    it was sent anyway, so a longer pause would not space the attempts any
    further and would only delay the retry that picks up a recovered device.
    """
    bt, registered = not_recomputing

    assert FAILED_CYCLE_BACKOFF_MAX_S <= _interval_s(bt, registered, "_reconcile_tick")


def test_a_failing_run_is_reported_less_often_than_it_is_retried():
    """``controlling.FAILED_CYCLE_WARNING_INTERVAL_S``.

    The warning summarises a run of attempts at the backoff ceiling; at the
    same interval or shorter it would repeat on every attempt.
    """
    assert FAILED_CYCLE_WARNING_INTERVAL_S > FAILED_CYCLE_BACKOFF_MAX_S


# ---------------------------------------------------------------------------
# The startup control budget
# ---------------------------------------------------------------------------


async def _worst_case_write_ladder_s():
    """Seconds one write channel spends retrying a device that never answers.

    Measured rather than recomputed: the write goes through the real retry
    wrapper against a device that always fails, with the jitter pinned to
    its upper end, and the sleeps it asks for are added up.
    """
    host = SimpleNamespace(
        device_name="Test BT",
        real_trvs={"climate.trv": SimpleNamespace(unreachable_write_channels={})},
    )
    write = AsyncMock(side_effect=HomeAssistantError("device unreachable"))
    sleeps = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    with (
        patch(f"{_RETRY}.asyncio.sleep", record_sleep),
        patch(f"{_RETRY}.random.uniform", lambda low, high: high),
        pytest.raises(HomeAssistantError),
    ):
        await _write_on_channel(
            host, "climate.trv", "setpoint", "setpoint", write, 21.0
        )

    assert write.await_count == len(sleeps) + 1
    return sum(sleeps)


@pytest.mark.asyncio
async def test_the_startup_control_budget_outlasts_one_full_write_ladder():
    """``climate.STARTUP_CONTROL_BUDGET_S``.

    Its comment requires the budget to outlast the retry ladder of the
    write it wraps, jitter included, plus the settle pause the control call
    ends with. A shorter budget cancels the write with attempts unspent,
    which is exactly the device that is still waking up after a restart.
    """
    ladder_s = await _worst_case_write_ladder_s()

    assert ladder_s + TRV_STATE_SETTLE_S < STARTUP_CONTROL_BUDGET_S


# ---------------------------------------------------------------------------
# Quarantine copies
# ---------------------------------------------------------------------------


def test_a_failed_quarantine_copy_is_retried_within_the_hour():
    """``state_manager.COPY_RETRY_FIRST_S`` and ``COPY_RETRY_MAX_S``.

    The retry doubles from the first delay up to the cap, and the comment
    promises that a disk which recovers is used within the hour.
    """
    assert COPY_RETRY_FIRST_S < COPY_RETRY_MAX_S <= timedelta(hours=1).total_seconds()
