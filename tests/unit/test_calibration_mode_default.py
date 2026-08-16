"""One default calibration mode, agreed on by every site that names one.

Three places used to disagree: the dropdown label marked
``External Sensor Offset Only`` as the default, the config flow preselected
Time Based, and the runtime fell back to MPC Predictive. A stored config
without ``calibration_mode`` therefore ran a Beta algorithm the UI never
offered as the default.
"""

from __future__ import annotations

import inspect

from custom_components.better_thermostat import calibration as calibration_module
from custom_components.better_thermostat import config_flow as config_flow_module
from custom_components.better_thermostat.utils import controlling as controlling_module
from custom_components.better_thermostat.utils.const import (
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
)

_RUNTIME_MODULES = (calibration_module, controlling_module)


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
