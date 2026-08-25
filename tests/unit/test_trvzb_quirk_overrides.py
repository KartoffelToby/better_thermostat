"""Tests for the TRVZB setpoint, HVAC mode and valve override quirks."""

import asyncio
import contextlib
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.better_thermostat.trv import Trv

quirk = importlib.import_module("custom_components.better_thermostat.model_fixes.TRVZB")


def _make_self():
    """Create a mock BetterThermostat with a spied service-call layer."""
    mock_self = MagicMock()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.hass.services.async_call = AsyncMock()
    return mock_self


class TestOverrideSetTemperature:
    """The quirk declines so the generic adapter performs the write."""

    @pytest.mark.asyncio
    async def test_returns_false_without_service_call(self):
        """The override returns False and issues no service call."""
        mock_self = _make_self()

        handled = await quirk.override_set_temperature(mock_self, "climate.trv1", 21.0)

        assert handled is False
        mock_self.hass.services.async_call.assert_not_awaited()


class TestOverrideSetHvacMode:
    """The quirk declines so the generic adapter performs the write."""

    @pytest.mark.asyncio
    async def test_returns_false_without_service_call(self):
        """The override returns False and issues no service call."""
        mock_self = _make_self()

        handled = await quirk.override_set_hvac_mode(mock_self, "climate.trv1", "heat")

        assert handled is False
        mock_self.hass.services.async_call.assert_not_awaited()


ENTITY = "climate.trv1"


def _make_valve_self(last_pct=40, *, in_maintenance=False):
    """Create a mock BetterThermostat whose TRV records a commanded valve percent."""
    mock_self = _make_self()
    mock_self.in_maintenance = in_maintenance
    trv_state = Trv(entity_id=ENTITY)
    trv_state.last_valve_percent = last_pct
    mock_self.real_trvs = {ENTITY: trv_state}
    mock_self.hass.async_create_background_task = lambda coro, name=None: (
        asyncio.ensure_future(coro)
    )
    return mock_self, trv_state


async def _settle(task):
    """Cancel a scheduled valve write, if there is one, and wait for it.

    ``Task.cancel()`` only requests cancellation, so a test that ends on it
    leaves the write pending into teardown. ``None`` stands for a call that
    scheduled nothing, which is a state several of these tests assert on.
    """
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.fixture
def writes(monkeypatch):
    """Record every valve percentage the quirk puts on the wire."""
    recorded = []

    async def _write(_self, _entity_id, percent):
        recorded.append(percent)
        return True

    monkeypatch.setattr(quirk, "maybe_set_sonoff_valve_percent", _write)
    return recorded


class TestOverrideSetValve:
    """The de-sticking bump must never cost the requested position."""

    @pytest.mark.asyncio
    async def test_a_close_bumps_open_and_defers_the_target(self, writes):
        """A close drives the valve open first and schedules the target."""
        mock_self, trv_state = _make_valve_self(last_pct=40)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 30)
        task = trv_state.extra.get("_trvzb_valve_bump_task")

        try:
            assert handled is True
            assert writes == [50]
            assert task is not None and not task.done()
        finally:
            await _settle(task)

    @pytest.mark.asyncio
    async def test_a_close_superseding_a_due_bump_writes_the_target(self, writes):
        """A close arriving before the deferred write lands goes out directly."""
        mock_self, trv_state = _make_valve_self(last_pct=40)
        await quirk.override_set_valve(mock_self, ENTITY, 30)
        first_task = trv_state.extra["_trvzb_valve_bump_task"]
        trv_state.last_valve_percent = 30

        handled = await quirk.override_set_valve(mock_self, ENTITY, 20)
        await asyncio.sleep(0)

        assert handled is True
        # 50 is the de-sticking bump of the first close; 20 is the new target.
        # A second bump would drive the valve open again and drop the target.
        assert writes == [50, 20]
        assert first_task.done()
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_repeated_closes_always_land_the_latest_target(
        self, writes, monkeypatch
    ):
        """Closes faster than the delay still put the newest position on the wire."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 30.0)
        mock_self, trv_state = _make_valve_self(last_pct=40)

        for target in (38, 36, 34, 32):
            await quirk.override_set_valve(mock_self, ENTITY, target)
            trv_state.last_valve_percent = target
        await asyncio.sleep(0)

        await _settle(trv_state.extra.get("_trvzb_valve_bump_task"))

        assert writes[-1] == 32, (
            "the newest requested position never reached the device"
        )
        assert max(writes) == 50, "the valve was driven further open than any bump"

    @pytest.mark.asyncio
    async def test_a_completed_bump_does_not_suppress_the_next_de_stick(
        self, writes, monkeypatch
    ):
        """Once the deferred write has run, the next close bumps again."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.0)
        mock_self, trv_state = _make_valve_self(last_pct=40)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        await trv_state.extra["_trvzb_valve_bump_task"]
        trv_state.last_valve_percent = 30

        await quirk.override_set_valve(mock_self, ENTITY, 20)
        await _settle(trv_state.extra.get("_trvzb_valve_bump_task"))

        assert writes == [50, 30, 40]

    @pytest.mark.asyncio
    async def test_an_opening_command_writes_directly(self, writes):
        """Opening needs no de-sticking, so the position goes out unchanged."""
        mock_self, trv_state = _make_valve_self(last_pct=40)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 60)

        assert handled is True
        assert writes == [60]
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_valve_maintenance_writes_directly(self, writes):
        """Maintenance drives the valve itself and takes no deferred steps."""
        mock_self, trv_state = _make_valve_self(last_pct=40, in_maintenance=True)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 0)

        assert handled is True
        assert writes == [0]
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_an_unknown_last_position_writes_directly(self, writes):
        """With no recorded position there is nothing to close further from."""
        mock_self, trv_state = _make_valve_self(last_pct=None)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 30)

        assert handled is True
        assert writes == [30]
        assert "_trvzb_valve_bump_task" not in trv_state.extra


