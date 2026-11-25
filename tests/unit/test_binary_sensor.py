"""Tests for the binary sensors that report on the room's heat balance.

Two report a source of heat the radiator does not explain, four an anomaly
the anomaly region found.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from homeassistant.helpers.entity import Entity
import pytest

from custom_components.better_thermostat import (
    BetterThermostatData,
    binary_sensor as binary_sensor_platform,
)
from custom_components.better_thermostat.binary_sensor import (
    BetterThermostatHeatLossBinarySensor,
    BetterThermostatHeatSourceBinarySensor,
    BetterThermostatNoHeatDeliveryBinarySensor,
    BetterThermostatSuspectedOpenWindowBinarySensor,
    BetterThermostatSustainedHeatLossBinarySensor,
    BetterThermostatValveStuckOpenBinarySensor,
    async_setup_entry,
)
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.anomaly import (
    AnomalyState,
    FaultPhase,
    FaultSlot,
    TrvFaultState,
)
from custom_components.better_thermostat.entity import FollowsThermostat
from custom_components.better_thermostat.utils.anomaly_evidence import (
    AnomalyEvidenceTracker,
)
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION_MODE,
    CalibrationMode,
)
from tests.factories import ThermostatStandIn, make_state, trv_record

FOUND = FaultSlot(phase=FaultPhase.ACTIVE, active_since=100.0)


def _record(
    tracker: AnomalyEvidenceTracker,
    disturbance: float,
    *,
    now_monotonic: float = 1000.0,
    learning_held: bool = True,
) -> None:
    """Record one MPC v2 observer reading with the given disturbance."""
    tracker.record_mpc_v2_diagnostics(
        now_monotonic=now_monotonic,
        room_temperature_hat=20.0,
        radiator_temperature_hat=25.0,
        disturbance_kelvin_per_min=disturbance,
        disturbance_at_bound=False,
        normalised_innovation=0.0,
        room_time_constant_minutes=80.0,
        k_rad_room_per_min=0.005,
        k_loss_per_min=0.001,
        learning_held=learning_held,
        room_temperature=20.0,
        contact_open=False,
    )


def _climate(*modes: CalibrationMode | None) -> ThermostatStandIn:
    """Build a thermostat stand-in that carries the values the sensor reads.

    ``modes`` names the calibration mode of each TRV of the room.
    """
    bt = ThermostatStandIn()
    bt.unique_id = "bt_1"
    bt.real_trvs = {
        f"climate.trv_{index}": trv_record(
            entity_id=f"climate.trv_{index}", advanced={CONF_CALIBRATION_MODE: mode}
        )
        for index, mode in enumerate(modes)
    }
    bt.device_info = dict[str, str]()
    bt._available = True
    bt.clock = MagicMock(monotonic=lambda: 1000.0)
    return bt


class TestSourceDetection:
    """The tracker tells a heat gain and a heat loss from the observer."""

    def test_a_gain_beyond_the_threshold_raises_the_heat_source(self) -> None:
        """A disturbance above the threshold is a heat source."""
        tracker = AnomalyEvidenceTracker()
        _record(tracker, 0.004)
        assert tracker.heat_source_active is False

        _record(tracker, 0.006)
        assert tracker.heat_source_active is True
        assert tracker.cool_source_active is False

    def test_a_loss_beyond_the_threshold_raises_the_heat_loss(self) -> None:
        """A disturbance below the negative threshold is a heat loss."""
        tracker = AnomalyEvidenceTracker()
        _record(tracker, -0.006)
        assert tracker.cool_source_active is True
        assert tracker.heat_source_active is False

    def test_a_source_is_released_only_below_the_quieter_threshold(self) -> None:
        """The band between the thresholds holds the last state."""
        tracker = AnomalyEvidenceTracker()
        _record(tracker, 0.006)
        assert tracker.heat_source_active is True

        _record(tracker, 0.003)
        assert tracker.heat_source_active is True

        _record(tracker, 0.001)
        assert tracker.heat_source_active is False

    def test_the_baseline_is_the_room_usual_behaviour(self) -> None:
        """A gain that is part of the room's baseline is no longer a finding."""
        tracker = AnomalyEvidenceTracker()
        _record(tracker, 0.006, learning_held=False)
        baseline = tracker.disturbance_baseline_kelvin_per_min
        assert baseline > 0.0
        assert tracker.heat_source_active is True

        _record(tracker, baseline, learning_held=False, now_monotonic=1060.0)
        assert tracker.heat_source_active is False

    def test_a_reset_drops_the_sources(self) -> None:
        """A reset leaves no source behind."""
        tracker = AnomalyEvidenceTracker()
        _record(tracker, 0.006)
        assert tracker.heat_source_active is True

        tracker.reset()
        assert tracker.heat_source_active is False
        assert tracker.cool_source_active is False


