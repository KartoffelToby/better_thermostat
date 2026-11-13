"""Tests for the binary sensors that report a source of heat in the room."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat import (
    BetterThermostatData,
    binary_sensor as binary_sensor_platform,
)
from custom_components.better_thermostat.binary_sensor import (
    BetterThermostatHeatLossBinarySensor,
    BetterThermostatHeatSourceBinarySensor,
    async_setup_entry,
)
from custom_components.better_thermostat.entity import FollowsThermostat
from custom_components.better_thermostat.utils.anomaly_evidence import (
    AnomalyEvidenceTracker,
)
from tests.factories import ThermostatStandIn


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


def _climate() -> ThermostatStandIn:
    """Build a thermostat stand-in that carries the values the sensor reads."""
    bt = ThermostatStandIn()
    bt.unique_id = "bt_1"
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


class TestPlatformSetup:
    """The platform builds the two sensors of every thermostat."""

    @pytest.mark.asyncio
    async def test_a_thermostat_without_an_entity_gets_no_sensors(self) -> None:
        """Without a thermostat there is nothing to report on."""
        entry = MagicMock()
        entry.runtime_data = BetterThermostatData(climate=None)
        add_entities = MagicMock()

        await async_setup_entry(MagicMock(), entry, add_entities)
        add_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_every_thermostat_gets_both_sensors(self) -> None:
        """The platform builds the heat source and the heat loss sensor."""
        entry = MagicMock()
        entry.runtime_data = BetterThermostatData(climate=_climate())
        add_entities = MagicMock()

        with (
            patch(f"{binary_sensor_platform.__name__}.er.async_get"),
            patch(
                f"{binary_sensor_platform.__name__}.remove_unclaimed_registry_entries"
            ),
            patch(f"{binary_sensor_platform.__name__}.async_normalize_bt_entity_ids"),
        ):
            await async_setup_entry(MagicMock(), entry, add_entities)

        (sensors,) = add_entities.call_args.args[:1]
        assert [type(sensor) for sensor in sensors] == [
            BetterThermostatHeatSourceBinarySensor,
            BetterThermostatHeatLossBinarySensor,
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