def _registry_entry(entity_id, *, domain, translation_key=None, device_id="dev1"):
    """A registry entry stand-in with the fields the lookup reads."""
    entry = MagicMock()
    entry.entity_id = entity_id
    entry.domain = domain
    entry.device_id = device_id
    entry.unique_id = entity_id
    entry.translation_key = translation_key
    entry.original_name = None
    return entry


def _make_selector_self(
    state, options=("internal", "external"), *, entries=None, trv=None
):
    """A BT stand-in whose TRV device carries a sensor selector.

    ``state`` is what the selector currently reports; ``None`` stands for a
    selector that publishes no state. ``trv`` is the registry entry the
    lookup starts from, so a test can hand it one that belongs to no
    device; it is the first of ``entries`` unless given separately.
    """
    mock_self = _make_self()
    if trv is None:
        trv = _registry_entry("climate.trv1", domain="climate")
    selector = _registry_entry(
        "select.trv1_temperature_sensor_select",
        domain="select",
        translation_key="temperature_sensor_select",
    )
    registry = MagicMock()
    registry.async_get.return_value = trv
    registry.entities.values.return_value = (
        entries if entries is not None else [trv, selector]
    )
    mock_self._registry = registry

    selector_state = None
    if state is not None:
        selector_state = MagicMock()
        selector_state.state = state
        selector_state.attributes = {"options": list(options)}
    mock_self.hass.states.get.return_value = selector_state
    return mock_self


def _selector_calls(mock_self):
    """The select_option payloads the quirk dispatched."""
    return [
        call.args[2]
        for call in mock_self.hass.services.async_call.await_args_list
        if call.args[:2] == ("select", "select_option")
    ]


