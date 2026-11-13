"""A TRV whose offset stays at a limit of its range is reported, and only then.

Offset calibration keeps adding the gap between the room and the TRV's reading
to the offset. On a TRV that does not apply the offset the gap never closes,
and the offset rests at the limit of the range. The thermostat says so with a
repair issue once the offset asked for and the one reported have stayed at the
same limit for the whole window, with the gap asking for a correction past it,
and takes the issue back once a cycle finds any of them off it.
"""

import logging
from unittest.mock import patch

import pytest

from custom_components.better_thermostat.calibration import calculate_calibration_local
from custom_components.better_thermostat.core.offset_limit import (
    CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS,
    compute_offset_correction,
)
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.watcher import (
    clear_calibration_offset_at_limit,
    follow_calibration_offset_at_limit,
)
from tests.factories import DEFAULT_TRV_ID, make_bt

_WATCHER = "custom_components.better_thermostat.utils.watcher"
_BOUNDS = (-5.0, 5.0)
_ISSUE_ID = f"entry_calibration_offset_at_limit_{DEFAULT_TRV_ID}"
# One five-minute control tick.
_TICK_SECONDS = 300.0
# A room far enough from the head's reading that the correction lies past
# whichever limit the offsets sit at.
_ROOM_FOR_THE_LIMIT = object()


@pytest.fixture
def registry():
    """Stand in for Home Assistant's repair issue registry."""
    with patch(f"{_WATCHER}.ir") as registry:
        yield registry


@pytest.fixture
def bt():
    """A thermostat with one TRV whose offset runs from -5 to 5 in 0.1 steps."""
    bt = make_bt()
    bt._unique_id = "entry"
    return bt


def _cycle(
    bt,
    calibration_offset,
    reported_offset,
    bounds=_BOUNDS,
    room_temperature=_ROOM_FOR_THE_LIMIT,
) -> None:
    """Run one cycle the way the control cycle hands it over.

    The room is warm enough above the head's 21 °C reading at the upper limit,
    and cold enough below it at the lower one, that the correction lies past
    the limit the offsets sit at, unless ``room_temperature`` says otherwise.
    """
    trv = bt.real_trvs[DEFAULT_TRV_ID]
    if room_temperature is _ROOM_FOR_THE_LIMIT:
        room_temperature = 17.0 if (reported_offset or 0.0) < 0 else 25.0
    assert room_temperature is None or isinstance(room_temperature, float)
    run = trv.calibration_offset_at_limit
    trv.calibration_offset_at_limit = None
    follow_calibration_offset_at_limit(
        bt,
        DEFAULT_TRV_ID,
        calibration_offset,
        reported_offset,
        bounds,
        room_temperature,
        run,
    )


def _cycles_at(bt, calibration_offset, reported_offset, seconds) -> None:
    """Run a cycle every tick for ``seconds``, the first one now."""
    elapsed = 0.0
    while True:
        _cycle(bt, calibration_offset, reported_offset)
        if elapsed >= seconds:
            return
        step = min(_TICK_SECONDS, seconds - elapsed)
        bt.clock.advance(step)
        elapsed += step


def test_an_offset_at_its_limit_for_the_window_is_reported_once(bt, registry, caplog):
    """The issue comes after the window, with one warning, and names the TRV.

    Every cycle before the window is over raises nothing. Further cycles at
    the limit raise nothing new.
    """
    trv = bt.real_trvs[DEFAULT_TRV_ID]
    caplog.set_level(logging.WARNING)

    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS - 1.0)
    assert not trv.calibration_offset_at_limit_reported
    registry.async_create_issue.assert_not_called()

    bt.clock.advance(1.0)
    _cycle(bt, 5.0, 5.0)
    bt.clock.advance(_TICK_SECONDS)
    _cycle(bt, 5.0, 5.0)

    assert trv.calibration_offset_at_limit_reported
    registry.async_create_issue.assert_called_once()
    issue = registry.async_create_issue.call_args.kwargs
    assert issue["issue_id"] == _ISSUE_ID
    assert issue["translation_key"] == "calibration_offset_at_limit"
    assert issue["learn_more_url"] == (
        "https://better-thermostat.org/faq/calibration-offset-at-limit"
    )
    assert issue["severity"] == registry.IssueSeverity.WARNING
    assert issue["is_persistent"] is False
    assert issue["translation_placeholders"] == {
        "name": "Test BT",
        "entity": DEFAULT_TRV_ID,
        "minutes": "60",
    }
    warnings = [r for r in caplog.records if "has stayed at" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


def test_the_lower_limit_is_reported_as_well(bt, registry):
    """A TRV that reads too warm runs its offset down to the lowest one."""
    _cycles_at(bt, -4.9, -5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS)

    assert bt.real_trvs[DEFAULT_TRV_ID].calibration_offset_at_limit_reported
    registry.async_create_issue.assert_called_once()


def test_an_offset_leaving_the_limit_starts_the_window_again(bt, registry):
    """One cycle off the limit is enough to show the TRV follows its offset."""
    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS - 60.0)
    bt.clock.advance(30.0)
    _cycle(bt, 3.0, 5.0)
    bt.clock.advance(30.0)
    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS - 1.0)

    registry.async_create_issue.assert_not_called()


