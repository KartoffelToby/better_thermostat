"""Regression tests for optional per-device on/off heater control."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import Context, Event, State
import pytest

from custom_components.better_thermostat.config_flow import (
    _build_advanced_fields,
    _normalize_advanced_submission,
    _on_off_control_errors,
)
from custom_components.better_thermostat.events import trv as trv_events
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils import controlling
from custom_components.better_thermostat.utils.const import (
    CONF_ON_OFF_CONTROL,
    CalibrationType,
)
from custom_components.better_thermostat.utils.hvac_action import (
    ToleranceHysteresis,
    TrvSnapshot,
    compute_hvac_action,
)

ENTITY_ID = "climate.binary_heater"


@pytest.fixture
def binary_bt():
    device = Trv(
        entity_id=ENTITY_ID,
        model="generic",
        advanced={CONF_ON_OFF_CONTROL: True},
        hvac_modes=[HVACMode.HEAT, HVACMode.OFF],
        hvac_mode=HVACMode.HEAT,
        min_temp=0,
        max_temp=40,
        target_temp_step=1,
        last_temperature=20,
        last_hvac_mode=HVACMode.HEAT,
    )
    hass = MagicMock()
    hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    hass.states.get.return_value = State(
        ENTITY_ID, HVACMode.HEAT, {"temperature": 20, "hvac_modes": device.hvac_modes}
    )
    bt = SimpleNamespace(
        hass=hass,
        real_trvs={ENTITY_ID: device},
        all_trvs=[],
        device_name="Binary heater",
        bt_hvac_mode=HVACMode.HEAT,
        bt_target_temp=18,
        bt_target_cooltemp=None,
        bt_min_temp=0,
        bt_max_temp=40,
        bt_target_temp_step=0.5,
        cur_temp=18.4,
        tolerance=0.3,
        contact_open=False,
        window_open=False,
        call_for_heat=True,
        cooler_entity_id=None,
        ignore_states=False,
        startup_running=False,
        bt_update_lock=False,
        preset_mode="sleep",
        context=Context(),
        control_queue_task=asyncio.Queue(),
        async_write_ha_state=MagicMock(),
        attr_hvac_action=None,
        _temp_lock=asyncio.Lock(),
        _hysteresis=ToleranceHysteresis(),
        task_manager=MagicMock(),
    )

    def compute():
        return compute_hvac_action(
            hysteresis=bt._hysteresis,
            cur_temp=bt.cur_temp,
            target_temp=bt.bt_target_temp,
            cool_target=None,
            hvac_mode=bt.bt_hvac_mode,
            bt_hvac_mode=bt.bt_hvac_mode,
            window_open=bt.contact_open,
            tolerance=bt.tolerance,
            ignore_states=bt.ignore_states,
            trv_snapshots=[TrvSnapshot(ENTITY_ID, hvac_action=device.hvac_action)],
        )

    def commit(result):
        bt._hysteresis.last_action = result.new_last_action
        bt._hysteresis.hold_active = result.new_hold_active

    bt._compute_hvac_action_pure = compute
    bt._commit_hvac_action = commit
    bt.task_manager.create_task.side_effect = lambda coroutine, **kwargs: (
        coroutine.close()
    )
    return bt


@pytest.mark.parametrize(
    ("temperature", "previous", "expected"),
    [
        (18.4, HVACAction.HEATING, HVACMode.OFF),
        (18, HVACAction.HEATING, HVACMode.OFF),
        (17.9, HVACAction.HEATING, HVACMode.HEAT),
        (17.9, HVACAction.IDLE, HVACMode.OFF),
        (17.6, HVACAction.IDLE, HVACMode.HEAT),
    ],
)
def test_binary_payload_follows_existing_hysteresis(
    binary_bt, temperature, previous, expected
):
    binary_bt.cur_temp = temperature
    binary_bt._hysteresis.last_action = previous
    payload = trv_events.convert_outbound_states(binary_bt, ENTITY_ID, HVACMode.HEAT)
    assert payload == {"temperature": None, "system_mode": expected}


@pytest.mark.parametrize("suppression", ["contact_open", "off", "missing_sensor"])
def test_suppressed_heating_commands_off(binary_bt, suppression):
    binary_bt.cur_temp = 17
    if suppression == "contact_open":
        binary_bt.contact_open = True
    elif suppression == "off":
        binary_bt.bt_hvac_mode = HVACMode.OFF
    else:
        binary_bt.cur_temp = None
    payload = trv_events.convert_outbound_states(
        binary_bt, ENTITY_ID, binary_bt.bt_hvac_mode
    )
    assert payload["system_mode"] == HVACMode.OFF


def test_device_report_does_not_override_binary_decision(binary_bt):
    binary_bt.cur_temp = 17.9
    binary_bt.real_trvs[ENTITY_ID].hvac_action = HVACAction.HEATING
    assert binary_bt._compute_hvac_action_pure().action == HVACAction.HEATING
    payload = trv_events.convert_outbound_states(binary_bt, ENTITY_ID, HVACMode.HEAT)
    assert payload["system_mode"] == HVACMode.OFF


def test_binary_control_preserves_mode_remapping(binary_bt):
    binary_bt.cur_temp = 17
    device = binary_bt.real_trvs[ENTITY_ID]
    device.hvac_modes = [HVACMode.AUTO, HVACMode.OFF]
    device.advanced["heat_auto_swapped"] = True
    payload = trv_events.convert_outbound_states(binary_bt, ENTITY_ID, HVACMode.HEAT)
    assert payload["system_mode"] == HVACMode.AUTO


@pytest.mark.parametrize("enabled", [False, None])
def test_disabled_or_missing_option_keeps_setpoint_control(binary_bt, enabled):
    device = binary_bt.real_trvs[ENTITY_ID]
    device.advanced = {} if enabled is None else {CONF_ON_OFF_CONTROL: enabled}
    payload = trv_events.convert_outbound_states(binary_bt, ENTITY_ID, HVACMode.HEAT)
    assert payload["temperature"] == 18
    assert payload["system_mode"] == HVACMode.HEAT


@pytest.mark.parametrize("calibration", list(CalibrationType))
def test_binary_control_skips_calibration(binary_bt, monkeypatch, calibration):
    local = MagicMock()
    setpoint = MagicMock()
    monkeypatch.setattr(trv_events, "calculate_calibration_local", local)
    monkeypatch.setattr(trv_events, "calculate_calibration_setpoint", setpoint)
    binary_bt.real_trvs[ENTITY_ID].advanced["calibration"] = calibration
    payload = trv_events.convert_outbound_states(binary_bt, ENTITY_ID, HVACMode.HEAT)
    assert payload["temperature"] is None
    local.assert_not_called()
    setpoint.assert_not_called()


@pytest.mark.parametrize("saved", [{}, {CONF_ON_OFF_CONTROL: True}])
def test_advanced_form_defaults_and_saved_value(saved):
    fields = _build_advanced_fields(
        sources=(saved,),
        default_calibration=CalibrationType.TARGET_TEMP_BASED,
        homematic=False,
        has_auto=False,
    )
    marker = next(key for key in fields if key == CONF_ON_OFF_CONTROL)
    assert marker.default() is saved.get(CONF_ON_OFF_CONTROL, False)
    normalized = _normalize_advanced_submission(
        saved,
        default_calibration=CalibrationType.TARGET_TEMP_BASED,
        homematic=False,
        has_auto=False,
    )
    assert normalized[CONF_ON_OFF_CONTROL] is saved.get(CONF_ON_OFF_CONTROL, False)


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ({}, None),
        ({"no_off_system_mode": True}, "on_off_control_conflict"),
        ({"valve_maintenance": True}, "on_off_control_valve_maintenance"),
    ],
)
def test_incompatible_options_are_rejected(binary_bt, extra, error):
    errors = _on_off_control_errors(
        binary_bt.hass, ENTITY_ID, {CONF_ON_OFF_CONTROL: True, **extra}
    )
    assert errors == ({CONF_ON_OFF_CONTROL: error} if error else {})


def test_real_off_capability_is_required(binary_bt):
    binary_bt.hass.states.get.return_value = State(
        ENTITY_ID, HVACMode.HEAT, {"hvac_modes": [HVACMode.HEAT]}
    )
    assert _on_off_control_errors(
        binary_bt.hass, ENTITY_ID, {CONF_ON_OFF_CONTROL: True}
    ) == {CONF_ON_OFF_CONTROL: "on_off_control_requires_off"}
    assert _on_off_control_errors(binary_bt.hass, ENTITY_ID, {}) == {}


@pytest.mark.parametrize("opened", [False, True])
async def test_stop_commands_only_off_even_with_direct_valve_boost(
    binary_bt, monkeypatch, opened
):
    binary_bt.contact_open = opened
    binary_bt.cur_temp = 17 if opened else 18.4
    binary_bt.preset_mode = "boost"
    binary_bt.real_trvs[ENTITY_ID].advanced["calibration"] = (
        CalibrationType.DIRECT_VALVE_BASED
    )
    monkeypatch.setattr(controlling.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        controlling, "override_set_hvac_mode", AsyncMock(return_value=False)
    )
    mode = AsyncMock()
    monkeypatch.setattr(controlling, "set_hvac_mode", mode)
    writers = {}
    for name in ["set_temperature", "set_offset", "set_valve"]:
        writers[name] = AsyncMock()
        monkeypatch.setattr(controlling, name, writers[name])
    assert await controlling.control_trv(binary_bt, ENTITY_ID)
    mode.assert_awaited_once_with(binary_bt, ENTITY_ID, HVACMode.OFF)
    for writer in writers.values():
        writer.assert_not_awaited()
    assert binary_bt.bt_hvac_mode == HVACMode.HEAT


async def test_heating_restarts_below_the_hysteresis_band(binary_bt, monkeypatch):
    binary_bt.cur_temp = 17.6
    device = binary_bt.real_trvs[ENTITY_ID]
    device.hvac_mode = HVACMode.OFF
    device.last_hvac_mode = HVACMode.OFF
    binary_bt.hass.states.get.return_value = State(
        ENTITY_ID, HVACMode.OFF, {"temperature": 20, "hvac_modes": device.hvac_modes}
    )
    monkeypatch.setattr(controlling.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        controlling, "override_set_hvac_mode", AsyncMock(return_value=False)
    )
    mode = AsyncMock()
    temperature = AsyncMock()
    monkeypatch.setattr(controlling, "set_hvac_mode", mode)
    monkeypatch.setattr(controlling, "set_temperature", temperature)
    assert await controlling.control_trv(binary_bt, ENTITY_ID)
    mode.assert_awaited_once_with(binary_bt, ENTITY_ID, HVACMode.HEAT)
    temperature.assert_not_awaited()
    assert binary_bt.bt_hvac_mode == HVACMode.HEAT


@pytest.mark.parametrize("last_command", [HVACMode.OFF, HVACMode.HEAT])
async def test_off_echo_is_not_adopted_but_manual_off_is(binary_bt, last_command):
    device = binary_bt.real_trvs[ENTITY_ID]
    device.last_hvac_mode = last_command
    device.system_mode_received = True
    old = State(ENTITY_ID, HVACMode.HEAT, {"temperature": 20})
    new = State(
        ENTITY_ID, HVACMode.OFF, {"temperature": 20, "hvac_modes": device.hvac_modes}
    )
    binary_bt.hass.states.get.return_value = new
    await trv_events.trigger_trv_change(
        binary_bt,
        Event(
            "state_changed",
            {"entity_id": ENTITY_ID, "old_state": old, "new_state": new},
        ),
    )
    expected = HVACMode.HEAT if last_command == HVACMode.OFF else HVACMode.OFF
    assert binary_bt.bt_hvac_mode == expected
    assert binary_bt.bt_target_temp == 18
