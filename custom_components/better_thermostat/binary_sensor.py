"""Better Thermostat Binary Sensor Platform.

The MPC v2 observer measures the heat flow the radiator does not explain: the
sun through the window, a fireplace in the next room, a draught at the door.
Two sensors turn that measurement into the statements a user can act on: an
additional heat source is warming the room, or an unexpected heat loss is
cooling it.

Four more show the anomalies the anomaly region found, one sensor for each:
a suspected open window, a sustained heat loss, no heat delivery and a valve
stuck open. They say on the device what the ``anomalies`` attribute of the
thermostat lists.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import TYPE_CHECKING, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BetterThermostatConfigEntry
from .core.desired import AnomalyCode
from .core.fsm.anomaly import FaultSlot, visible_codes as anomaly_visible_codes
from .entity import FollowsThermostat, remove_unclaimed_registry_entries
from .utils.const import CalibrationMode
from .utils.helpers import async_normalize_bt_entity_ids, configured_calibration_mode

if TYPE_CHECKING:
    from .climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

# Every entity is pushed and none polls; actions are not limited per platform.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterThermostatConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors that have something to report on the room.

    Every room gets the anomalies its TRVs and its room sensor are enough to
    find. Only the MPC v2 observer feeds the heat source, the heat loss and
    the sustained heat loss, so a room none of whose TRVs runs it gets none
    of these; the registry entries such a room left behind are removed. A
    change of the calibration mode reloads the entry, and this setup then
    decides again.
    """
    bt_climate = entry.runtime_data.climate
    if not bt_climate:
        _LOGGER.warning(
            "Better Thermostat climate entity not found for entry %s. "
            "Binary sensors will not be added.",
            entry.entry_id,
        )
        return

    sensors: list[BinarySensorEntity] = [
        BetterThermostatSuspectedOpenWindowBinarySensor(bt_climate),
        BetterThermostatNoHeatDeliveryBinarySensor(bt_climate),
        BetterThermostatValveStuckOpenBinarySensor(bt_climate),
    ]
    if any(
        configured_calibration_mode(trv.advanced) is CalibrationMode.MPC_V2_CALIBRATION
        for trv in bt_climate.real_trvs.values()
    ):
        sensors += [
            BetterThermostatSustainedHeatLossBinarySensor(bt_climate),
            BetterThermostatHeatSourceBinarySensor(bt_climate),
            BetterThermostatHeatLossBinarySensor(bt_climate),
        ]

    remove_unclaimed_registry_entries(
        er.async_get(hass),
        entry,
        Platform.BINARY_SENSOR,
        (sensor.unique_id for sensor in sensors),
        bt_climate,
    )
    async_normalize_bt_entity_ids(hass, entry, Platform.BINARY_SENSOR)
    async_add_entities(sensors, True)


class _BtBinarySensor(FollowsThermostat, BinarySensorEntity):
    """Base class for the binary sensors of a thermostat."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _unique_id_suffix: str

    def __init__(self, bt_climate: BetterThermostat) -> None:
        """Initialize the binary sensor."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_{self._unique_id_suffix}"

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_thermostat()

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return self._bt_climate.device_info


class _BtSourceBinarySensor(_BtBinarySensor):
    """Base class for the sensors that report a source of heat in the room.

    The observer's disturbance is measured against the room's own baseline,
    so what the sensor reports is the heat flow that is there now and does
    not belong to the room's usual behaviour. Without an observer reading
    the sensor is unavailable: nothing measures the room, so nothing is
    known about it.
    """

    @property
    @override
    def available(self) -> bool:
        """Return whether an observer reading is there to report."""
        if not self._bt_climate._available:
            return False
        return (
            self._bt_climate.anomaly_evidence.fresh_observer_disturbance(
                self._bt_climate.clock.monotonic()
            )
            is not None
        )


class BetterThermostatHeatSourceBinarySensor(_BtSourceBinarySensor):
    """Sensor that reports a heat gain the radiator does not explain."""

    _attr_translation_key = "heat_source_detected"
    _unique_id_suffix = "heat_source_detected"

    @property
    @override
    def is_on(self) -> bool:
        """Return whether a heat source is warming the room."""
        return self._bt_climate.anomaly_evidence.heat_source_active


class BetterThermostatHeatLossBinarySensor(_BtSourceBinarySensor):
    """Sensor that reports a heat loss the radiator does not explain."""

    _attr_translation_key = "heat_loss_detected"
    _unique_id_suffix = "heat_loss_detected"

    @property
    @override
    def is_on(self) -> bool:
        """Return whether a heat loss is cooling the room."""
        return self._bt_climate.anomaly_evidence.cool_source_active


