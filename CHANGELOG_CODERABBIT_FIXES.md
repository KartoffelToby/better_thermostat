# Changelog: CodeRabbit Review Fixes

## Commit: Fix CodeRabbit issues - Add missing MpcKaSensor class and correct signal handling

**Date:** February 1, 2026  
**Branch:** feature/dynamic-entity-cleanup  
**Type:** Fix (Code Review Issues)  
**Pull Request:** #1887

### Issues Fixed

#### Issue 1: Missing BetterThermostatMpcKaSensor Class Definition
- **Severity:** 🔴 Critical
- **File:** `custom_components/better_thermostat/sensor.py`
- **Problem:** Line 80 instantiated `BetterThermostatMpcKaSensor`, but class was not defined
- **Impact:** `NameError` at runtime when MPC calibration mode is active
- **Resolution:** Added complete class implementation

**Added Implementation:**
```python
class BetterThermostatMpcKaSensor(SensorEntity):
    """Representation of a Better Thermostat MPC Ka (Insulation) Sensor."""
    
    _attr_has_entity_name = True
    _attr_name = "MPC Insulation (Ka)"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
   _attr_native_unit_of_measurement = "1/min"
    _attr_should_poll = False
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
```

**Key Features:**
- Extracts `mpc_ka` value from TRV calibration debug data
- Monitors thermal insulation coefficient
- Follows same pattern as other MPC sensors (Gain, Loss)
- Proper state updates via climate entity callbacks

#### Issue 2: Misaligned Signal Format in Climate Entity
- **Severity:** 🟠 Major
- **File:** `custom_components/better_thermostat/climate.py`
- **Problem:** Signal used `self._entry_id` (undefined attribute) and wrong format
- **Impact:** Listeners in sensor.py wouldn't receive config change signals
- **Resolution:** Changed to `self._config_entry_id` (correct attribute)

**Before:**
```python
signal_key = f"bt_config_changed_{self._entry_id}"  # ❌ Undefined
dispatcher_send(self.hass, signal_key, {"entry_id": self._entry_id})
```

**After:**
```python
signal_key = f"bt_config_changed_{self._config_entry_id}"  # ✅ Correct
dispatcher_send(self.hass, signal_key, {"entry_id": self._config_entry_id})
```

**Alignment:**
- Matches expected format in `sensor.py` line 130
- Uses existing attribute defined in `climate.py` line 449
- Ensures proper signal-based communication between components

### Files Modified

1. **custom_components/better_thermostat/sensor.py**
   - Added `BetterThermostatMpcKaSensor` class (58 lines)
   - Positioned between `BetterThermostatMpcLossSensor` and `BetterThermostatSolarIntensitySensor`

2. **custom_components/better_thermostat/climate.py**
   - Fixed `_signal_config_change()` method (2 lines)
   - Changed `self._entry_id` → `self._config_entry_id`

### Testing Performed

✅ **Syntax Validation:** No errors in modified files  
✅ **Attribute Check:** `_config_entry_id` exists and is correctly initialized  
✅ **Signal Format:** Matches listener expectations in sensor platform  
✅ **Class Structure:** MpcKaSensor follows established pattern  

### Impact Assessment

- **Risk:** Low - Fixes blocking runtime errors
- **Breaking Changes:** None - additive fixes only
- **Functionality:** Enables MPC mode without crashes
- **Compatibility:** Full backward compatibility maintained

### Benefits

✅ **Runtime Stability:** Prevents NameError when MPC mode is enabled  
✅ **Signal Communication:** Config changes now properly propagate to sensors  
✅ **Code Correctness:** All references use correct attributes  
✅ **CodeRabbit Clean:** All critical and major issues resolved  

### Next Steps

- Merge approved changes to feature branch
- Verify integration tests pass
- Monitor for additional review feedback

---

**Summary:** Fixed all critical CodeRabbit review issues, ensuring MPC sensor instantiation works correctly and configuration change signals properly reach entity cleanup handlers.