@pytest.mark.parametrize(
    ("calibration_offset", "reported_offset"),
    [
        pytest.param(5.0, 3.0, id="the TRV holds an offset off the limit"),
        pytest.param(5.0, -5.0, id="the TRV holds the other limit"),
        pytest.param(3.0, 5.0, id="the intent is off the limit"),
    ],
)
def test_both_offsets_have_to_sit_at_the_same_limit(
    bt, registry, calibration_offset, reported_offset
):
    """An offset the TRV does not hold is not an offset resting at the limit."""
    _cycles_at(
        bt,
        calibration_offset,
        reported_offset,
        2 * CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS,
    )

    assert bt.real_trvs[DEFAULT_TRV_ID].calibration_offset_at_limit is None
    registry.async_create_issue.assert_not_called()


def test_the_limit_is_the_range_the_cycle_hands_over(bt, registry):
    """An offset at 5 is no limit for a TRV whose range reaches 12.7."""
    elapsed = 0.0
    while elapsed <= 2 * CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS:
        _cycle(bt, 5.0, 5.0, bounds=(-12.7, 12.7))
        bt.clock.advance(_TICK_SECONDS)
        elapsed += _TICK_SECONDS

    registry.async_create_issue.assert_not_called()


@pytest.mark.parametrize(
    ("calibration_offset", "reported_offset"),
    [
        pytest.param(None, 5.0, id="the cycle follows no offset"),
        pytest.param(5.0, None, id="the TRV reports no readable offset"),
    ],
)
def test_a_cycle_that_observes_nothing_breaks_the_run(
    bt, registry, calibration_offset, reported_offset
):
    """The window wants an unbroken run, so a blind cycle starts it again."""
    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS - 60.0)
    _cycle(bt, calibration_offset, reported_offset)
    bt.clock.advance(60.0)
    _cycle(bt, 5.0, 5.0)

    registry.async_create_issue.assert_not_called()
    run = bt.real_trvs[DEFAULT_TRV_ID].calibration_offset_at_limit
    assert run is not None
    assert run.since_monotonic == bt.clock.monotonic()


def test_a_cycle_that_observes_nothing_leaves_a_raised_report(bt, registry):
    """Only a cycle that sees the offset off the limit takes the issue back."""
    trv = bt.real_trvs[DEFAULT_TRV_ID]
    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS)
    assert trv.calibration_offset_at_limit_reported

    _cycle(bt, None, None)

    assert trv.calibration_offset_at_limit_reported
    registry.async_delete_issue.assert_not_called()


def test_the_report_goes_once_the_offset_leaves_the_limit(bt, registry, caplog):
    """The issue is deleted, once, when a cycle finds the offset off the limit."""
    trv = bt.real_trvs[DEFAULT_TRV_ID]
    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS)
    assert trv.calibration_offset_at_limit_reported
    caplog.set_level(logging.INFO)

    _cycle(bt, 2.0, 5.0)
    _cycle(bt, 2.0, 2.0)

    assert not trv.calibration_offset_at_limit_reported
    assert trv.calibration_offset_at_limit is None
    registry.async_delete_issue.assert_called_once_with(
        bt.hass, "better_thermostat", _ISSUE_ID
    )
    assert any("has left the limit" in r.getMessage() for r in caplog.records)


def test_a_report_comes_back_after_another_full_window(bt, registry):
    """A TRV that ends up at the limit again is reported again, after the window."""
    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS)
    _cycle(bt, 2.0, 2.0)
    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS - 1.0)
    assert registry.async_create_issue.call_count == 1

    bt.clock.advance(1.0)
    _cycle(bt, 5.0, 5.0)

    assert registry.async_create_issue.call_count == 2


def test_clearing_an_unreported_trv_deletes_nothing(bt, registry):
    """Taking back a report that was not raised leaves the registry alone."""
    clear_calibration_offset_at_limit(bt, DEFAULT_TRV_ID)

    registry.async_delete_issue.assert_not_called()


