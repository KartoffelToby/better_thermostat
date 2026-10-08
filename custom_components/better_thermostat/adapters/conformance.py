"""Every ecosystem adapter module, typed as the adapter it has to be.

Production loads an adapter lazily through :func:`.delegate.load_adapter`,
which keeps the imports off the event loop and checks only that each
member is present. Binding every adapter module to :class:`.TrvAdapter`
here lets the type checker hold each module's signatures against the
protocol, so an adapter that drifts from it fails the type check rather
than a call on a live device. Nothing in production imports this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from . import deconz, generic, mqtt, shelly, tado, zwave_js
from .types import TrvAdapter

ECOSYSTEM_ADAPTERS: Final[Mapping[str, TrvAdapter]] = {
    "deconz": deconz,
    "generic": generic,
    "mqtt": mqtt,
    "shelly": shelly,
    "tado": tado,
    "zwave_js": zwave_js,
}
