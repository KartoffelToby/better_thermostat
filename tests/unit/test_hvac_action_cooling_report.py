"""The reported cooling action is derived from the cooler command, not beside it.

``control_cooler`` decides with ``should_cool_with_tolerance``, a minimum band
of ``COOLER_MODE_HYSTERESIS_K``, a latch carrying the hold edge and the heating
target as a hard floor. ``compute_hvac_action`` reports ``COOLING`` under
exactly those conditions, so the two agree by construction.
"""

import types
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.snapshot import HvacMode as CoreHvacMode
from custom_components.better_thermostat.utils.controlling import control_cooler
from custom_components.better_thermostat.utils.hvac_action import (
    COOLER_MODE_HYSTERESIS_K,
    ToleranceHysteresis,
    compute_hvac_action,
    should_cool_with_tolerance,
)
from tests.factories import make_snapshot

COOLER_ID = "climate.air_conditioner"


def build_bt(
    cur_temp,
    target_temp=21.0,
    cool_target=24.0,
    tolerance=0.5,
    decided_mode=None,
    reported_mode=HVACMode.OFF,
    cooler_entity_id=COOLER_ID,
):
    """Return a BT mock with the real hvac-action methods bound to it."""
    bt = MagicMock()
    bt.tolerance = tolerance
    bt.bt_target_temp = target_temp
    bt.bt_target_cooltemp = cool_target
    bt.cur_temp = cur_temp
    bt.hvac_mode = HVACMode.HEAT_COOL
    bt.bt_hvac_mode = HVACMode.HEAT_COOL
    bt.contact_open = False
    bt.ignore_states = False
    bt.real_trvs = {}
    bt._hysteresis = ToleranceHysteresis()
    bt.device_name = "Test"
    bt.cooler_entity_id = cooler_entity_id
    bt._cooler_last_sent = (
        {} if decided_mode is None else {"hvac_mode_decided": decided_mode}
    )

    cooler_state = MagicMock()
    cooler_state.state = reported_mode
    bt.hass.states.get.return_value = cooler_state

    bt._build_trv_snapshots = types.MethodType(
        BetterThermostat._build_trv_snapshots, bt
    )
    bt._cooler_previously_active = types.MethodType(
        BetterThermostat._cooler_previously_active, bt
    )
    bt._compute_hvac_action_pure = types.MethodType(
        BetterThermostat._compute_hvac_action_pure, bt
    )
    return bt


def test_report_holds_cooling_inside_the_hold_band():
    """A running cooler keeps reporting COOLING below the switch-on edge."""
    bt = build_bt(cur_temp=24.2, decided_mode=HVACMode.COOL)
    assert bt._compute_hvac_action_pure().action == HVACAction.COOLING


def test_report_does_not_start_cooling_inside_the_hold_band():
    """A stopped cooler does not report COOLING below the switch-on edge."""
    bt = build_bt(cur_temp=24.2, decided_mode=HVACMode.OFF)
    assert bt._compute_hvac_action_pure().action == HVACAction.IDLE


def test_report_starts_cooling_at_the_switch_on_edge():
    """At cooling target plus tolerance the report turns to COOLING."""
    bt = build_bt(cur_temp=24.5, decided_mode=HVACMode.OFF)
    assert bt._compute_hvac_action_pure().action == HVACAction.COOLING


def test_report_drops_cooling_below_the_hold_edge():
    """Below the cooling target a running cooler stops being reported."""
    bt = build_bt(cur_temp=23.9, decided_mode=HVACMode.COOL)
    assert bt._compute_hvac_action_pure().action == HVACAction.IDLE


def test_heating_target_floors_the_cooling_report():
    """At or below the heating target nothing is reported as cooling.

    A cooling target close to the heating target lets the minimum band reach
    under the heating target; the cooler command refuses to run there and the
    report follows it.
    """
    kwargs = {
        "target_temp": 21.0,
        "cool_target": 21.2,
        "tolerance": 0.0,
        "decided_mode": HVACMode.COOL,
    }
    on_the_floor = build_bt(cur_temp=21.0, **kwargs)
    above_the_floor = build_bt(cur_temp=21.05, **kwargs)

    assert on_the_floor._compute_hvac_action_pure().action == HVACAction.IDLE
    assert above_the_floor._compute_hvac_action_pure().action == HVACAction.COOLING


def test_cooling_report_does_not_hold_the_heating_tolerance():
    """A reported cooling action clears the tolerance hold."""
    bt = build_bt(cur_temp=24.2, decided_mode=HVACMode.COOL)
    result = bt._compute_hvac_action_pure()
    assert result.action == HVACAction.COOLING
    assert result.new_hold_active is False
    assert result.new_last_action == HVACAction.IDLE