class TestMaybeSelectExternalSensor:
    """Which sensor the TRV regulates on while BT writes into its input.

    Writing the room temperature into the external input achieves nothing
    while the device regulates on its own sensor, and it lands there on its
    own: a TRVZB that is re-paired comes back on the internal sensor.
    """

    @pytest.mark.asyncio
    async def test_a_trv_on_its_internal_sensor_is_switched_over(self, monkeypatch):
        """The selector is moved onto the option BT writes for."""
        mock_self = _make_selector_self("internal")
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert await quirk.maybe_select_external_sensor(mock_self, "climate.trv1")

        assert _selector_calls(mock_self) == [
            {"entity_id": "select.trv1_temperature_sensor_select", "option": "external"}
        ]

    @pytest.mark.asyncio
    async def test_a_trv_already_on_an_external_option_is_left_alone(self, monkeypatch):
        """Which external option it is stays its owner's choice.

        Devices offer more than one option naming an external sensor, and
        rewriting the plain one would take that choice back on every write.
        """
        mock_self = _make_selector_self(
            "external_ignore_internal",
            options=("internal", "external", "external_ignore_internal"),
        )
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert await quirk.maybe_select_external_sensor(mock_self, "climate.trv1")

        assert _selector_calls(mock_self) == []

    @pytest.mark.asyncio
    async def test_a_selector_without_the_option_is_not_written(self, monkeypatch):
        """A device that names no external option keeps its selection."""
        mock_self = _make_selector_self("internal", options=("internal",))
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert (
            await quirk.maybe_select_external_sensor(mock_self, "climate.trv1") is False
        )

        assert _selector_calls(mock_self) == []

    @pytest.mark.asyncio
    async def test_a_trv_that_belongs_to_no_device_is_not_written(self, monkeypatch):
        """No device is no sibling, not "every entity without a device".

        A registry entry carrying no ``device_id`` would otherwise match
        every other entity that carries none, and the first one answering
        to the selector's translation key or id fragment would be written
        as if it sat on this TRV.
        """
        trv = _registry_entry("climate.trv1", domain="climate", device_id=None)
        stray = _registry_entry(
            "select.somewhere_else_temperature_sensor_select",
            domain="select",
            translation_key="temperature_sensor_select",
            device_id=None,
        )
        mock_self = _make_selector_self("internal", entries=[trv, stray], trv=trv)
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert (
            await quirk.maybe_select_external_sensor(mock_self, "climate.trv1") is False
        )

        assert _selector_calls(mock_self) == []

    @pytest.mark.parametrize("reported", ["unavailable", "unknown"])
    @pytest.mark.asyncio
    async def test_a_selector_that_is_not_reporting_is_not_written(
        self, monkeypatch, reported
    ):
        """A selector naming no option is in no state to be given one.

        The device behind an unavailable or unknown selector is out of
        reach, so the write would fail; the options the entity still lists
        are the ones it had when it was last reachable.
        """
        mock_self = _make_selector_self(reported)
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert (
            await quirk.maybe_select_external_sensor(mock_self, "climate.trv1") is False
        )

        assert _selector_calls(mock_self) == []

    @pytest.mark.asyncio
    async def test_the_translation_key_wins_over_an_earlier_id_match(self, monkeypatch):
        """The key names the selector; the id fragment only guesses at it.

        A device that carries a second select whose id reads like the
        selector — a leftover from a rename, say — hands it out first, and
        matching per entry would write to it and never reach the entry that
        names itself.
        """
        trv = _registry_entry("climate.trv1", domain="climate")
        decoy = _registry_entry(
            "select.trv1_temperature_sensor_select_old", domain="select"
        )
        selector = _registry_entry(
            "select.trv1_temperature_sensor_select",
            domain="select",
            translation_key="temperature_sensor_select",
        )
        mock_self = _make_selector_self("internal", entries=[trv, decoy, selector])
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert await quirk.maybe_select_external_sensor(mock_self, "climate.trv1")

        assert _selector_calls(mock_self) == [
            {"entity_id": "select.trv1_temperature_sensor_select", "option": "external"}
        ]

    @pytest.mark.asyncio
    async def test_a_device_without_a_selector_is_not_written(self, monkeypatch):
        """Nothing on the device answers for the sensor choice."""
        trv = _registry_entry("climate.trv1", domain="climate")
        mock_self = _make_selector_self("internal", entries=[trv])
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert (
            await quirk.maybe_select_external_sensor(mock_self, "climate.trv1") is False
        )

        assert _selector_calls(mock_self) == []


class TestExternalTemperatureWriteSelectsTheSensor:
    """The write and the sensor choice belong to the same intent."""

    @pytest.mark.asyncio
    async def test_writing_the_input_also_points_the_selector_at_it(self, monkeypatch):
        """A value written into an input the device ignores changes nothing.

        This pins the wiring rather than either half: the selector check
        has to happen on the path that writes the value, because that is
        the only path that knows a value was written.
        """
        trv = _registry_entry("climate.trv1", domain="climate")
        number = _registry_entry(
            "number.trv1_external_temperature_input",
            domain="number",
            translation_key="external_temperature_input",
        )
        selector = _registry_entry(
            "select.trv1_temperature_sensor_select",
            domain="select",
            translation_key="temperature_sensor_select",
        )
        mock_self = _make_selector_self("internal", entries=[trv, number, selector])
        mock_self.real_trvs = {
            "climate.trv1": Trv(entity_id="climate.trv1", model="TRVZB")
        }
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert await quirk.maybe_set_external_temperature(
            mock_self, "climate.trv1", 21.42
        )

        payloads = [
            call.args[2] for call in mock_self.hass.services.async_call.await_args_list
        ]
        assert payloads == [
            {"entity_id": "number.trv1_external_temperature_input", "value": 21.4},
            {
                "entity_id": "select.trv1_temperature_sensor_select",
                "option": "external",
            },
        ]
