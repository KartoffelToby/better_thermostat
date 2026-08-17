"""The quirk that runs for every device without a file of its own.

Eleven models carry a quirk module; everything else is driven by this
one, which makes it the most-executed quirk code in the field. Its two
halves are tested apart because they promise different things:

* the ``fix_*`` and ``override_*`` functions promise to do nothing —
  they hand the value back untouched and decline every override, so the
  standard path runs. A quirk that starts changing values here changes
  them for the whole install base;
* ``initial_tweak`` promises to put a freshly adopted device into a
  known state: calibration back to zero, child lock following the
  configuration, and the device's own window and away detection off,
  because Better Thermostat does both itself. Each of those is optional,
  independent, and must not take the others down when it fails.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.lock import LockState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.model_fixes import default as default_quirk
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.trv"
DEVICE_ID = "device-1"

CALIBRATION_ENTITY = "number.trv_local_temperature_calibration"
CHILD_LOCK_SWITCH = "switch.trv_child_lock"
CHILD_LOCK_LOCK = "lock.trv_child_lock"
WINDOW_SWITCH = "switch.trv_window_detection"
AWAY_SWITCH = "switch.trv_away_mode"

# Which of ``initial_tweak``'s four lookups a keyword list belongs to.
_LOOKUP_BY_KEYWORD = {
    "local_temperature_calibration": "calibration",
    "child_lock": "child_lock",
    "window_detection": "window",
    "away_mode": "away",
}


def _thermostat(child_lock=None, states=None):
    """Build a thermostat whose service calls are recorded, not executed.

    Parameters
    ----------
    child_lock : bool or None
        The configured child lock, or None for a configuration that
        carries no such setting.
    states : dict or None
        Current state string per entity ID; an entity left out of it
        reads as unknown to Home Assistant.

    Returns
    -------
    MagicMock
        A stand-in for the Better Thermostat climate entity instance.
    """
    states = states or {}
    thermostat = MagicMock()
    thermostat.device_name = "Test BT"
    thermostat.hass = MagicMock()
    thermostat.hass.services.async_call = AsyncMock()
    thermostat.hass.states.get = lambda requested: (
        State(requested, states[requested]) if requested in states else None
    )
    advanced = {} if child_lock is None else {"child_lock": child_lock}
    thermostat.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID, advanced=advanced)}
    return thermostat


def _registry(device_id=DEVICE_ID, known=True):
    """An entity registry answering for one entity.

    Parameters
    ----------
    device_id : str or None
        The device the entity belongs to, or None for an entity that
        belongs to no device.
    known : bool
        Whether the registry knows the entity at all.
    """
    registry = MagicMock()
    registry.async_get.return_value = MagicMock(device_id=device_id) if known else None
    return registry


def _discovering(**found):
    """Stand in for the device-entity lookup with a fixed outcome.

    Parameters
    ----------
    **found
        Entity ID per lookup name (``calibration``, ``child_lock``,
        ``window``, ``away``); a lookup left out finds nothing.
    """

    def _find(_registry, _device_id, _domains, keywords):
        for keyword in keywords:
            lookup = _LOOKUP_BY_KEYWORD.get(keyword)
            if lookup is not None:
                return found.get(lookup)
        return None

    return _find


async def _run_tweak(thermostat, registry=None, **found):
    """Run initial_tweak against a fixed registry and lookup outcome."""
    with (
        patch.object(
            default_quirk.er, "async_get", return_value=registry or _registry()
        ),
        patch.object(
            default_quirk, "find_device_entity", side_effect=_discovering(**found)
        ),
    ):
        await default_quirk.initial_tweak(thermostat, ENTITY_ID)


def _calls(thermostat):
    """Service calls the run recorded, as (domain, service, payload)."""
    return [
        (call.args[0], call.args[1], call.args[2])
        for call in thermostat.hass.services.async_call.await_args_list
    ]


class TestTheDefaultQuirkChangesNothing:
    """The passthrough half: values come back as they went in."""

    @pytest.mark.parametrize("offset", [0.0, -2.5, 7.0])
    def test_a_local_calibration_is_handed_back_untouched(self, offset):
        """The offset reaches the device as it was calculated."""
        assert (
            default_quirk.fix_local_calibration(_thermostat(), ENTITY_ID, offset)
            == offset
        )

    @pytest.mark.parametrize("valve", [0, 42, 100])
    def test_a_valve_calibration_is_handed_back_untouched(self, valve):
        """The valve percentage reaches the device as it was calculated."""
        assert (
            default_quirk.fix_valve_calibration(_thermostat(), ENTITY_ID, valve)
            == valve
        )

    @pytest.mark.parametrize("temperature", [5.0, 21.5, 30.0])
    def test_a_target_temperature_is_handed_back_untouched(self, temperature):
        """The setpoint reaches the device as it was calculated."""
        assert (
            default_quirk.fix_target_temperature_calibration(
                _thermostat(), ENTITY_ID, temperature
            )
            == temperature
        )

    @pytest.mark.asyncio
    async def test_the_hvac_mode_write_is_not_overridden(self):
        """Declining the override is what lets the adapter write."""
        assert (
            await default_quirk.override_set_hvac_mode(_thermostat(), ENTITY_ID, "heat")
            is False
        )

    @pytest.mark.asyncio
    async def test_the_temperature_write_is_not_overridden(self):
        """Declining the override is what lets the adapter write."""
        assert (
            await default_quirk.override_set_temperature(_thermostat(), ENTITY_ID, 21.0)
            is False
        )

    @pytest.mark.asyncio
    async def test_the_valve_write_is_not_overridden(self):
        """Declining the override is what lets the adapter write."""
        assert (
            await default_quirk.override_set_valve(_thermostat(), ENTITY_ID, 50)
            is False
        )


class TestAdoptionNeedsADevice:
    """Without a device there is nothing on it to look up."""

    @pytest.mark.asyncio
    async def test_an_entity_the_registry_does_not_know_is_left_alone(self):
        """A YAML entity carries no registry row and no device."""
        thermostat = _thermostat(child_lock=True)

        await _run_tweak(
            thermostat,
            registry=_registry(known=False),
            calibration=CALIBRATION_ENTITY,
            child_lock=CHILD_LOCK_SWITCH,
        )

        assert _calls(thermostat) == []

    @pytest.mark.asyncio
    async def test_an_entity_without_a_device_is_left_alone(self):
        """A registry row without a device has no siblings to find."""
        thermostat = _thermostat(child_lock=True)

        await _run_tweak(
            thermostat,
            registry=_registry(device_id=None),
            calibration=CALIBRATION_ENTITY,
            child_lock=CHILD_LOCK_SWITCH,
        )

        assert _calls(thermostat) == []

    @pytest.mark.asyncio
    async def test_a_device_offering_none_of_the_helpers_is_left_alone(self):
        """Every step is optional, so a bare device gets no calls."""
        thermostat = _thermostat(child_lock=True)

        await _run_tweak(thermostat)

        assert _calls(thermostat) == []


class TestCalibrationStartsFromZero:
    """A device's own offset is cleared before BT starts writing one."""

    @pytest.mark.asyncio
    async def test_a_discovered_calibration_entity_is_reset(self):
        """The device's residual offset would otherwise add to BT's."""
        thermostat = _thermostat()

        await _run_tweak(thermostat, calibration=CALIBRATION_ENTITY)

        assert _calls(thermostat) == [
            ("number", "set_value", {"entity_id": CALIBRATION_ENTITY, "value": 0})
        ]

    @pytest.mark.asyncio
    async def test_a_failing_reset_does_not_stop_the_remaining_steps(self):
        """One unavailable helper must not abort the adoption."""
        thermostat = _thermostat(child_lock=True, states={CHILD_LOCK_SWITCH: STATE_OFF})
        thermostat.hass.services.async_call = AsyncMock(
            side_effect=[RuntimeError("entity unavailable"), None]
        )

        await _run_tweak(
            thermostat, calibration=CALIBRATION_ENTITY, child_lock=CHILD_LOCK_SWITCH
        )

        assert _calls(thermostat)[-1] == (
            "switch",
            "turn_on",
            {"entity_id": CHILD_LOCK_SWITCH},
        )