def test_seed_follows_the_latched_cooler_decision():
    """The latched decision decides the hold edge whenever it holds one."""
    latched_on = build_bt(
        cur_temp=24.2, decided_mode=HVACMode.COOL, reported_mode=HVACMode.OFF
    )
    latched_off = build_bt(
        cur_temp=24.2, decided_mode=HVACMode.OFF, reported_mode=HVACMode.COOL
    )

    assert latched_on._cooler_previously_active() is True
    assert latched_off._cooler_previously_active() is False


def test_seed_falls_back_to_the_reported_cooler_mode():
    """Without a latched decision the cooler's own mode seeds the hold edge."""
    running = build_bt(cur_temp=24.2, decided_mode=None, reported_mode=HVACMode.COOL)
    stopped = build_bt(cur_temp=24.2, decided_mode=None, reported_mode=HVACMode.OFF)

    assert running._cooler_previously_active() is True
    assert stopped._cooler_previously_active() is False
    assert running._compute_hvac_action_pure().action == HVACAction.COOLING
    assert stopped._compute_hvac_action_pure().action == HVACAction.IDLE


def test_seed_is_off_without_a_cooler():
    """An installation without a cooler never holds the cooling band open."""
    bt = build_bt(cur_temp=24.2, decided_mode=HVACMode.COOL, cooler_entity_id=None)
    assert bt._cooler_previously_active() is False


@pytest.mark.parametrize("target_temp, cool_target", [(21.0, 24.0), (21.0, 21.5)])
@pytest.mark.parametrize("tolerance", [0.0, 0.2, 0.5, 1.0])
@pytest.mark.parametrize("cool_previously_active", [False, True])
@pytest.mark.parametrize("offset", [-0.5, -0.2, -0.1, 0.0, 0.1, 0.2, 0.5, 1.0, 2.0])
def test_report_agrees_with_the_command(
    target_temp, cool_target, tolerance, cool_previously_active, offset
):
    """The reported cooling action matches what control_cooler would command."""
    cur_temp = round(cool_target + offset, 2)
    reported = compute_hvac_action(
        hysteresis=ToleranceHysteresis(),
        cur_temp=cur_temp,
        target_temp=target_temp,
        cool_target=cool_target,
        hvac_mode=HVACMode.HEAT_COOL,
        bt_hvac_mode=HVACMode.HEAT_COOL,
        window_open=False,
        tolerance=tolerance,
        ignore_states=False,
        trv_snapshots=[],
        cool_previously_active=cool_previously_active,
    ).action
    commanded = (
        should_cool_with_tolerance(
            cur_temp,
            cool_target,
            tolerance,
            cool_previously_active,
            min_band=COOLER_MODE_HYSTERESIS_K,
        )
        and cur_temp > target_temp
    )

    assert (reported == HVACAction.COOLING) is commanded


@pytest.mark.asyncio
async def test_command_and_report_agree_across_a_temperature_sweep():
    """Both real entry points agree at every step of a rise and a fall.

    The sweep crosses the switch-on edge, walks through the hold band and
    leaves it underneath, which is where two independent decisions drift
    apart.
    """
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get.return_value = State(
        "climate.cooler", HVACMode.OFF, {"temperature": 24.0}
    )

    bt = build_bt(cur_temp=21.5, cool_target=24.0, target_temp=21.0, tolerance=0.5)
    bt.hass = hass
    bt.context = None
    bt.cooler_entity_id = "climate.cooler"
    bt.clock = FakeClock()
    bt._cooler_last_sent = {}

    rise = [21.5, 23.0, 24.0, 24.2, 24.4, 24.5, 24.6, 25.0]
    sweep = rise + list(reversed(rise))
    seen = {}
    for temp in sweep:
        bt.cur_temp = temp
        snapshot = make_snapshot(
            hvac_mode=CoreHvacMode.HEAT_COOL,
            room_temp=temp,
            target_temp=21.0,
            target_cooltemp=24.0,
            tolerance=0.5,
            trvs={},
        )
        await control_cooler(bt, snapshot)
        commanded = bt._cooler_last_sent.get("hvac_mode_decided")
        reported = bt._compute_hvac_action_pure().action
        assert (reported == HVACAction.COOLING) is (commanded == HVACMode.COOL), temp
        seen[temp] = commanded

    # The sweep has to reach the hold band, or agreement is trivial.
    assert seen[25.0] == HVACMode.COOL
    assert seen[24.2] == HVACMode.COOL
    assert seen[23.0] == HVACMode.OFF
