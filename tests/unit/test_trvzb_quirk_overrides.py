"""Tests for the TRVZB setpoint, HVAC mode and valve override quirks."""

import asyncio
import contextlib
import importlib
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

quirk = importlib.import_module("custom_components.better_thermostat.model_fixes.TRVZB")


def _make_self():
    """Create a mock BetterThermostat with a spied service-call layer."""
    mock_self = ThermostatStandIn()
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


def _make_valve_self(last_percent=40, *, in_maintenance=False):
    """Create a mock BetterThermostat whose TRV records a commanded valve percent."""
    mock_self = _make_self()
    mock_self.in_maintenance = in_maintenance
    mock_self.is_removed = False
    trv_state = Trv(entity_id=ENTITY)
    trv_state.last_valve_percent = last_percent
    mock_self.real_trvs = {ENTITY: trv_state}
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
        mock_self, trv_state = _make_valve_self(last_percent=40)

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
        mock_self, trv_state = _make_valve_self(last_percent=40)
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
        mock_self, trv_state = _make_valve_self(last_percent=40)

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
        mock_self, trv_state = _make_valve_self(last_percent=40)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        await trv_state.extra["_trvzb_valve_bump_task"]
        trv_state.last_valve_percent = 30

        await quirk.override_set_valve(mock_self, ENTITY, 20)
        await _settle(trv_state.extra.get("_trvzb_valve_bump_task"))

        assert writes == [50, 30, 40]

    @pytest.mark.asyncio
    async def test_an_opening_command_writes_directly(self, writes):
        """Opening needs no de-sticking, so the position goes out unchanged."""
        mock_self, trv_state = _make_valve_self(last_percent=40)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 60)

        assert handled is True
        assert writes == [60]
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_valve_maintenance_writes_directly(self, writes):
        """Maintenance drives the valve itself and takes no deferred steps."""
        mock_self, trv_state = _make_valve_self(last_percent=40, in_maintenance=True)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 0)

        assert handled is True
        assert writes == [0]
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_valve_maintenance_drops_a_deferred_close(self, writes, monkeypatch):
        """A close still due from before maintenance stays off the valve.

        Maintenance opens the valve fully and holds it there; the target a
        bump deferred just before would land in that hold and close it.
        """
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.01)
        mock_self, trv_state = _make_valve_self(last_percent=60)
        await quirk.override_set_valve(mock_self, ENTITY, 20)
        deferred = trv_state.extra["_trvzb_valve_bump_task"]

        mock_self.in_maintenance = True
        await quirk.override_set_valve(mock_self, ENTITY, 100)
        await asyncio.wait([deferred])
        await asyncio.sleep(0.02)

        assert writes == [70, 100]
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_an_unknown_last_position_writes_directly(self, writes):
        """With no recorded position there is nothing to close further from."""
        mock_self, trv_state = _make_valve_self(last_percent=None)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 30)

        assert handled is True
        assert writes == [30]
        assert "_trvzb_valve_bump_task" not in trv_state.extra


def _registry_entry(entity_id, *, translation_key=None, device_id="dev1"):
    """A registry entry on the TRV's device; its domain is the entity id's."""
    return make_registry_entry(
        entity_id, translation_key=translation_key, device_id=device_id
    )