class TestSourceBinarySensors:
    """The sensors report what the tracker finds."""

    def test_the_sensors_follow_the_tracker(self) -> None:
        """Each sensor shows the source of its own kind."""
        bt = _climate()
        heat_source = BetterThermostatHeatSourceBinarySensor(bt)
        heat_loss = BetterThermostatHeatLossBinarySensor(bt)

        assert heat_source.is_on is False
        assert heat_loss.is_on is False

        _record(bt.anomaly_evidence, 0.006)
        assert heat_source.is_on is True
        assert heat_loss.is_on is False

        _record(bt.anomaly_evidence, -0.006)
        assert heat_source.is_on is False
        assert heat_loss.is_on is True

    def test_the_sensors_are_unavailable_without_an_observer_reading(self) -> None:
        """Nothing measures the room, so nothing is known about it."""
        bt = _climate()
        sensor = BetterThermostatHeatSourceBinarySensor(bt)

        assert sensor.available is False

        _record(bt.anomaly_evidence, 0.0)
        assert sensor.available is True

    def test_a_stale_reading_leaves_the_sensor_unavailable(self) -> None:
        """A reading that is too old no longer stands for the room."""
        bt = _climate()
        sensor = BetterThermostatHeatSourceBinarySensor(bt)

        _record(bt.anomaly_evidence, 0.0, now_monotonic=1000.0)
        assert sensor.available is True

        bt.clock = MagicMock(monotonic=lambda: 1000.0 + 3600.0)
        assert sensor.available is False

    @pytest.mark.parametrize(
        ("entity", "suffix"),
        [
            (BetterThermostatHeatSourceBinarySensor, "heat_source_detected"),
            (BetterThermostatHeatLossBinarySensor, "heat_loss_detected"),
        ],
    )
    def test_each_sensor_carries_its_own_unique_id(self, entity, suffix) -> None:
        """The unique id follows the entity it belongs to."""
        assert entity(_climate()).unique_id == f"bt_1_{suffix}"


def _watched(anomaly: AnomalyState | None = None) -> ThermostatStandIn:
    """Build a thermostat stand-in whose anomaly detection is on."""
    bt = _climate()
    bt.kernel_state = make_state(
        anomaly_detection=True, anomaly=anomaly or AnomalyState()
    )
    return bt


