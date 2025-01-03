---
name: Bug report
about: Create a report to let us know of any issues you encountered
labels: new bug
assignees: kartoffeltoby
---

<!--
Please take care to fill the data as complete as possible.
The more information you provide, the higher the chances are that we can reporoduce and fix the issue for you!
-->

### Description

<!-- Description of the issue -->

### Steps to Reproduce

1. <!-- First Step -->
2. <!-- Second Step -->
3. <!-- and so on… -->

**Expected behavior:**

<!-- What you expect to happen -->

**Actual behavior:**

<!-- What happens -->

### Versions and HW

<!-- Provide both, HA (Home Assistant) and BT (Better Thermostat) version -->
Home Assistant: 
Better Thermostat: 
<!-- Thermostat valve model(s) -->
TRV(s):

### Debug data

**diagnostic data**
<!--
IMPORTANT:
Download and paste the diagnostic data from your Better Thermostat Entity(s) below.
https://www.home-assistant.io/docs/configuration/troubleshooting/#download-diagnostics
-->

```json
{
  YOUR DEVICE DIAGNOSTICS JSON OUTPUT HERE
}
```

**debug log**
<!--
Depending on how complicated you issue is, it might be necessary to enable debug logging for BT,
reproduce the issue, and then upload this logfile here.
https://www.home-assistant.io/docs/configuration/troubleshooting/#enabling-debug-logging
-->

<!--
Alternatively your Home Assistant system log might be needed - Download here (top right corner):
https://my.home-assistant.io/redirect/logs
-> This might contain sensitive data though, so it's highly adviced *NOT* to share this file publicly.
-->

**graphs**
<!--
For issues in regards to the calibration / control routines, it is very helpful to also
provide statistics graph screenshots from HA for the following entities (from the time you had issues):
- BT climate entity
- TRV climate entities controlled by BT
- (optional) Valve opening states
-->

### Additional Information

<!-- Any additional information, configuration, or data that might be necessary to reproduce the issue. -->