# The selector options Zigbee2MQTT publishes for a TRVZB from 2.14.2 on.
Z2M_2_14_2_OPTIONS = (
    "local_temperature",
    "remote_temperature",
    "remote_source_offline",
)


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
        trv = _registry_entry("climate.trv1")
    selector = _registry_entry(
        "select.trv1_temperature_sensor_select",
        translation_key="temperature_sensor_select",
    )
    mock_self._registry = make_entity_registry(
        *(entries if entries is not None else [trv, selector])
    )

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
    async def test_a_trv_on_its_local_sensor_is_switched_to_the_remote_one(
        self, monkeypatch
    ):
        """Zigbee2MQTT 2.14.2 names the options after the temperature source."""
        mock_self = _make_selector_self("local_temperature", options=Z2M_2_14_2_OPTIONS)
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert await quirk.maybe_select_external_sensor(mock_self, "climate.trv1")

        assert _selector_calls(mock_self) == [
            {
                "entity_id": "select.trv1_temperature_sensor_select",
                "option": "remote_temperature",
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "selected", ["remote_temperature", "remote_source_offline"]
    )
    async def test_a_trv_on_the_remote_source_is_left_alone(
        self, monkeypatch, selected
    ):
        """A device that fell back while the remote value is gone returns by itself."""
        mock_self = _make_selector_self(selected, options=Z2M_2_14_2_OPTIONS)
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
        trv = _registry_entry("climate.trv1", device_id=None)
        stray = _registry_entry(
            "select.somewhere_else_temperature_sensor_select",
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

        A device can carry a second select whose id reads like the
        selector, a leftover from a rename. The registry hands that one
        out first, so matching per entry would write to it and never
        reach the entry that names itself.
        """
        trv = _registry_entry("climate.trv1")
        decoy = _registry_entry("select.trv1_temperature_sensor_select_old")
        selector = _registry_entry(
            "select.trv1_temperature_sensor_select",
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
    async def test_a_selector_that_names_itself_nothing_is_found_by_its_id(
        self, monkeypatch
    ):
        """Not every integration publishes a translation key.

        The id is the only handle left on such an entry, so it stays the
        fallback for the siblings that carry no key.
        """
        trv = _registry_entry("climate.trv1")
        unnamed = _registry_entry("select.trv1_temperature_sensor_select")
        mock_self = _make_selector_self("internal", entries=[trv, unnamed])
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert await quirk.maybe_select_external_sensor(mock_self, "climate.trv1")

        assert _selector_calls(mock_self) == [
            {"entity_id": "select.trv1_temperature_sensor_select", "option": "external"}
        ]

    @pytest.mark.asyncio
    async def test_a_sibling_that_names_something_else_is_left_alone(self, monkeypatch):
        """An entry with a key of its own has said what it is.

        Guessing at its id would write the sensor choice into whatever
        else the device exposes, so the fallback passes it by.
        """
        trv = _registry_entry("climate.trv1")
        named_otherwise = _registry_entry(
            "select.trv1_temperature_sensor_select",
            translation_key="valve_opening_degree",
        )
        unrelated = _registry_entry("select.trv1_backlight")
        mock_self = _make_selector_self(
            "internal", entries=[trv, named_otherwise, unrelated]
        )
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )

        assert (
            await quirk.maybe_select_external_sensor(mock_self, "climate.trv1") is False
        )

        assert _selector_calls(mock_self) == []

    @pytest.mark.asyncio
    async def test_a_device_without_a_selector_is_not_written(self, monkeypatch):
        """Nothing on the device answers for the sensor choice."""
        trv = _registry_entry("climate.trv1")
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
        trv = _registry_entry("climate.trv1")
        number = _registry_entry(
            "number.trv1_external_temperature_input",
            translation_key="external_temperature_input",
        )
        selector = _registry_entry(
            "select.trv1_temperature_sensor_select",
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


VALVE_OPENING = "number.trv1_valve_opening_degree"
VALVE_CLOSING = "number.trv1_valve_closing_degree"
VALVE_GENERIC = "number.trv1_valve_position"

WRITE_REFUSALS = [
    HomeAssistantError("device did not answer"),
    ServiceValidationError("value is out of range"),
    OSError("connection reset"),
]
WRITE_REFUSAL_IDS = ["unreachable", "out_of_range", "transport"]


def _valve_number(entity_id, translation_key=None):
    """A number entity on the TRV's device that the valve write can pick up."""
    return _registry_entry(entity_id, translation_key=translation_key)


class TestValveWriteTheDeviceRefuses:
    """A refused valve write is reported, not raised.

    Batteries sleep, devices go out of reach, an integration reloads its
    config entry, and a number entity may declare a narrower range than the
    percentage handed to it. All of these answer a blocking service call with
    a ``HomeAssistantError``. Raising it strands the caller in two ways: the
    complement of a written opening degree never goes out, leaving the valve
    on a range nobody asked for, and the deferred half of the de-sticking bump
    runs in a background task where nothing is left to catch it.
    """

    @staticmethod
    def _trvzb_carrying(monkeypatch, numbers):
        """A TRVZB whose device carries the given number entities."""
        trv = _registry_entry(ENTITY)
        registry = make_entity_registry(trv, *numbers)
        monkeypatch.setattr(quirk.er, "async_get", lambda hass: registry, raising=True)

        mock_self = _make_self()
        mock_self.in_maintenance = False
        mock_self.is_removed = False
        mock_self.real_trvs = {ENTITY: Trv(entity_id=ENTITY, model="TRVZB")}
        return mock_self

    @pytest.mark.parametrize(
        "refused_entity", [VALVE_OPENING, VALVE_CLOSING], ids=["opening", "closing"]
    )
    @pytest.mark.parametrize("refusal", WRITE_REFUSALS, ids=WRITE_REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_the_refusal_is_reported_as_a_declined_write(
        self, monkeypatch, refused_entity, refusal
    ):
        """Either degree may be refused; both yield False."""
        mock_self = self._trvzb_carrying(
            monkeypatch,
            [
                _valve_number(VALVE_OPENING, "valve_opening_degree"),
                _valve_number(VALVE_CLOSING, "valve_closing_degree"),
            ],
        )

        async def _refuse(_domain, _service, data, **_kwargs):
            if data["entity_id"] == refused_entity:
                raise refusal

        mock_self.hass.services.async_call = AsyncMock(side_effect=_refuse)

        assert (
            await quirk.maybe_set_sonoff_valve_percent(mock_self, ENTITY, 30) is False
        )

    @pytest.mark.parametrize("refusal", WRITE_REFUSALS, ids=WRITE_REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_fallback_write_is_reported_too(self, monkeypatch, refusal):
        """A device naming neither degree is written through the same handler."""
        mock_self = self._trvzb_carrying(monkeypatch, [_valve_number(VALVE_GENERIC)])
        mock_self.hass.services.async_call = AsyncMock(side_effect=refusal)

        assert (
            await quirk.maybe_set_sonoff_valve_percent(mock_self, ENTITY, 30) is False
        )

    @pytest.mark.parametrize("refusal", WRITE_REFUSALS, ids=WRITE_REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_deferred_write_does_not_strand_its_task(
        self, monkeypatch, refusal
    ):
        """The write carrying the requested position has no caller left.

        The de-sticking bump has already driven the valve further open by the
        time it goes out, and the commanded position is recorded as taken. An
        error escaping the background task leaves the valve on the bump.
        """
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.0)
        mock_self = self._trvzb_carrying(
            monkeypatch, [_valve_number(VALVE_OPENING, "valve_opening_degree")]
        )
        mock_self.real_trvs[ENTITY].last_valve_percent = 40

        async def _refuse(_domain, _service, data, **_kwargs):
            if data["value"] == 30:
                raise refusal

        mock_self.hass.services.async_call = AsyncMock(side_effect=_refuse)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        task = mock_self.real_trvs[ENTITY].extra["_trvzb_valve_bump_task"]
        await asyncio.wait([task])

        assert task.exception() is None


class TestExternalTemperatureWriteTheDeviceRefuses:
    """A refused write is reported, not raised.

    Batteries sleep, devices go out of reach, an integration reloads its
    config entry, and a device may declare a narrower range than the clamp
    the quirk applies. All of these answer a blocking service call with a
    ``HomeAssistantError``. Raising it here would abandon the caller's own
    work on the room temperature it just accepted, so the quirk reports the
    write as declined instead.
    """

    @staticmethod
    def _device_on_the_internal_sensor(monkeypatch):
        """A TRVZB whose device carries both the input and the selector."""
        trv = _registry_entry("climate.trv1")
        number = _registry_entry(
            "number.trv1_external_temperature_input",
            translation_key="external_temperature_input",
        )
        selector = _registry_entry(
            "select.trv1_temperature_sensor_select",
            translation_key="temperature_sensor_select",
        )
        mock_self = _make_selector_self("internal", entries=[trv, number, selector])
        mock_self.real_trvs = {
            "climate.trv1": Trv(entity_id="climate.trv1", model="TRVZB")
        }
        monkeypatch.setattr(
            quirk.er, "async_get", lambda hass: mock_self._registry, raising=True
        )
        return mock_self

    @pytest.mark.parametrize("failing_domain", ["number", "select"])
    @pytest.mark.parametrize(
        "refusal",
        [
            HomeAssistantError("device did not answer"),
            ServiceValidationError("value is out of range"),
            OSError("connection reset"),
        ],
        ids=["unreachable", "out_of_range", "transport"],
    )
    @pytest.mark.asyncio
    async def test_the_refusal_is_reported_as_a_declined_write(
        self, monkeypatch, failing_domain, refusal
    ):
        """Either half of the write may be refused; both yield False."""
        mock_self = self._device_on_the_internal_sensor(monkeypatch)

        async def _refuse(domain, *args, **kwargs):
            if domain == failing_domain:
                raise refusal

        mock_self.hass.services.async_call = AsyncMock(side_effect=_refuse)

        assert (
            await quirk.maybe_set_external_temperature(mock_self, "climate.trv1", 21.42)
            is False
        )


def _trvzb_with_numbers(monkeypatch, numbers, *, model="TRVZB"):
    """A TRVZB whose device carries ``numbers`` and accepts every write."""
    registry = make_entity_registry(_registry_entry(ENTITY), *numbers)
    monkeypatch.setattr(quirk.er, "async_get", lambda hass: registry, raising=True)
    mock_self = _make_self()
    mock_self.in_maintenance = False
    mock_self.real_trvs = {ENTITY: Trv(entity_id=ENTITY, model=model)}
    return mock_self


def _number_writes(mock_self):
    """The number.set_value payloads the quirk dispatched, in order."""
    return [
        call.args[2]
        for call in mock_self.hass.services.async_call.await_args_list
        if call.args[:2] == ("number", "set_value")
    ]


class TestValveNumbersWithoutATranslationKey:
    """A valve number that carries no key is found by the names it does carry."""

    @pytest.mark.parametrize(
        ("opening", "closing"),
        [
            (_registry_entry(VALVE_OPENING), _registry_entry(VALVE_CLOSING)),
            (
                make_registry_entry(
                    "number.radiator_a",
                    unique_id="0x1234_valve_opening_degree",
                    device_id="dev1",
                ),
                make_registry_entry(
                    "number.radiator_b",
                    unique_id="0x1234_valve_closing_degree",
                    device_id="dev1",
                ),
            ),
            (
                make_registry_entry(
                    "number.radiator_a",
                    original_name="Valve opening degree",
                    device_id="dev1",
                ),
                make_registry_entry(
                    "number.radiator_b",
                    original_name="Valve closing degree",
                    device_id="dev1",
                ),
            ),
        ],
        ids=["entity_id", "unique_id", "original_name"],
    )
    @pytest.mark.asyncio
    async def test_the_opening_and_its_complement_are_written(
        self, monkeypatch, opening, closing
    ):
        """The opening degree takes the position, the closing degree the rest."""
        mock_self = _trvzb_with_numbers(monkeypatch, [opening, closing])

        assert await quirk.maybe_set_sonoff_valve_percent(mock_self, ENTITY, 30)

        assert _number_writes(mock_self) == [
            {"entity_id": opening.entity_id, "value": 30},
            {"entity_id": closing.entity_id, "value": 70},
        ]

    @pytest.mark.asyncio
    async def test_a_generic_valve_number_is_preferred_over_a_position(
        self, monkeypatch
    ):
        """Without either degree, a number naming the valve takes the position."""
        mock_self = _trvzb_with_numbers(
            monkeypatch,
            [
                _valve_number("number.trv1_motor_degree"),
                _valve_number("number.trv1_position_limit"),
                _valve_number("number.trv1_valve_target"),
            ],
        )

        assert await quirk.maybe_set_sonoff_valve_percent(mock_self, ENTITY, 30)

        assert _number_writes(mock_self) == [
            {"entity_id": "number.trv1_valve_target", "value": 30}
        ]

    @pytest.mark.asyncio
    async def test_a_position_number_is_preferred_over_any_other_match(
        self, monkeypatch
    ):
        """Among generic matches that do not name the valve, position comes first."""
        mock_self = _trvzb_with_numbers(
            monkeypatch,
            [
                _valve_number("number.trv1_motor_degree"),
                _valve_number("number.trv1_position_limit"),
            ],
        )

        assert await quirk.maybe_set_sonoff_valve_percent(mock_self, ENTITY, 30)

        assert _number_writes(mock_self) == [
            {"entity_id": "number.trv1_position_limit", "value": 30}
        ]

    @pytest.mark.asyncio
    async def test_a_number_keyed_as_something_else_is_no_valve(self, monkeypatch):
        """The external temperature input is a number, but no valve channel."""
        mock_self = _trvzb_with_numbers(
            monkeypatch,
            [
                _registry_entry(
                    "number.trv1_external_temperature_input",
                    translation_key="external_temperature_input",
                )
            ],
        )

        assert quirk.has_valve_channel(mock_self, ENTITY) is False
        assert (
            await quirk.maybe_set_sonoff_valve_percent(mock_self, ENTITY, 30) is False
        )
        assert _number_writes(mock_self) == []


class TestOverrideSetValveEdges:
    """The valve override around its de-sticking bump."""

    @pytest.mark.asyncio
    async def test_a_trv_bt_does_not_hold_is_declined(self, writes):
        """Without the TRV's record there is no position to start from."""
        mock_self, _ = _make_valve_self(last_percent=40)
        mock_self.real_trvs = {}

        assert await quirk.override_set_valve(mock_self, ENTITY, 30) is False
        assert writes == []

    @pytest.mark.asyncio
    async def test_a_position_that_is_no_number_is_declined(self, writes):
        """The adapter's own valve channel is left to handle it."""
        mock_self, _ = _make_valve_self(last_percent=40)

        assert await quirk.override_set_valve(mock_self, ENTITY, "half") is False
        assert writes == []

    @pytest.mark.asyncio
    async def test_a_refused_bump_still_writes_the_target(self, monkeypatch):
        """A device that did not take the bump is handed the target directly."""
        recorded = []

        async def _write(_self, _entity_id, percent):
            recorded.append(percent)
            return percent != 50

        monkeypatch.setattr(quirk, "maybe_set_sonoff_valve_percent", _write)
        mock_self, trv_state = _make_valve_self(last_percent=40)

        assert await quirk.override_set_valve(mock_self, ENTITY, 30) is True

        assert recorded == [50, 30]
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_a_bump_the_loop_can_no_longer_cancel_is_no_pending_write(
        self, writes
    ):
        """A reference left behind by a closed loop does not stand in for a bump.

        Cancelling a future whose loop is closed raises; the close that
        follows is then treated as the first one and bumps again.
        """
        stale_loop = asyncio.new_event_loop()
        stale = stale_loop.create_future()
        stale.add_done_callback(lambda _future: None)
        stale_loop.close()
        mock_self, trv_state = _make_valve_self(last_percent=40)
        trv_state.extra["_trvzb_valve_bump_task"] = stale

        handled = await quirk.override_set_valve(mock_self, ENTITY, 30)
        task = trv_state.extra.get("_trvzb_valve_bump_task")

        try:
            assert handled is True
            assert writes == [50]
            assert task is not None and task is not stale
        finally:
            await _settle(task)


class TestTheDeferredValveWrite:
    """The target a bump deferred lands only while it is still the newest."""

    @pytest.mark.asyncio
    async def test_it_lands_after_the_delay(self, writes, monkeypatch):
        """The baseline: the deferred target follows the bump."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.0)
        mock_self, trv_state = _make_valve_self(last_percent=40)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        await trv_state.extra["_trvzb_valve_bump_task"]

        assert writes == [50, 30]

    @pytest.mark.asyncio
    async def test_a_trv_removed_meanwhile_is_not_written(self, writes, monkeypatch):
        """A TRV that left the thermostat during the delay takes no write."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.0)
        mock_self, trv_state = _make_valve_self(last_percent=40)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        mock_self.real_trvs = {}
        await trv_state.extra["_trvzb_valve_bump_task"]

        assert writes == [50]

    @pytest.mark.asyncio
    async def test_a_thermostat_removed_meanwhile_does_not_write(
        self, writes, monkeypatch
    ):
        """A thermostat removed during the delay leaves the valve alone."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.0)
        mock_self, trv_state = _make_valve_self(last_percent=40)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        mock_self.is_removed = True
        await trv_state.extra["_trvzb_valve_bump_task"]

        assert writes == [50]

    @pytest.mark.asyncio
    async def test_the_removal_cancels_the_waiting_write(self, writes, monkeypatch):
        """The deferred write is the thermostat's own work and ends with it."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.01)
        mock_self, trv_state = _make_valve_self(last_percent=40)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        task = trv_state.extra["_trvzb_valve_bump_task"]
        cancelled = mock_self.task_manager.cancel_all()
        await asyncio.wait(cancelled)
        await asyncio.sleep(0.02)

        assert task in cancelled
        assert writes == [50]

    @pytest.mark.asyncio
    async def test_a_removed_thermostat_closes_without_deferring(self, writes):
        """Once the removal has closed the task owner, the close goes out at once.

        The bump has already opened the valve; with no deferred write to
        follow it, leaving the valve there would hold it further open than
        asked for.
        """
        mock_self, trv_state = _make_valve_self(last_percent=40)
        mock_self.task_manager.cancel_all()

        assert await quirk.override_set_valve(mock_self, ENTITY, 30) is True

        assert writes == [50, 30]
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_a_superseded_write_is_dropped(self, writes, monkeypatch):
        """A newer bump sequence owns the valve; the older target stays off it."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.0)
        mock_self, trv_state = _make_valve_self(last_percent=40)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        trv_state.extra["_trvzb_valve_bump_seq"] += 1
        await trv_state.extra["_trvzb_valve_bump_task"]

        assert writes == [50]

    @pytest.mark.asyncio
    async def test_a_write_cancelled_during_the_delay_never_lands(
        self, writes, monkeypatch
    ):
        """Cancelling the waiting task drops the target it carried."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.01)
        mock_self, trv_state = _make_valve_self(last_percent=40)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        task = trv_state.extra["_trvzb_valve_bump_task"]
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait([task])
        await asyncio.sleep(0.02)

        assert writes == [50]

    @pytest.mark.asyncio
    async def test_a_failing_write_is_logged_not_raised(self, monkeypatch, caplog):
        """Nothing awaits the background task, so its error goes to the log."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.0)

        async def _write(_self, _entity_id, percent):
            if percent == 30:
                raise RuntimeError("integration is shutting down")
            return True

        monkeypatch.setattr(quirk, "maybe_set_sonoff_valve_percent", _write)
        mock_self, trv_state = _make_valve_self(last_percent=40)

        with caplog.at_level("DEBUG", logger=quirk.__name__):
            await quirk.override_set_valve(mock_self, ENTITY, 30)
            task = trv_state.extra["_trvzb_valve_bump_task"]
            await asyncio.wait([task])

        assert task.exception() is None
        assert "TRVZB delayed valve set exception" in caplog.text
        assert "integration is shutting down" in caplog.text


class TestExternalTemperatureIsOnlyWrittenWhereItCanLand:
    """The input is written for a Sonoff device and a numeric reading only."""

    @staticmethod
    def _device_with_input(monkeypatch, model):
        number = _registry_entry(
            "number.trv1_external_temperature_input",
            translation_key="external_temperature_input",
        )
        return _trvzb_with_numbers(monkeypatch, [number], model=model)

    @pytest.mark.parametrize("model", ["TS0601", "", None])
    @pytest.mark.asyncio
    async def test_a_device_of_another_model_is_not_written(self, monkeypatch, model):
        """An input of the same name on another device means something else."""
        mock_self = self._device_with_input(monkeypatch, model)

        assert (
            await quirk.maybe_set_external_temperature(mock_self, ENTITY, 21.0) is False
        )
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.parametrize("reading", [None, "unknown"])
    @pytest.mark.asyncio
    async def test_a_reading_that_is_no_number_is_not_written(
        self, monkeypatch, reading
    ):
        """Without a room temperature the input keeps its last value."""
        mock_self = self._device_with_input(monkeypatch, "TRVZB")

        assert (
            await quirk.maybe_set_external_temperature(mock_self, ENTITY, reading)
            is False
        )
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.parametrize(("reading", "written"), [(-3.0, 0.0), (120.0, 99.9)])
    @pytest.mark.asyncio
    async def test_the_reading_is_clamped_to_the_input_range(
        self, monkeypatch, reading, written
    ):
        """The input takes 0 to 99.9 degrees."""
        mock_self = self._device_with_input(monkeypatch, "TRVZB")

        assert await quirk.maybe_set_external_temperature(mock_self, ENTITY, reading)

        assert _number_writes(mock_self) == [
            {"entity_id": "number.trv1_external_temperature_input", "value": written}
        ]
