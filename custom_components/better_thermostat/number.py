"""Better Thermostat Number Platform."""

import logging

from homeassistant.components.number import NumberEntity, NumberDeviceClass, NumberMode
from homeassistant.const import UnitOfTemperature, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.components.climate.const import PRESET_NONE

from .utils.calibration.pid import (
    _PID_STATES,
    build_pid_key,
    PIDState,
    DEFAULT_PID_KP,
    DEFAULT_PID_KI,
    DEFAULT_PID_KD,
)
from .utils.const import CalibrationMode

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Better Thermostat numbers."""
    bt_climate = hass.data[DOMAIN][entry.entry_id].get("climate")
    if not bt_climate:
        _LOGGER.warning(
            "Better Thermostat climate entity not found for entry %s. "
            "Numbers will not be added.",
            entry.entry_id,
        )
        return

    numbers = []
    # Create a number entity for each preset mode (except NONE)
    _LOGGER.debug(
        "Better Thermostat Number: Found preset modes: %s", bt_climate.preset_modes
    )
    for preset_mode in bt_climate.preset_modes:
        if preset_mode == PRESET_NONE:
            continue
        numbers.append(BetterThermostatPresetNumber(bt_climate, preset_mode))

    # Create PID numbers for each TRV if PID calibration is enabled
    has_multiple_trvs = len(bt_climate.real_trvs) > 1
    for trv_entity_id, trv_data in bt_climate.real_trvs.items():
        advanced = trv_data.get("advanced", {})
        if advanced.get("calibration_mode") == CalibrationMode.PID_CALIBRATION:
            numbers.append(BetterThermostatPIDNumber(
                bt_climate, trv_entity_id, "kp", has_multiple_trvs))
            numbers.append(BetterThermostatPIDNumber(
                bt_climate, trv_entity_id, "ki", has_multiple_trvs))
            numbers.append(BetterThermostatPIDNumber(
                bt_climate, trv_entity_id, "kd", has_multiple_trvs))

    async_add_entities(numbers)


class BetterThermostatPresetNumber(NumberEntity, RestoreEntity):
    """Representation of a Better Thermostat Preset Temperature Number."""

    _attr_has_entity_name = True
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, bt_climate, preset_mode):
        """Initialize the number."""
        self._bt_climate = bt_climate
        self._preset_mode = preset_mode
        self._attr_unique_id = f"{bt_climate.unique_id}_preset_{preset_mode}"
        self._attr_name = f"Preset {preset_mode.capitalize()}"

        # Set min/max/step based on climate entity configuration
        self._attr_native_min_value = bt_climate.min_temp
        self._attr_native_max_value = bt_climate.max_temp
        self._attr_native_step = bt_climate.target_temperature_step

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            None,
            "unknown",
            "unavailable",
        ):
            try:
                val = float(last_state.state)
                self._bt_climate._preset_temperatures[self._preset_mode] = val
                _LOGGER.debug(
                    "Restored preset %s to %s from number entity state",
                    self._preset_mode,
                    val,
                )
            except ValueError:
                pass

    @property
    def device_info(self):
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    def native_value(self) -> float | None:
        """Return the value of the number."""
        return self._bt_climate._preset_temperatures.get(self._preset_mode)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        # Update the storage in the climate entity
        self._bt_climate._preset_temperatures[self._preset_mode] = value

        # If this preset is currently active, update the target temperature immediately
        if self._bt_climate.preset_mode == self._preset_mode:
            await self._bt_climate.async_set_temperature(temperature=value)

        self.async_write_ha_state()
        # Force update of climate entity state to persist the new preset temperature in attributes
        self._bt_climate.async_write_ha_state()


class BetterThermostatPIDNumber(NumberEntity, RestoreEntity):
    """Representation of a Better Thermostat PID Parameter Number."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, bt_climate, trv_entity_id, parameter, show_trv_name=True):
        """Initialize the number."""
        self._bt_climate = bt_climate
        self._trv_entity_id = trv_entity_id
        self._parameter = parameter
        self._attr_unique_id = f"{bt_climate.unique_id}_{trv_entity_id}_pid_{parameter}"

        if show_trv_name:
            trv_state = bt_climate.hass.states.get(trv_entity_id)
            trv_name = trv_state.name if trv_state and trv_state.name else trv_entity_id
            self._attr_translation_key = f"pid_{parameter}"
            self._attr_translation_placeholders = {"trv_name": trv_name}
        else:
            self._attr_translation_key = f"pid_{parameter}_no_trv"

        if parameter == "kp":
            self._attr_native_min_value = 0.0
            self._attr_native_max_value = 1000.0
            self._attr_native_step = 0.1
        elif parameter == "ki":
            self._attr_native_min_value = 0.0
            self._attr_native_max_value = 100.0
            self._attr_native_step = 0.001
        elif parameter == "kd":
            self._attr_native_min_value = 0.0
            self._attr_native_max_value = 10000.0
            self._attr_native_step = 1.0

    @property
    def device_info(self):
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    def native_value(self) -> float | None:
        """Return the value of the number."""
        # Try to get the value from the current active PID state
        key = build_pid_key(self._bt_climate, self._trv_entity_id)
        pid_state = _PID_STATES.get(key)

        if pid_state is not None:
            val = getattr(pid_state, f"pid_{self._parameter}")
            if val is not None:
                return val

        # Defaults
        if self._parameter == "kp":
            return DEFAULT_PID_KP
        if self._parameter == "ki":
            return DEFAULT_PID_KI
        if self._parameter == "kd":
            return DEFAULT_PID_KD
        return 0.0

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        # Update ONLY the current PID state to avoid overwriting learned values for other temperatures
        key = build_pid_key(self._bt_climate, self._trv_entity_id)

        # Ensure state exists
        if key not in _PID_STATES:
            _PID_STATES[key] = PIDState()

        pid_state = _PID_STATES[key]

        _LOGGER.debug("Updating PID state key %s: %s -> %s", key,
                      getattr(pid_state, f"pid_{self._parameter}"), value)
        setattr(pid_state, f"pid_{self._parameter}", value)

        self.async_write_ha_state()
