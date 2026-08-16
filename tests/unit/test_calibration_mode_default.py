"""One default calibration mode, agreed on by every site that names one.

Three places used to disagree: the dropdown label marked
``External Sensor Offset Only`` as the default, the config flow preselected
Time Based, and the runtime fell back to MPC Predictive. A stored config
without ``calibration_mode`` therefore ran a Beta algorithm the UI never
offered as the default.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat import (
    calibration as calibration_module,
    config_flow as config_flow_module,
)
from custom_components.better_thermostat.calibration import calculate_calibration_local
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils import controlling as controlling_module
from custom_components.better_thermostat.utils.const import (
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
)
from custom_components.better_thermostat.utils.helpers import (
    is_calibration_mode,
    normalize_calibration_mode,
)

_RUNTIME_MODULES = (calibration_module, controlling_module)


def _thermostat_without_target(stored_mode: object) -> MagicMock:
    """A thermostat carrying *stored_mode* and no target temperature.

    Without a target, only a mode that needs none produces a value, which
    makes the resolved mode observable from the return value alone.
    """
    bt = MagicMock()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = 0.5
    bt.attr_hvac_action = None
    bt.hvac_action = None
    bt.cur_temp = 20.0
    bt.bt_target_temp = None

    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = lambda _self, _entity_id, offset: float(
        offset
    )

    bt.real_trvs = {
        "climate.trv": Trv.from_legacy_dict(
            "climate.trv",
            {
                "advanced": {"calibration_mode": stored_mode},
                "current_temperature": 22.0,
                "last_calibration": 2.0,
                "local_calibration_step": 0.1,
                "local_calibration_min": -5.0,
                "local_calibration_max": 5.0,
                "target_temp_step": 0.5,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "model_quirks": quirks,
            },
        )
    }
    return bt


def test_default_is_a_known_mode():
    """The shared default has to be a real member of the enum."""
    assert DEFAULT_CALIBRATION_MODE in set(CalibrationMode)


def test_default_is_not_a_beta_mode():
    """The silent default must not be one of the modes labelled Beta."""
    assert DEFAULT_CALIBRATION_MODE not in {
        CalibrationMode.MPC_CALIBRATION,
        CalibrationMode.MPC_V2_CALIBRATION,
    }


def test_no_runtime_fallback_hardcodes_a_mode():
    """Runtime fallbacks read the shared default instead of naming a mode."""
    for module in _RUNTIME_MODULES:
        source = inspect.getsource(module)
        assert '"calibration_mode", CalibrationMode.' not in source, (
            f"{module.__name__} hardcodes a calibration-mode fallback"
        )
        assert '"calibration_mode", DEFAULT_CALIBRATION_MODE' in source
        assert "_calibration_mode = CalibrationMode." not in source, (
            f"{module.__name__} rewrites an unresolved mode to a named one "
            "instead of the shared default"
        )


@pytest.mark.parametrize("stored_mode", [None, 3], ids=["null", "unmappable-number"])
def test_unresolvable_stored_mode_falls_back_to_the_shared_default(
    monkeypatch, stored_mode
):
    """A stored mode that normalizes to ``None`` resolves to the shared default.

    The default is swapped for a mode that works without a target, so a
    returned value proves the fallback read the constant rather than
    naming a mode of its own.
    """
    monkeypatch.setattr(
        calibration_module, "DEFAULT_CALIBRATION_MODE", CalibrationMode.DEFAULT
    )
    thermostat = _thermostat_without_target(stored_mode)

    assert calculate_calibration_local(thermostat, "climate.trv") == 0.0


def test_a_named_but_unknown_mode_does_not_become_the_default(monkeypatch):
    """A string naming a mode this version does not know stays unresolved.

    ``normalize_calibration_mode`` hands an unrecognized string back
    unchanged instead of returning ``None``, and that is the answer the
    rest of the code reads: ``is_calibration_mode`` reports ``False`` for
    it against every mode. A config that names something is not a config
    that names nothing, so it does not take the shared default.
    """
    assert normalize_calibration_mode("a mode from another version") == (
        "a mode from another version"
    )
    assert not is_calibration_mode(
        "a mode from another version", DEFAULT_CALIBRATION_MODE
    )

    monkeypatch.setattr(
        calibration_module, "DEFAULT_CALIBRATION_MODE", CalibrationMode.DEFAULT
    )
    thermostat = _thermostat_without_target("a mode from another version")

    assert calculate_calibration_local(thermostat, "climate.trv") is None


def test_config_flow_uses_the_shared_default():
    """The form default and the submission normaliser read the same constant."""
    source = inspect.getsource(config_flow_module)
    assert "CONF_CALIBRATION_MODE, DEFAULT_CALIBRATION_MODE" in source
    assert (
        "CONF_CALIBRATION_MODE, CalibrationMode.HEATING_POWER_CALIBRATION" not in source
    )


def test_only_the_real_default_is_labelled_default():
    """Exactly one dropdown entry carries the (Default) marker, and it is the default."""
    options = config_flow_module.CALIBRATION_MODE_SELECTOR.config["options"]
    marked = [opt for opt in options if "(Default)" in opt["label"]]
    assert len(marked) == 1
    assert marked[0]["value"] == DEFAULT_CALIBRATION_MODE


def test_every_mode_is_offered_exactly_once():
    """The selector stays a faithful listing of the enum."""
    options = config_flow_module.CALIBRATION_MODE_SELECTOR.config["options"]
    values = [opt["value"] for opt in options]
    assert len(values) == len(set(values))
    assert set(values) == set(CalibrationMode)
