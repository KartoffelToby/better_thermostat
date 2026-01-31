# Changelog: Merge Conflict Resolution

## Commit: Resolve merge conflict in sensor.py - Integrate MpcKaSensor with dynamic entity management

**Date:** January 31, 2026  
**Branch:** feature/dynamic-entity-cleanup  
**Type:** Fix (Merge Conflict Resolution)

### Changes Made

#### Resolved Merge Conflict
- **File:** `custom_components/better_thermostat/sensor.py`
- **Conflict:** Between `feature/dynamic-entity-cleanup` branch and `master` branch
- **Resolution Strategy:** Integrate both feature sets seamlessly

#### Technical Details

**From master branch (integrated):**
- Added `BetterThermostatMpcKaSensor` class for MPC Insulation monitoring
- New sensor provides MPC thermal insulation coefficient (Ka) diagnostics
- Unit: `1/min` for insulation decay rate measurement

**From feature/dynamic-entity-cleanup branch (preserved):**
- Complete dynamic entity management system maintained
- Universal algorithm entity cleanup functionality preserved
- Signal-based configuration change detection kept intact

#### Updated Entity Management

**MPC Algorithm Sensors (now 5 total):**
1. `BetterThermostatVirtualTempSensor` - Virtual Temperature
2. `BetterThermostatMpcGainSensor` - MPC Gain  
3. `BetterThermostatMpcLossSensor` - MPC Loss
4. `BetterThermostatMpcKaSensor` - **NEW:** MPC Insulation (Ka)
5. `BetterThermostatMpcStatusSensor` - Learning Status

**Entity Tracking Updated:**
- Added `mpc_ka` entity to automatic cleanup tracking
- All 5 MPC entities now properly managed by dynamic system
- Seamless cleanup when switching away from MPC algorithm

#### Removed Redundant Code

**Legacy MPC Detection:**
- Removed old `has_mpc` boolean logic 
- Eliminated manual MPC sensor instantiation
- Replaced with universal algorithm detection system

### Benefits

✅ **Complete Feature Integration:** Both dynamic entity management and new MPC diagnostics  
✅ **Backward Compatibility:** All existing MPC functionality preserved  
✅ **Enhanced Diagnostics:** New Ka sensor provides thermal insulation insights  
✅ **Automatic Cleanup:** All MPC entities (including Ka) removed when switching algorithms  
✅ **Code Consistency:** Single unified entity management approach  

### Impact Assessment

- **Risk:** Low - Additive changes with proven cleanup system
- **Compatibility:** Full backward compatibility maintained  
- **Performance:** No performance impact, same dynamic loading pattern
- **Functionality:** Enhanced MPC diagnostics + automatic cleanup

### Testing Recommendations

1. **MPC Algorithm Usage:**
   - Verify all 5 MPC sensors appear when MPC enabled
   - Confirm Ka sensor shows insulation coefficient values
   - Test MPC learning status and diagnostics

2. **Algorithm Switching:**
   - Test MPC → Normal: Verify all 5 entities removed
   - Test Normal → MPC: Verify all 5 entities created
   - Test MPC → AI Time Based: Verify cleanup + new entities

3. **Integration Stability:**
   - Home Assistant restart with MPC active
   - Integration reload scenarios
   - Multiple TRV configurations

### Implementation Notes

- Merge conflict successfully resolved without feature loss
- Dynamic entity management system scales to new MPC sensor automatically  
- Entity tracking properly updated for complete MPC sensor suite
- No breaking changes to existing installations

---

**Summary:** Successfully merged master branch improvements (MpcKaSensor) with dynamic entity cleanup feature while maintaining full functionality and automatic entity lifecycle management.