class TestAnomalyBinarySensors:
    """Each anomaly the region finds is a problem the user can see and act on."""

    @pytest.mark.parametrize(
        ("entity", "anomaly"),
        [
            (
                BetterThermostatSuspectedOpenWindowBinarySensor,
                AnomalyState(rapid_loss=FOUND),
            ),
            (
                BetterThermostatSustainedHeatLossBinarySensor,
                AnomalyState(parasitic_loss=FOUND),
            ),
            (
                BetterThermostatNoHeatDeliveryBinarySensor,
                AnomalyState(no_heat_delivery=FOUND),
            ),
            (
                BetterThermostatNoHeatDeliveryBinarySensor,
                AnomalyState(
                    trvs={"climate.trv_0": TrvFaultState(no_heat_delivery=FOUND)}
                ),
            ),
            (
                BetterThermostatValveStuckOpenBinarySensor,
                AnomalyState(
                    trvs={"climate.trv_0": TrvFaultState(valve_stuck_open=FOUND)}
                ),
            ),
        ],
    )
    def test_a_sensor_is_on_while_its_anomaly_is_reported(
        self, entity, anomaly
    ) -> None:
        """The sensor says problem for exactly the anomaly it stands for."""
        assert entity(_watched()).is_on is False
        assert entity(_watched(anomaly)).is_on is True

    def test_another_anomaly_leaves_the_sensor_off(self) -> None:
        """A suspected window is no sustained heat loss."""
        bt = _watched(AnomalyState(rapid_loss=FOUND))
        assert BetterThermostatSustainedHeatLossBinarySensor(bt).is_on is False

    @pytest.mark.parametrize(
        "entity",
        [
            BetterThermostatSuspectedOpenWindowBinarySensor,
            BetterThermostatSustainedHeatLossBinarySensor,
            BetterThermostatNoHeatDeliveryBinarySensor,
            BetterThermostatValveStuckOpenBinarySensor,
        ],
    )
    def test_a_room_nobody_watches_is_not_shown_as_free_of_problems(
        self, entity
    ) -> None:
        """While anomaly detection is off the sensor is unavailable, not off."""
        bt = _climate()
        bt.kernel_state = make_state(anomaly_detection=False)
        assert entity(bt).available is False

        watched = _watched()
        assert entity(watched).available is True
        watched._available = False
        assert entity(watched).available is False

    @pytest.mark.parametrize(
        ("entity", "anomaly_in"),
        [
            (
                BetterThermostatSuspectedOpenWindowBinarySensor,
                lambda slot: AnomalyState(rapid_loss=slot),
            ),
            (
                BetterThermostatSustainedHeatLossBinarySensor,
                lambda slot: AnomalyState(parasitic_loss=slot),
            ),
        ],
        ids=["suspected_open_window", "sustained_heat_loss"],
    )
    @pytest.mark.parametrize("phase", list(FaultPhase))
    def test_a_room_wide_sensor_names_the_phase_of_its_detector(
        self, entity, anomaly_in, phase
    ) -> None:
        """The user sees whether a finding is suspected, recovering or expired."""
        slot = FaultSlot(phase=phase, since=50.0, active_since=10.0)
        bt = _watched(anomaly_in(slot))
        assert entity(bt).extra_state_attributes == {
            "phase": phase.value,
            "since": None,
        }

    def test_a_trv_sensor_names_the_trvs_it_is_about(self) -> None:
        """The user is told which radiator thermostat to look at."""
        bt = _watched(
            AnomalyState(
                trvs={
                    "climate.trv_0": TrvFaultState(valve_stuck_open=FOUND),
                    "climate.trv_1": TrvFaultState(no_heat_delivery=FOUND),
                }
            )
        )
        stuck = BetterThermostatValveStuckOpenBinarySensor(bt)
        cold = BetterThermostatNoHeatDeliveryBinarySensor(bt)
        assert stuck.extra_state_attributes == {
            "trvs": ["climate.trv_0"],
            "since": None,
        }
        assert cold.extra_state_attributes == {"trvs": ["climate.trv_1"], "since": None}

    def test_a_supply_failure_names_no_single_thermostat(self) -> None:
        """A room no radiator warms is the supply's problem, not one TRV's."""
        bt = _watched(AnomalyState(no_heat_delivery=FOUND))
        sensor = BetterThermostatNoHeatDeliveryBinarySensor(bt)
        assert sensor.is_on is True
        assert sensor.extra_state_attributes == {"trvs": [], "since": None}

    @pytest.mark.parametrize(
        ("entity", "anomaly"),
        [
            (
                BetterThermostatSuspectedOpenWindowBinarySensor,
                AnomalyState(rapid_loss=FOUND),
            ),
            (
                BetterThermostatValveStuckOpenBinarySensor,
                AnomalyState(
                    trvs={"climate.trv_0": TrvFaultState(valve_stuck_open=FOUND)}
                ),
            ),
        ],
    )
    def test_a_sensor_names_the_moment_its_anomaly_was_first_reported(
        self, entity, anomaly
    ) -> None:
        """The user sees since when the problem is there.

        The moment stays while the anomaly is reported, and is gone with it.
        """
        bt = _watched(anomaly)
        bt.clock = FakeClock(
            monotonic_value=1000.0, now_value=datetime(2026, 1, 15, 8, 20, tzinfo=UTC)
        )
        sensor = entity(bt)

        with patch.object(Entity, "_async_write_ha_state"):
            sensor._async_write_ha_state()
            bt.clock.advance(600.0)
            sensor._async_write_ha_state()
            reported_since = sensor.extra_state_attributes["since"]

            bt.kernel_state = make_state(anomaly_detection=True)
            sensor._async_write_ha_state()

        assert reported_since == "2026-01-15T08:20:00+00:00"
        assert sensor.extra_state_attributes["since"] is None


