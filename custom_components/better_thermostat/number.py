"""Better Thermostat Number Platform."""

from __future__ import annotations

import logging
import math

from homeassistant.components.climate.const import (
    PRESET_ACTIVITY,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    PRESET_SLEEP,
    HVACMode,
)
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, Platform, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import BetterThermostatConfigEntry
from .entity import (
    FollowsThermostat,
    TrvNamedEntity,
    current_trv_name,
    remove_unclaimed_registry_entries,
)
from .sensor import _ACTIVE_PID_NUMBERS, _ACTIVE_PRESET_NUMBERS
from .utils.calibration.pid import (
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    build_pid_key,
)
from .utils.const import (
    CONF_CALIBRATION_MODE,
    CONF_CALIBRATION_OUTPUT,
    CalibrationMode,
    CalibrationOutput,
)
from .utils.helpers import (
    async_normalize_bt_entity_ids,
    convert_to_float_celsius,
    get_cool_temperature_range,
    get_heat_temperature_range,
)
from .utils.scheduler import request_control_cycle

_LOGGER = logging.getLogger(__name__)

_PRESET_TRANSLATION_KEYS = {
    PRESET_ECO: "preset_eco",
    PRESET_AWAY: "preset_away",
    PRESET_BOOST: "preset_boost",
    PRESET_COMFORT: "preset_comfort",
    PRESET_HOME: "preset_home",
    PRESET_SLEEP: "preset_sleep",
    PRESET_ACTIVITY: "preset_activity",
}
# With a cooler configured the heating preset becomes the lower bound of a
# range, so it needs its own name next to the cooling preset upper bound.
_PRESET_MIN_TRANSLATION_KEYS = {
    preset: f"{key}_min" for preset, key in _PRESET_TRANSLATION_KEYS.items()
}
_PRESET_MAX_TRANSLATION_KEYS = {
    preset: f"{key}_max" for preset, key in _PRESET_TRANSLATION_KEYS.items()
}


