"""Tests for the outdoor-sensor state-change handler in climate.py.

The outdoor temperature threshold used to be re-evaluated only at startup,
once per day at 05:00, and in the conditional 5-minute tick. ``_trigger_outdoor_change``
re-evaluates the threshold whenever the outdoor sensor changes, so heating
reacts promptly instead of waiting for the daily tick. Control is only
re-queued when ``call_for_heat`` actually flips, to avoid spamming the queue
on every outdoor reading.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat

CLIMATE_MOD = "custom_components.better_thermostat.climate"


def _make_self(*, call_for_heat_after, last_call_for_heat, in_maintenance=False):
    """Build a BetterThermostat stand-in for the outdoor-change handler."""
    bt = SimpleNamespace(
        hass=MagicMock(),
        entity_id="climate.test_bt",
        device_name="Test BT",
        in_maintenance=in_maintenance,
        call_for_heat=last_call_for_heat,
        _last_call_for_heat=last_call_for_heat,
        async_write_ha_state=MagicMock(),
        devices_errors=[],
        control_queue_task=MagicMock(put=AsyncMock()),
    )
    # The new outdoor reading drives call_for_heat to this value.
    bt._call_for_heat_after = call_for_heat_after
    return bt


def _patch_checks(bt, *, trv_gone=False):
    """Patch the watcher/weather helpers used by the handler.

    The ambient-check mock is stored on ``bt._ambient_mock`` so tests can
    assert whether the threshold was (re-)evaluated. With ``trv_gone`` the
    critical check finds a TRV away and records it, which is all it does.
    """

    async def _set_call_for_heat(_self):
        _self.call_for_heat = _self._call_for_heat_after

    def _record_gone_trv(_self):
        _self.devices_errors.append("climate.gone_trv")

    bt._ambient_mock = AsyncMock(side_effect=_set_call_for_heat)
    return patch.multiple(
        CLIMATE_MOD,
        check_critical_entities=AsyncMock(
            side_effect=_record_gone_trv if trv_gone else None
        ),
        check_and_update_degraded_mode=AsyncMock(),
        check_ambient_air_temperature=bt._ambient_mock,
    )


@pytest.mark.asyncio
async def test_outdoor_change_flips_call_for_heat_enqueues_control():
    """Threshold crossing (heat -> no heat) writes state and queues control."""
    bt = _make_self(call_for_heat_after=False, last_call_for_heat=True)

    with _patch_checks(bt):
        await BetterThermostat._trigger_outdoor_change(bt, event=MagicMock())

    assert bt.call_for_heat is False
    assert bt._last_call_for_heat is False
    bt.async_write_ha_state.assert_called_once()
    bt.control_queue_task.put_nowait.assert_called_once_with(bt)


@pytest.mark.asyncio
async def test_outdoor_change_no_flip_does_not_enqueue():
    """An outdoor reading that does not cross the threshold queues nothing."""
    bt = _make_self(call_for_heat_after=True, last_call_for_heat=True)

    with _patch_checks(bt):
        await BetterThermostat._trigger_outdoor_change(bt, event=MagicMock())

    bt.async_write_ha_state.assert_not_called()
    bt.control_queue_task.put_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_outdoor_change_is_evaluated_while_a_critical_entity_is_unavailable():
    """A TRV off the air does not hold back the threshold evaluation.

    Summer mode belongs to the room; the reachable TRVs follow the flip, and
    the control cycle leaves the unreachable one out.
    """
    bt = _make_self(call_for_heat_after=False, last_call_for_heat=True)

    with _patch_checks(bt, trv_gone=True):
        await BetterThermostat._trigger_outdoor_change(bt, event=MagicMock())

    bt._ambient_mock.assert_awaited_once()
    bt.control_queue_task.put_nowait.assert_called_once_with(bt)


@pytest.mark.asyncio
async def test_outdoor_change_skips_during_maintenance():
    """Valve maintenance suppresses the outdoor re-evaluation."""
    bt = _make_self(
        call_for_heat_after=False, last_call_for_heat=True, in_maintenance=True
    )

    with _patch_checks(bt):
        await BetterThermostat._trigger_outdoor_change(bt, event=MagicMock())

    bt._ambient_mock.assert_not_awaited()
    bt.control_queue_task.put_nowait.assert_not_called()