class TestTheChildLockFollowsTheConfiguration:
    """The device's lock is brought to the configured state, once."""

    @pytest.mark.parametrize(
        ("configured", "current", "service"),
        [(True, STATE_OFF, "turn_on"), (False, STATE_ON, "turn_off")],
    )
    @pytest.mark.asyncio
    async def test_a_switch_away_from_the_configured_state_is_moved(
        self, configured, current, service
    ):
        """A Zigbee2MQTT child lock is a switch."""
        thermostat = _thermostat(
            child_lock=configured, states={CHILD_LOCK_SWITCH: current}
        )

        await _run_tweak(thermostat, child_lock=CHILD_LOCK_SWITCH)

        assert _calls(thermostat) == [
            ("switch", service, {"entity_id": CHILD_LOCK_SWITCH})
        ]

    @pytest.mark.parametrize(
        ("configured", "current"), [(True, STATE_ON), (False, STATE_OFF)]
    )
    @pytest.mark.asyncio
    async def test_a_switch_already_in_the_configured_state_is_left_alone(
        self, configured, current
    ):
        """Adoption does not write what the device already reports."""
        thermostat = _thermostat(
            child_lock=configured, states={CHILD_LOCK_SWITCH: current}
        )

        await _run_tweak(thermostat, child_lock=CHILD_LOCK_SWITCH)

        assert _calls(thermostat) == []

    @pytest.mark.parametrize(
        ("configured", "current", "service"),
        [
            (True, LockState.UNLOCKED.value, "lock"),
            (False, LockState.LOCKED.value, "unlock"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_lock_away_from_the_configured_state_is_moved(
        self, configured, current, service
    ):
        """Other ecosystems expose the same lock in the lock domain."""
        thermostat = _thermostat(
            child_lock=configured, states={CHILD_LOCK_LOCK: current}
        )

        await _run_tweak(thermostat, child_lock=CHILD_LOCK_LOCK)

        assert _calls(thermostat) == [("lock", service, {"entity_id": CHILD_LOCK_LOCK})]

    @pytest.mark.parametrize(
        ("configured", "current"),
        [(True, LockState.LOCKED.value), (False, LockState.UNLOCKED.value)],
    )
    @pytest.mark.asyncio
    async def test_a_lock_already_in_the_configured_state_is_left_alone(
        self, configured, current
    ):
        """Adoption does not write what the device already reports."""
        thermostat = _thermostat(
            child_lock=configured, states={CHILD_LOCK_LOCK: current}
        )

        await _run_tweak(thermostat, child_lock=CHILD_LOCK_LOCK)

        assert _calls(thermostat) == []

    @pytest.mark.asyncio
    async def test_an_unreadable_lock_entity_is_left_alone(self):
        """A helper that reports nothing yet is not written blindly."""
        thermostat = _thermostat(child_lock=True)

        await _run_tweak(thermostat, child_lock=CHILD_LOCK_SWITCH)

        assert _calls(thermostat) == []

    @pytest.mark.asyncio
    async def test_a_configuration_without_the_setting_looks_for_no_lock(self):
        """Nothing is assumed about a lock the user never configured."""
        thermostat = _thermostat(states={CHILD_LOCK_SWITCH: STATE_OFF})

        await _run_tweak(thermostat, child_lock=CHILD_LOCK_SWITCH)

        assert _calls(thermostat) == []

    @pytest.mark.asyncio
    async def test_a_failing_child_lock_write_does_not_stop_the_remaining_steps(self):
        """One unavailable helper must not abort the adoption."""
        thermostat = _thermostat(
            child_lock=True,
            states={CHILD_LOCK_SWITCH: STATE_OFF, WINDOW_SWITCH: STATE_ON},
        )
        thermostat.hass.services.async_call = AsyncMock(
            side_effect=[RuntimeError("entity unavailable"), None]
        )

        await _run_tweak(thermostat, child_lock=CHILD_LOCK_SWITCH, window=WINDOW_SWITCH)

        assert _calls(thermostat)[-1] == (
            "switch",
            "turn_off",
            {"entity_id": WINDOW_SWITCH},
        )


class TestTheDeviceStopsDetectingOnItsOwn:
    """Window and away detection are Better Thermostat's job, not the TRV's."""

    @pytest.mark.parametrize(
        ("lookup", "entity"), [("window", WINDOW_SWITCH), ("away", AWAY_SWITCH)]
    )
    @pytest.mark.asyncio
    async def test_an_enabled_detection_is_switched_off(self, lookup, entity):
        """Both detections would otherwise fight Better Thermostat's."""
        thermostat = _thermostat(states={entity: STATE_ON})

        await _run_tweak(thermostat, **{lookup: entity})

        assert _calls(thermostat) == [("switch", "turn_off", {"entity_id": entity})]

    @pytest.mark.parametrize(
        ("lookup", "entity"), [("window", WINDOW_SWITCH), ("away", AWAY_SWITCH)]
    )
    @pytest.mark.asyncio
    async def test_a_detection_already_off_is_left_alone(self, lookup, entity):
        """Adoption does not write what the device already reports."""
        thermostat = _thermostat(states={entity: STATE_OFF})

        await _run_tweak(thermostat, **{lookup: entity})

        assert _calls(thermostat) == []

    @pytest.mark.parametrize(
        ("lookup", "entity"), [("window", WINDOW_SWITCH), ("away", AWAY_SWITCH)]
    )
    @pytest.mark.asyncio
    async def test_an_unreadable_detection_is_left_alone(self, lookup, entity):
        """A helper that reports nothing yet is not written blindly."""
        thermostat = _thermostat()

        await _run_tweak(thermostat, **{lookup: entity})

        assert _calls(thermostat) == []

    @pytest.mark.asyncio
    async def test_a_failing_window_write_does_not_stop_the_away_step(self):
        """The two detections are independent of each other."""
        thermostat = _thermostat(
            states={WINDOW_SWITCH: STATE_ON, AWAY_SWITCH: STATE_ON}
        )
        thermostat.hass.services.async_call = AsyncMock(
            side_effect=[RuntimeError("entity unavailable"), None]
        )

        await _run_tweak(thermostat, window=WINDOW_SWITCH, away=AWAY_SWITCH)

        assert _calls(thermostat)[-1] == (
            "switch",
            "turn_off",
            {"entity_id": AWAY_SWITCH},
        )

    @pytest.mark.asyncio
    async def test_a_failing_away_write_is_swallowed(self):
        """The last step fails as quietly as the ones before it."""
        thermostat = _thermostat(states={AWAY_SWITCH: STATE_ON})
        thermostat.hass.services.async_call = AsyncMock(
            side_effect=RuntimeError("entity unavailable")
        )

        await _run_tweak(thermostat, away=AWAY_SWITCH)

        assert len(_calls(thermostat)) == 1


class TestAdoptionRunsEveryStep:
    """A device offering all four helpers gets all four visited."""

    @pytest.mark.asyncio
    async def test_a_fully_equipped_device_is_brought_into_a_known_state(self):
        """One adoption, four independent commands, in order."""
        thermostat = _thermostat(
            child_lock=True,
            states={
                CHILD_LOCK_SWITCH: STATE_OFF,
                WINDOW_SWITCH: STATE_ON,
                AWAY_SWITCH: STATE_ON,
            },
        )

        await _run_tweak(
            thermostat,
            calibration=CALIBRATION_ENTITY,
            child_lock=CHILD_LOCK_SWITCH,
            window=WINDOW_SWITCH,
            away=AWAY_SWITCH,
        )

        assert _calls(thermostat) == [
            ("number", "set_value", {"entity_id": CALIBRATION_ENTITY, "value": 0}),
            ("switch", "turn_on", {"entity_id": CHILD_LOCK_SWITCH}),
            ("switch", "turn_off", {"entity_id": WINDOW_SWITCH}),
            ("switch", "turn_off", {"entity_id": AWAY_SWITCH}),
        ]