class _BtAnomalyBinarySensor(_BtBinarySensor):
    """Base class for the sensors that report an anomaly of the room.

    A sensor is on for as long as the thermostat lists its anomaly, and
    names the moment it was first reported. While anomaly detection is off
    nothing watches the room, so the sensor is unavailable: off would read
    as a room without a problem.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _anomaly_code: AnomalyCode
    _since: datetime | None = None

    @override
    @callback
    def _async_write_ha_state(self) -> None:
        """Stamp the moment the anomaly was first reported, then publish.

        The state's own last change starts over whenever the sensor has been
        unavailable; this moment stays for as long as the anomaly does.
        """
        if not self.is_on:
            self._since = None
        elif self._since is None:
            self._since = self._bt_climate.clock.utcnow()
        super()._async_write_ha_state()

    def _since_attribute(self) -> str | None:
        """Return the moment the anomaly was first reported, as ISO 8601."""
        return None if self._since is None else self._since.isoformat()

    @property
    @override
    def available(self) -> bool:
        """Return whether the anomaly region watches the room."""
        return (
            self._bt_climate._available
            and self._bt_climate.kernel_state.anomaly_detection
        )

    @property
    @override
    def is_on(self) -> bool:
        """Return whether the thermostat lists this sensor's anomaly."""
        room_codes, _ = anomaly_visible_codes(self._bt_climate.kernel_state.anomaly)
        return self._anomaly_code in room_codes


class _BtRoomAnomalyBinarySensor(_BtAnomalyBinarySensor):
    """An anomaly of the room as a whole, with the phase its detector is in.

    The phase tells a finding that is being checked, one that is recovering
    and one that has expired apart: an expired one is still listed and no
    longer holds learning or heating.
    """

    def _slot(self) -> FaultSlot:
        """Return the fault slot of this sensor's detector."""
        raise NotImplementedError

    @property
    @override
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return the phase of the detector and since when it reports."""
        return {"phase": self._slot().phase.value, "since": self._since_attribute()}


class _BtTrvAnomalyBinarySensor(_BtAnomalyBinarySensor):
    """An anomaly a single TRV can have, with the TRVs it was found on."""

    @property
    @override
    def extra_state_attributes(self) -> dict[str, list[str] | str | None]:
        """Return the TRVs the anomaly was found on and since when it is reported.

        No TRV is named for an anomaly of the whole room.
        """
        _, trv_codes = anomaly_visible_codes(self._bt_climate.kernel_state.anomaly)
        return {
            "trvs": [
                entity_id
                for entity_id, codes in trv_codes.items()
                if self._anomaly_code in codes
            ],
            "since": self._since_attribute(),
        }


class BetterThermostatSuspectedOpenWindowBinarySensor(_BtRoomAnomalyBinarySensor):
    """Sensor that reports a rapid heat loss no contact sensor explains."""

    _attr_translation_key = "suspected_open_window"
    _unique_id_suffix = "suspected_open_window"
    _anomaly_code = AnomalyCode.UNEXPECTED_RAPID_LOSS

    @override
    def _slot(self) -> FaultSlot:
        return self._bt_climate.kernel_state.anomaly.rapid_loss


class BetterThermostatSustainedHeatLossBinarySensor(_BtRoomAnomalyBinarySensor):
    """Sensor that reports a steady extra heat loss, such as an open interior door."""

    _attr_translation_key = "sustained_heat_loss"
    _unique_id_suffix = "sustained_heat_loss"
    _anomaly_code = AnomalyCode.SUSTAINED_PARASITIC_LOSS

    @override
    def _slot(self) -> FaultSlot:
        return self._bt_climate.kernel_state.anomaly.parasitic_loss


class BetterThermostatNoHeatDeliveryBinarySensor(_BtTrvAnomalyBinarySensor):
    """Sensor that reports heating that is asked for and does not arrive."""

    _attr_translation_key = "no_heat_delivery"
    _unique_id_suffix = "no_heat_delivery"
    _anomaly_code = AnomalyCode.NO_HEAT_DELIVERY


class BetterThermostatValveStuckOpenBinarySensor(_BtTrvAnomalyBinarySensor):
    """Sensor that reports a valve that heats the room although it is shut."""

    _attr_translation_key = "valve_stuck_open"
    _unique_id_suffix = "valve_stuck_open"
    _anomaly_code = AnomalyCode.VALVE_STUCK_OPEN