def test_an_offset_the_head_applies_at_its_limit_is_not_reported(bt, registry):
    """A head whose reading carries the offset needs no correction past the range.

    A calibration mode or overheating protection can hold the offset at a
    limit on purpose; the gap between room and reading then asks for no more
    than the range holds.
    """
    trv = bt.real_trvs[DEFAULT_TRV_ID]
    trv.current_temperature = 26.0
    elapsed = 0.0
    while elapsed <= 2 * CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS:
        _cycle(bt, 5.0, 5.0, room_temperature=21.5)
        bt.clock.advance(_TICK_SECONDS)
        elapsed += _TICK_SECONDS

    assert trv.calibration_offset_at_limit is None
    registry.async_create_issue.assert_not_called()


def test_a_correction_back_inside_the_range_takes_the_report_back(bt, registry):
    """A head that starts applying its offset is no longer stuck at the limit."""
    trv = bt.real_trvs[DEFAULT_TRV_ID]
    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS)
    assert trv.calibration_offset_at_limit_reported

    trv.current_temperature = 21.0 + 5.0
    _cycle(bt, 5.0, 5.0, room_temperature=25.0)

    assert not trv.calibration_offset_at_limit_reported
    registry.async_delete_issue.assert_called_once()


@pytest.mark.parametrize(
    "missing", ["room temperature", "head reading"], ids=lambda name: f"no {name}"
)
def test_a_cycle_without_a_temperature_observes_nothing(bt, registry, missing):
    """Without both temperatures there is no correction to judge."""
    trv = bt.real_trvs[DEFAULT_TRV_ID]
    _cycles_at(bt, 5.0, 5.0, CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS)
    assert trv.calibration_offset_at_limit_reported
    if missing == "head reading":
        trv.current_temperature = None
        _cycle(bt, 5.0, 5.0)
    else:
        _cycle(bt, 5.0, 5.0, room_temperature=None)

    assert trv.calibration_offset_at_limit is None
    assert trv.calibration_offset_at_limit_reported
    registry.async_delete_issue.assert_not_called()


class _SetpointShiftingQuirks:
    """The quirk of a head that adds its offset to its setpoint, as COZB0001 does."""

    def local_calibration_shifts_setpoint(self, host, entity_id, /) -> bool:
        return True

    def fix_local_calibration(self, host, entity_id, calibration_offset, /) -> float:
        return float(calibration_offset)


@pytest.mark.parametrize(
    ("calibration_offset", "reported"),
    [
        pytest.param(-5.0, True, id="the lower limit the negated gap asks for"),
        pytest.param(5.0, False, id="the upper limit the negated gap does not ask for"),
    ],
)
def test_a_head_that_shifts_its_setpoint_is_judged_by_the_negated_gap(
    bt, registry, calibration_offset, reported
):
    """A head that reads 6 K below the room asks for a 6 K lower setpoint offset.

    Its reading carries no offset, so the offset it holds does not move the
    correction: at the lower limit the correction lies past it, at the upper
    one it does not.
    """
    trv = bt.real_trvs[DEFAULT_TRV_ID]
    trv.model_quirks = _SetpointShiftingQuirks()
    trv.current_temperature = 19.0
    elapsed = 0.0
    while elapsed <= CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS:
        _cycle(bt, calibration_offset, calibration_offset, room_temperature=25.0)
        bt.clock.advance(_TICK_SECONDS)
        elapsed += _TICK_SECONDS

    assert trv.calibration_offset_at_limit_reported is reported


@pytest.mark.parametrize("shifts_setpoint", [False, True])
def test_the_correction_is_the_one_offset_calibration_starts_from(bt, shifts_setpoint):
    """The judged correction is the offset Default calibration writes.

    Default calibration applies no mode, tolerance or protection on top of
    the gap, so what it computes is the correction itself.
    """
    trv = bt.real_trvs[DEFAULT_TRV_ID]
    trv.advanced = {"calibration_mode": CalibrationMode.DEFAULT}
    trv.current_temperature = 19.0
    trv.last_calibration = 2.0
    trv.local_calibration_step = 0.1
    if shifts_setpoint:
        trv.model_quirks = _SetpointShiftingQuirks()
    bt.room_temperature = 22.5
    bt.hvac_action = None
    bt.attr_hvac_action = None

    calibrated = calculate_calibration_local(bt, DEFAULT_TRV_ID)

    assert calibrated == pytest.approx(
        compute_offset_correction(22.5, 19.0, 2.0, shifts_setpoint=shifts_setpoint)
    )
