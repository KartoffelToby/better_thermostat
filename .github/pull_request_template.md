<!--
Before proposing a syntax fix: this project requires Python 3.14.2, and
`except TypeError, ValueError:` without parentheses is PEP 758, valid since 3.14.
Python 3.13 and older reject that line, so a parser that rejects it is out of date.
See CONTRIBUTING.md, section "Python version".
-->

## Motivation:

## Changes:

## Related issue (check one):

- [ ] fixes #<issue number goes here>
- [ ] There is no related issue ticket

## Checklist (check one):

- [ ] I did not change any code (e.g. documentation changes)
- [ ] The code change is tested and works locally.

## Test-Hardware list (for code changes)

<!-- Please specify the hardware/software which was used to test the code locally: -->

HA Version:
Zigbee2MQTT Version:
TRV Hardware:

## New device mappings

<!-- If a new device mapping has been added, please make sure to fill in this checklist: -->

- [ ] I avoided any changes to other device mappings
- [ ] There are no changes in `climate.py`

<!-- If you changed the `climate.py` please create a dedicated PR for this. -->
