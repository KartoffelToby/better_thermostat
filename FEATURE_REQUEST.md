# Feature Request: Automatic Cleanup of Algorithm-Specific Entities

> **🤖 AI Implementation Note**  
> This feature request and its complete technical implementation were developed in collaboration with **Claude AI (Anthropic)** on January 31, 2026. The AI assistant analyzed the existing Better Thermostat codebase, identified the entity management challenges, designed the comprehensive solution architecture, and implemented all code changes across multiple files with full consideration of Home Assistant best practices and extensibility requirements.

## Summary

Automatically remove algorithm-specific sensor entities when switching calibration algorithms, preventing orphaned entities in Home Assistant.

## Problem Description

### Current Behavior

When switching between calibration algorithms (e.g., from MPC Predictive to AI Time Based, from PID Controller to Normal, etc.), algorithm-specific sensor entities remain in Home Assistant:

**MPC Predictive → Other Algorithm:**

- `sensor.thermostat_virtual_temp` (Virtual Temperature)
- `sensor.thermostat_mpc_gain` (MPC Gain)
- `sensor.thermostat_mpc_loss` (MPC Loss)
- `sensor.thermostat_mpc_status` (Learning Status)

**Future Algorithm Entities:**

- PID Controller: PID-specific diagnostic sensors
- TPI Controller: TPI-specific monitoring sensors
- Time Based: Time-based learning sensors

These orphaned entities become stale and confusing for users regardless of which algorithm transition occurs.

### Impact

- **User Experience**: Confusing stale entities in entity list
- **Maintenance**: Manual cleanup required after algorithm changes
- **Interface Pollution**: Irrelevant diagnostic sensors remain visible
- **Consistency**: Similar behavior expected for future algorithm-specific entities

## Proposed Solution

### Technical Approach

Implement dynamic entity management that:

1. **Tracks Active Entities**: Monitor which algorithm-specific entities are currently active
2. **Detects Configuration Changes**: Listen for calibration mode changes via config flow
3. **Automatic Cleanup**: Remove stale entities when no longer needed
4. **Dynamic Creation**: Create new entities when algorithms are enabled

### Implementation Strategy

#### 1. Enhanced Sensor Platform (`sensor.py`)

```python
# Global tracking for all algorithm-specific entities
_ACTIVE_ALGORITHM_ENTITIES = {}  # {entry_id: {algorithm: [entity_unique_ids]}}
_ENTITY_CLEANUP_CALLBACKS = {}

async def _setup_algorithm_sensors(hass, entry, bt_climate):
    """Setup algorithm-specific sensors based on current configuration."""
    current_algorithms = _get_active_algorithms(bt_climate)
    
    # Cleanup stale entities from previous algorithm configurations
    await _cleanup_stale_algorithm_entities(hass, entry.entry_id, bt_climate, current_algorithms)
    
    # Setup algorithm-specific sensors
    if CalibrationMode.MPC_CALIBRATION in current_algorithms:
        # Create MPC sensors + track their unique IDs
    if CalibrationMode.PID_CALIBRATION in current_algorithms:
        # Create PID sensors + track their unique IDs
    # ... extensible for all algorithms

async def _cleanup_stale_algorithm_entities(hass, entry_id, bt_climate, current_algorithms):
    """Remove algorithm-specific entities that are no longer needed."""
    # Compare current vs tracked algorithms and remove stale entities
    # Use entity registry for safe removal
    # Log cleanup actions for transparency
```

#### 2. Configuration Change Detection (`config_flow.py`)

```python
async def _check_calibration_changes(self):
    """Check for calibration algorithm changes and signal entity updates."""
    old_algorithms = self._get_active_algorithms(old_config)
    new_algorithms = self._get_active_algorithms(new_config)
    
    if old_algorithms != new_algorithms:
        algorithms_added = new_algorithms - old_algorithms
        algorithms_removed = old_algorithms - new_algorithms
        
        # Signal configuration change for dynamic entity management
        dispatcher_send(self.hass, signal_key, {"entry_id": entry_id})

def _get_active_algorithms(self, config):
    """Get set of calibration algorithms currently in use."""
    # Scan all TRVs and collect unique calibration modes
    # Return set of active CalibrationMode enums
```

#### 3. Signal-Based Communication (`climate.py`)

```python
def _signal_config_change(self):
    """Signal a configuration change to trigger entity cleanup/recreation."""
    signal_key = SIGNAL_BT_CONFIG_CHANGED.format(self.entity_id)
    dispatcher_send(self.hass, signal_key, {"entry_id": self._entry_id})
```

### Architecture Benefits

#### Scalable Design

- **Extensible**: Easy to add cleanup for future algorithm-specific entities (PID, TPI, etc.)
- **Modular**: Each algorithm can register its own cleanup logic
- **Maintainable**: Clear separation between entity lifecycle and business logic

#### Robust Operation

- **Error Handling**: Graceful cleanup failure handling
- **Logging**: Detailed logs for debugging entity management
- **Performance**: Minimal overhead, only triggered on actual config changes

#### User-Friendly

- **Automatic**: No manual intervention required
- **Transparent**: Clear logging of cleanup actions
- **Reliable**: Works across integration reloads and HA restarts

## Expected Behavior After Implementation

### Scenario 1: MPC → AI Time Based

1. User changes calibration mode from "MPC Predictive" to "AI Time Based"
2. Config flow detects calibration mode change
3. Cleanup automatically removes 4 MPC sensor entities
4. Logs: "Better Thermostat Living Room: Removed 4 MPC entities"
5. Entity list shows only active, relevant entities

