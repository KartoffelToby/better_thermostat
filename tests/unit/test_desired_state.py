"""Tests for the core DesiredState type."""

from dataclasses import FrozenInstanceError, asdict
import json

import pytest

from custom_components.better_thermostat.core.desired import DesiredState, TrvDesired
from custom_components.better_thermostat.core.snapshot import HvacMode


def _sample() -> DesiredState:
    return DesiredState(
        call_for_heat=True,
        trvs={
            "climate.trv": TrvDesired(
                entity_id="climate.trv",
                hvac_mode=HvacMode.HEAT,
                setpoint=21.5,
                valve_percent=40.0,
            )
        },
    )


class TestDesiredState:
    """Construction, equality, immutability, and serializability."""

    def test_value_equality(self):
        """Two identically built DesiredStates compare equal."""
        assert _sample() == _sample()

    def test_inequality_on_differing_intent(self):
        """A different setpoint yields a different DesiredState."""
        other = DesiredState(
            call_for_heat=True,
            trvs={"climate.trv": TrvDesired(entity_id="climate.trv", setpoint=19.0)},
        )
        assert _sample() != other

    def test_defaults_express_no_intent(self):
        """The default DesiredState wants no heat and addresses no TRV."""
        desired = DesiredState()
        assert desired.call_for_heat is False
        assert dict(desired.trvs) == {}

    def test_is_frozen(self):
        """DesiredState and TrvDesired fields cannot be reassigned."""
        desired = _sample()
        with pytest.raises(FrozenInstanceError):
            desired.call_for_heat = False
        with pytest.raises(FrozenInstanceError):
            desired.trvs["climate.trv"].setpoint = 5.0

    def test_serializable_for_flight_recorder(self):
        """asdict() output survives json round-tripping."""
        payload = json.dumps(asdict(_sample()))
        restored = json.loads(payload)
        assert restored["call_for_heat"] is True
        assert restored["trvs"]["climate.trv"]["setpoint"] == 21.5
        assert restored["trvs"]["climate.trv"]["hvac_mode"] == "heat"