def _is_usable_setting(bt_climate, value: float, setting: str) -> bool:
    """Return whether ``value`` is a finite number, and warn when it is not.

    ``number.set_value`` accepts ``nan``, and every comparison with NaN is
    false, so it passes Home Assistant's range check and every clamp of the
    setting it lands in. It is refused before anything is stored.
    """
    if math.isfinite(value):
        return True
    _LOGGER.warning(
        "Better Thermostat %s: %s is not a usable %s, keeping the current value",
        bt_climate.device_name,
        value,
        setting,
    )
    return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterThermostatConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Better Thermostat numbers."""
    bt_climate = entry.runtime_data.climate
    if not bt_climate:
        _LOGGER.warning(
            "Better Thermostat climate entity not found for entry %s. "
            "Numbers will not be added.",
            entry.entry_id,
        )
        return

    numbers: list[NumberEntity] = []
    preset_unique_ids: dict[str | None, dict[str, str | bool]] = {}
    pid_unique_ids = {}
    # Create number entities for each preset mode (except NONE)
    _LOGGER.debug(
        "Better Thermostat Number: Found preset modes: %s", bt_climate.preset_modes
    )
    has_heater = len(bt_climate.real_trvs) > 0
    has_cooler = bt_climate.cooler_entity_id is not None
    for preset_mode in bt_climate.preset_modes:
        if preset_mode == PRESET_NONE:
            continue
        if has_heater:
            preset_number = BetterThermostatPresetNumber(bt_climate, preset_mode)
            numbers.append(preset_number)
            preset_unique_ids[preset_number._attr_unique_id] = {"preset": preset_mode}
        if has_cooler:
            cool_number = BetterThermostatPresetCoolNumber(bt_climate, preset_mode)
            numbers.append(cool_number)
            preset_unique_ids[cool_number._attr_unique_id] = {
                "preset": preset_mode,
                "cool": True,
            }

    # Create PID numbers for each TRV if PID calibration is enabled
    if hasattr(bt_climate, "all_trvs"):
        has_multiple_trvs = len(bt_climate.all_trvs) > 1
        for trv_config in bt_climate.all_trvs:
            trv_entity_id = trv_config.get("trv")
            if not trv_entity_id:
                continue

            advanced = trv_config.get("advanced", {})
            calibration_mode = advanced.get(CONF_CALIBRATION_MODE)
            calibration_output = advanced.get(CONF_CALIBRATION_OUTPUT)

            # Normalize string values to CalibrationMode enum
            try:
                if isinstance(calibration_mode, str):
                    calibration_mode = CalibrationMode(calibration_mode)
            except ValueError, TypeError:
                calibration_mode = None

            try:
                if isinstance(calibration_output, str):
                    calibration_output = CalibrationOutput(calibration_output)
            except ValueError, TypeError:
                calibration_output = None

            if calibration_mode == CalibrationMode.PID_CALIBRATION:
                for param in ["kp", "ki", "kd"]:
                    pid_number = BetterThermostatPIDNumber(
                        bt_climate, trv_entity_id, param, has_multiple_trvs
                    )
                    numbers.append(pid_number)
                    pid_unique_ids[pid_number._attr_unique_id] = {
                        "trv": trv_entity_id,
                        "param": param,
                    }

            if calibration_output == CalibrationOutput.DIRECT_VALVE_BASED:
                numbers.append(
                    BetterThermostatValveMaxOpeningNumber(
                        bt_climate, trv_entity_id, has_multiple_trvs
                    )
                )

    # Track created number entities for cleanup
    _ACTIVE_PRESET_NUMBERS[entry.entry_id] = preset_unique_ids
    _ACTIVE_PID_NUMBERS[entry.entry_id] = pid_unique_ids

    _LOGGER.debug(
        "Better Thermostat %s: Created %d preset and %d PID number entities",
        bt_climate.device_name,
        len(preset_unique_ids),
        len(pid_unique_ids),
    )

    remove_unclaimed_registry_entries(
        er.async_get(hass),
        entry,
        Platform.NUMBER,
        (number.unique_id for number in numbers),
        bt_climate,
    )
    async_normalize_bt_entity_ids(hass, entry, Platform.NUMBER)
    async_add_entities(numbers)


class BetterThermostatPresetNumber(FollowsThermostat, NumberEntity, RestoreEntity):
    """Representation of a Better Thermostat Preset Temperature Number."""

    _attr_has_entity_name = True
    # NumberEntity and the Entity/RestoreEntity bases type _attr_device_class
    # incompatibly; the value itself is correct. Pyright reports this on the
    # class line (like the other entity classes), so a per-line ignore here has
    # no effect; left unsuppressed for consistency with the rest of the codebase.
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, bt_climate, preset_mode):
        """Initialize the number."""
        self._bt_climate = bt_climate
        self._preset_mode = preset_mode
        self._attr_unique_id = f"{bt_climate.unique_id}_preset_{preset_mode}"
        if bt_climate.cooler_entity_id is not None:
            self._attr_translation_key = _PRESET_MIN_TRANSLATION_KEYS[preset_mode]
        else:
            self._attr_translation_key = _PRESET_TRANSLATION_KEYS[preset_mode]

    # The range is the thermostat's heating channel and the step is the
    # thermostat's. Its startup resolves them from the device, which on a boot
    # runs after this entity is built, so they are read from the thermostat
    # and republished with its state.
    @property
    def native_min_value(self) -> float:
        """Return the lowest heating temperature the preset can hold."""
        return get_heat_temperature_range(self._bt_climate)[0]

    @property
    def native_max_value(self) -> float:
        """Return the highest heating temperature the preset can hold."""
        return get_heat_temperature_range(self._bt_climate)[1]

    @property
    def native_step(self) -> float:
        """Return the step the thermostat's setpoint moves in.

        Home Assistant converts this number's value and range into the system
        unit but publishes its step as given, so the step is the one of the
        system unit, as the climate entity publishes it.
        """
        return self._bt_climate.target_temperature_step or 0.1

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_thermostat()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        # ``last_state.state`` is in HA's display unit, not the native
        # Celsius; normalise via the saved ``unit_of_measurement``.
        saved_unit = last_state.attributes.get("unit_of_measurement")
        val_celsius = convert_to_float_celsius(
            last_state.state,
            self._bt_climate.device_name,
            "BetterThermostatPresetNumber.async_added_to_hass",
            unit_of_measurement=saved_unit,
        )
        if val_celsius is None:
            return
        self._bt_climate.preset_mgr.update_temperature(self._preset_mode, val_celsius)
        _LOGGER.debug(
            "Restored preset %s to %s°C from number entity state (saved unit=%s)",
            self._preset_mode,
            val_celsius,
            saved_unit,
        )
        # The climate entity picks the active preset's target while this
        # platform is still being set up, so a value restored here for the
        # active preset has to be pushed onto the target it already chose. It
        # goes in the way the startup restore puts a stored target back —
        # bounded, and past the manual-change check, which reads a target that
        # differs from the preset's own temperature as the user overriding the
        # preset and switches the preset off.
        if self._bt_climate.preset_mode == self._preset_mode:
            bounded = self._bt_climate._bound_target_to_range(val_celsius)
            if self._bt_climate.heat_target_temperature != bounded:
                self._bt_climate.heat_target_temperature = bounded
                if self._bt_climate.bt_hvac_mode != HVACMode.OFF:
                    request_control_cycle(self._bt_climate)
        # The thermostat state carries the preset map a restart restores from,
        # so the restored value is published whether or not it moved the target.
        self._bt_climate.async_write_ha_state()

    @property
    def device_info(self):
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    def native_value(self) -> float | None:
        """Return the value of the number."""
        return self._bt_climate.preset_mgr.get_temperature(self._preset_mode)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        if not _is_usable_setting(
            self._bt_climate, value, f"{self._preset_mode} preset temperature"
        ):
            return
        # Update the storage in the climate entity
        self._bt_climate.preset_mgr.update_temperature(self._preset_mode, value)

        # If this preset is currently active, update the target temperature immediately
        if self._bt_climate.preset_mode == self._preset_mode:
            await self._bt_climate.async_set_temperature(temperature=value)

        self.async_write_ha_state()
        # Force update of climate entity state to persist the new preset temperature in attributes
        self._bt_climate.async_write_ha_state()


class BetterThermostatPresetCoolNumber(BetterThermostatPresetNumber):
    """Representation of a cooling preset temperature number.

    This entity exposes the high target temperature for a single preset when a
    cooler is configured. Stored values are kept in the owning climate entity
    and are applied immediately when the represented preset is active.
    """

    def __init__(self, bt_climate, preset_mode):
        """Initialize the cooling preset number.

        Parameters
        ----------
        bt_climate
            Better Thermostat climate entity that owns the preset settings.
        preset_mode
            Preset mode represented by this number entity.
        """
        super().__init__(bt_climate, preset_mode)
        self._attr_unique_id = f"{bt_climate.unique_id}_preset_{preset_mode}_cool"
        self._attr_translation_key = _PRESET_MAX_TRANSLATION_KEYS[preset_mode]

    async def async_added_to_hass(self) -> None:
        """Restore the last persisted cooling preset value.

        The restored Home Assistant state is converted to Celsius before it is
        stored in the climate entity's cooling preset map.

        Returns
        -------
        None
            This method only updates internal state.
        """
        # Skip BetterThermostatPresetNumber.async_added_to_hass (heating-only preset
        # restore into preset_mgr) and call the RestoreEntity/NumberEntity bases; this
        # entity restores into the cooling map below instead.
        await super(BetterThermostatPresetNumber, self).async_added_to_hass()
        self._follow_thermostat()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        saved_unit = last_state.attributes.get("unit_of_measurement")
        val_celsius = convert_to_float_celsius(
            last_state.state,
            self._bt_climate.device_name,
            "BetterThermostatPresetCoolNumber.async_added_to_hass",
            unit_of_measurement=saved_unit,
        )
        if val_celsius is None:
            return
        self._bt_climate._preset_cool_temperatures[self._preset_mode] = val_celsius
        _LOGGER.debug(
            "Restored cool preset %s to %s°C from number entity state (saved unit=%s)",
            self._preset_mode,
            val_celsius,
            saved_unit,
        )

    @property
    def native_min_value(self) -> float:
        """Return the lowest cooling temperature the preset can hold."""
        return get_cool_temperature_range(self._bt_climate)[0]

    @property
    def native_max_value(self) -> float:
        """Return the highest cooling temperature the preset can hold."""
        return get_cool_temperature_range(self._bt_climate)[1]

    @property
    def native_value(self) -> float | None:
        """Return the configured cooling temperature for this preset.

        Returns
        -------
        float | None
            Cooling temperature in Celsius, or ``None`` if no value is stored.
        """
        return self._bt_climate._preset_cool_temperatures.get(self._preset_mode)

    async def async_set_native_value(self, value: float) -> None:
        """Set the cooling temperature for this preset.

        Parameters
        ----------
        value
            Requested cooling preset temperature in Celsius.

        Returns
        -------
        None
            This method stores the preset value and updates the active cooling
            target when this preset is currently selected.
        """
        if not _is_usable_setting(
            self._bt_climate, value, f"{self._preset_mode} cooling preset temperature"
        ):
            return
        cool_value = value
        if (
            self._bt_climate.preset_mode == self._preset_mode
            and self._bt_climate.heat_target_temperature is not None
            and value <= self._bt_climate.heat_target_temperature
        ):
            step = self._bt_climate.bt_target_temp_step or 0.5
            cool_value = self._bt_climate.heat_target_temperature + step

        cool_lower, cool_upper = get_cool_temperature_range(self._bt_climate)
        cool_value = min(cool_upper, max(cool_lower, cool_value))
        self._bt_climate._preset_cool_temperatures[self._preset_mode] = cool_value

        if self._bt_climate.preset_mode == self._preset_mode:
            self._bt_climate.cool_target_temperature = cool_value
            self._bt_climate._enforce_cool_above_heat()
            # The ordering only moves a cooling target that is set, so it stays set.
            enforced = self._bt_climate.cool_target_temperature
            assert enforced is not None
            self._bt_climate._preset_cool_temperatures[self._preset_mode] = enforced
            if self._bt_climate.bt_hvac_mode != HVACMode.OFF:
                request_control_cycle(self._bt_climate)

        self.async_write_ha_state()
        self._bt_climate.async_write_ha_state()


class BetterThermostatPIDNumber(
    FollowsThermostat, TrvNamedEntity, NumberEntity, RestoreEntity
):
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
            self._attr_translation_key = f"pid_{parameter}"
            self._attr_translation_placeholders = {
                "trv_name": current_trv_name(bt_climate.hass, trv_entity_id)
            }
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

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_trv_name()
        self._follow_thermostat()

    @property
    def device_info(self):
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    def native_value(self) -> float | None:
        """Return the value of the number."""
        # Try to get the value from the current active PID state
        state_mgr = getattr(self._bt_climate, "state_mgr", None)
        if state_mgr is not None:
            key = build_pid_key(self._bt_climate, self._trv_entity_id)
            pid_state = state_mgr.state.pid.get(key)
            if pid_state is not None:
                value = getattr(pid_state, f"pid_{self._parameter}")
                if value is not None:
                    return value

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
        if not _is_usable_setting(
            self._bt_climate, value, f"PID {self._parameter} for {self._trv_entity_id}"
        ):
            return
        state_mgr = getattr(self._bt_climate, "state_mgr", None)
        if state_mgr is None:
            _LOGGER.debug(
                "Cannot set PID %s for %s: state manager not ready",
                self._parameter,
                self._trv_entity_id,
            )
            return

        # Update ONLY the current PID state to avoid overwriting learned values for other temperatures
        key = build_pid_key(self._bt_climate, self._trv_entity_id)
        pid_state = state_mgr.get_pid(key)

        _LOGGER.debug(
            "Updating PID state key %s: %s -> %s",
            key,
            getattr(pid_state, f"pid_{self._parameter}"),
            value,
        )
        setattr(pid_state, f"pid_{self._parameter}", value)
        state_mgr.set_pid(key, pid_state)

        self._bt_climate.schedule_save_state()
        self.async_write_ha_state()


class BetterThermostatValveMaxOpeningNumber(
    FollowsThermostat, TrvNamedEntity, NumberEntity, RestoreEntity
):
    """Representation of a Better Thermostat Valve Max Opening Number."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = "%"

    def __init__(self, bt_climate, trv_entity_id, show_trv_name=True):
        """Initialize the number."""
        self._bt_climate = bt_climate
        self._trv_entity_id = trv_entity_id
        self._attr_unique_id = (
            f"{bt_climate.unique_id}_{trv_entity_id}_valve_max_opening"
        )

        if show_trv_name:
            self._attr_translation_key = "valve_max_opening"
            self._attr_translation_placeholders = {
                "trv_name": current_trv_name(bt_climate.hass, trv_entity_id)
            }
        else:
            self._attr_translation_key = "valve_max_opening_no_trv"

        self._attr_native_min_value = 0.0
        self._attr_native_max_value = 100.0
        self._attr_native_step = 1.0

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_trv_name()
        self._follow_thermostat()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            None,
            "unknown",
            "unavailable",
        ):
            try:
                value = float(last_state.state)
                self._set_value(value)
            except TypeError, ValueError:
                pass

    @property
    def device_info(self):
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    def native_value(self) -> float | None:
        """Return the value of the number."""
        return self._get_value()

    def _get_value(self) -> float:
        trv_state = self._bt_climate.real_trvs.get(self._trv_entity_id)
        value = trv_state.valve_max_opening if trv_state is not None else 100.0
        try:
            return float(value)
        except TypeError, ValueError:
            return 100.0

    def _set_value(self, value: float) -> None:
        trv_state = self._bt_climate.real_trvs.get(self._trv_entity_id)
        if trv_state is None:
            return
        numeric = float(value)
        if not math.isfinite(numeric):
            # Clamping against 0..100 does not remove a non-finite number,
            # it disguises it: NaN and +inf leave the clamp as 100 (the cap
            # stops limiting anything) and -inf as 0 (the valve is held
            # shut). Home Assistant's range check rejects the infinities —
            # `+inf > max_value` holds — but every comparison with NaN is
            # false, so NaN passes it. A restored or internally set value
            # meets no such check at all, which leaves this the last place
            # any of the three can still be recognised as unusable.
            _LOGGER.warning(
                "Better Thermostat %s: %s is not a usable maximum valve "
                "opening for %s, keeping %s %%",
                self._bt_climate.device_name,
                value,
                self._trv_entity_id,
                trv_state.valve_max_opening,
            )
            return
        trv_state.valve_max_opening = max(0.0, min(100.0, numeric))

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        self._set_value(value)
        self.async_write_ha_state()