### Scenario 2: Normal → PID Controller

1. User enables "PID Controller" calibration mode
2. Config flow detects new PID requirement
3. Setup automatically creates PID diagnostic entities
4. Logs: "Better Thermostat Living Room: Created PID sensors"
5. PID diagnostic entities immediately available

### Scenario 3: Multiple Algorithm Changes

1. User switches from "MPC Predictive" + "PID Controller" to "AI Time Based"
2. Config flow detects removal of MPC and PID algorithms
3. Cleanup removes all MPC and PID entities
4. Logs: "Better Thermostat Living Room: Algorithm configuration changed. Added: [AI_TIME_BASED], Removed: [MPC_CALIBRATION, PID_CALIBRATION]"
5. Only base sensors remain

### Scenario 4: Integration Reload

1. Integration reload triggered (manual or automatic)
2. Setup detects current configuration state
3. Creates only algorithm-appropriate entities
4. No orphaned entities remain from previous states

## Implementation Files

### Core Changes

- `custom_components/better_thermostat/sensor.py`: Dynamic entity management
- `custom_components/better_thermostat/config_flow.py`: Change detection
- `custom_components/better_thermostat/climate.py`: Signal emission

### Key Functions

- `_setup_mpc_sensors()`: Conditional MPC entity creation
- `_cleanup_mpc_entities()`: Safe entity removal via registry
- `_check_calibration_changes()`: Configuration diff detection
- `_register_dynamic_entity_callback()`: Signal-based communication

## Future Extensibility

### Algorithm Support

This pattern enables automatic cleanup for:

- **PID Controller**: PID-specific diagnostic entities
- **TPI Controller**: TPI-specific monitoring entities
- **Future Algorithms**: Any algorithm requiring dedicated entities

### Configuration

```python
# Easy extension for new algorithms
ALGORITHM_ENTITY_PATTERNS = {
    CalibrationMode.MPC_CALIBRATION: {
        "entities": ["*_virtual_temp", "*_mpc_gain", "*_mpc_loss", "*_mpc_status"],
        "description": "Model Predictive Control diagnostics"
    },
    CalibrationMode.PID_CALIBRATION: {
        "entities": ["*_pid_kp", "*_pid_ki", "*_pid_kd", "*_pid_error"],
        "description": "PID Controller parameters and state"
    },
    CalibrationMode.TPI_CALIBRATION: {
        "entities": ["*_tpi_cycle", "*_tpi_output", "*_tpi_target"],
        "description": "Time Proportional Integral monitoring"
    },
    CalibrationMode.HEATING_POWER_CALIBRATION: {
        "entities": ["*_heating_curve", "*_power_factor", "*_learning_progress"],
        "description": "AI Time Based learning sensors"
    }
}

# Automatic cleanup registration
for algorithm, config in ALGORITHM_ENTITY_PATTERNS.items():
    register_algorithm_cleanup(algorithm, config["entities"])
```

## Testing Scenarios

### Manual Testing

1. Create Better Thermostat with MPC mode → Verify 4 entities created
2. Switch to AI Time Based → Verify MPC entities automatically removed
3. Switch to PID Controller → Verify PID entities created, AI entities removed
4. Switch to Normal → Verify all algorithm entities removed
5. Enable multiple algorithms → Verify correct entity combination
6. Reload integration → Verify correct entity state
7. Restart Home Assistant → Verify cleanup persistence

### Algorithm Transition Testing

- **Normal ↔ MPC**: Base entities ↔ Base + MPC entities
- **PID ↔ TPI**: PID entities ↔ TPI entities  
- **MPC + PID ↔ Normal**: Multiple algorithm entities ↔ Base entities
- **Any ↔ Multiple**: Single algorithm ↔ Multiple algorithm combinations

### Edge Cases

- Multiple rapid algorithm changes across different algorithms
- Partial entity cleanup failures for specific algorithms
- Mixed algorithm configurations across multiple TRVs
- Algorithm-specific entity creation failures
- Integration unload during cleanup of multiple algorithm entities
- Entity registry corruption scenarios with algorithm tracking
- Concurrent algorithm changes in multi-TRV setups

## Benefits Summary

### For Users

- ✅ **No Manual Cleanup**: Automatic entity management
- ✅ **Clean Entity List**: Only relevant entities visible
- ✅ **Transparent Operation**: Clear logging of all actions
- ✅ **Reliable Behavior**: Works across all HA operations

### For Developers

- ✅ **Maintainable Code**: Clear entity lifecycle management
- ✅ **Extensible Pattern**: Easy to add new algorithms
- ✅ **Robust Architecture**: Proper error handling and logging
- ✅ **Future-Proof**: Scalable for additional entity types

### For Integration

- ✅ **Professional UX**: Polished, intuitive behavior
- ✅ **Resource Efficiency**: No unnecessary entity overhead
- ✅ **Consistency**: Uniform behavior across all algorithms
- ✅ **Quality**: Enterprise-grade entity management

## Implementation Priority

**Priority**: High
**Complexity**: Medium
**Risk**: Low
**Impact**: High User Experience Improvement

**🤖 AI Development Credits**
This comprehensive feature was conceptualized, designed, and implemented through collaborative AI assistance using Claude (Anthropic). The AI provided:

- Complete codebase analysis and problem identification
- Architectural design for scalable entity management
- Full implementation across sensor.py, config_flow.py, and climate.py
- Comprehensive testing scenarios and edge case considerations
- Future extensibility planning for additional calibration algorithms

This feature significantly improves the user experience by eliminating manual entity cleanup tasks and providing a more professional, polished integration behavior.