async def _set_up(bt: ThermostatStandIn) -> tuple[list[object], list[str | None]]:
    """Set the platform up for ``bt``; return the added sensors and the claimed ids."""
    entry = MagicMock()
    entry.runtime_data = BetterThermostatData(climate=bt)
    add_entities = MagicMock()

    with (
        patch(f"{binary_sensor_platform.__name__}.er.async_get"),
        patch(
            f"{binary_sensor_platform.__name__}.remove_unclaimed_registry_entries"
        ) as remove_unclaimed,
        patch(f"{binary_sensor_platform.__name__}.async_normalize_bt_entity_ids"),
    ):
        await async_setup_entry(MagicMock(), entry, add_entities)

    (sensors,) = add_entities.call_args.args[:1]
    return list(sensors), list(remove_unclaimed.call_args.args[3])


class TestPlatformSetup:
    """The platform builds the sensors that have something to report on the room."""

    @pytest.mark.asyncio
    async def test_a_thermostat_without_an_entity_gets_no_sensors(self) -> None:
        """Without a thermostat there is nothing to report on."""
        entry = MagicMock()
        entry.runtime_data = BetterThermostatData(climate=None)
        add_entities = MagicMock()

        await async_setup_entry(MagicMock(), entry, add_entities)
        add_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_room_mpc_v2_controls_gets_every_sensor(self) -> None:
        """The observer adds the sensors that only it can feed."""
        sensors, claimed = await _set_up(_climate(CalibrationMode.MPC_V2_CALIBRATION))

        assert [type(sensor) for sensor in sensors] == [
            BetterThermostatSuspectedOpenWindowBinarySensor,
            BetterThermostatNoHeatDeliveryBinarySensor,
            BetterThermostatValveStuckOpenBinarySensor,
            BetterThermostatSustainedHeatLossBinarySensor,
            BetterThermostatHeatSourceBinarySensor,
            BetterThermostatHeatLossBinarySensor,
        ]
        assert claimed == [
            "bt_1_suspected_open_window",
            "bt_1_no_heat_delivery",
            "bt_1_valve_stuck_open",
            "bt_1_sustained_heat_loss",
            "bt_1_heat_source_detected",
            "bt_1_heat_loss_detected",
        ]

    @pytest.mark.asyncio
    async def test_one_trv_on_mpc_v2_is_enough(self) -> None:
        """The observer runs for the room as soon as one of its TRVs uses it."""
        sensors, _ = await _set_up(
            _climate(
                CalibrationMode.PID_CALIBRATION, CalibrationMode.MPC_V2_CALIBRATION
            )
        )

        assert len(sensors) == 6

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "modes",
        [
            (),
            (CalibrationMode.MPC_CALIBRATION,),
            (CalibrationMode.PID_CALIBRATION, CalibrationMode.DEFAULT),
        ],
    )
    async def test_a_room_without_mpc_v2_gets_no_observer_sensors(self, modes) -> None:
        """A room no observer measures gets the anomalies every room can have.

        A sensor only the observer can feed would never report there. Its
        registry entry is not claimed either, so the one a room left behind
        when it stopped using MPC v2 is removed.
        """
        sensors, claimed = await _set_up(_climate(*modes))

        assert [type(sensor) for sensor in sensors] == [
            BetterThermostatSuspectedOpenWindowBinarySensor,
            BetterThermostatNoHeatDeliveryBinarySensor,
            BetterThermostatValveStuckOpenBinarySensor,
        ]
        assert claimed == [
            "bt_1_suspected_open_window",
            "bt_1_no_heat_delivery",
            "bt_1_valve_stuck_open",
        ]

    @pytest.mark.asyncio
    async def test_a_sensor_follows_the_thermostat(self) -> None:
        """The sensor is published again whenever the thermostat reports."""
        sensor = BetterThermostatHeatSourceBinarySensor(_climate())

        with patch.object(FollowsThermostat, "_follow_thermostat") as follow:
            await sensor.async_added_to_hass()

        follow.assert_called_once()

    def test_the_sensor_carries_the_device_of_the_thermostat(self) -> None:
        """The sensor sits on the device of the thermostat it reports on."""
        bt = _climate()
        bt.device_info = {"name": "Living room"}

        sensor = BetterThermostatHeatSourceBinarySensor(bt)
        assert sensor.device_info == {"name": "Living room"}

    def test_an_unavailable_thermostat_leaves_the_sensor_unavailable(self) -> None:
        """Nothing the thermostat reports is worth showing then."""
        bt = _climate()
        bt._available = False
        _record(bt.anomaly_evidence, 0.006)

        assert BetterThermostatHeatSourceBinarySensor(bt).available is False
