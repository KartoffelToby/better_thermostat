"""Shared numpy type aliases for the MPC v2 internals.

``FloatArray`` is the floating-point ndarray used for every state vector,
covariance and prediction matrix in the controller, so the element type is
pinned once here rather than left as a bare ``np.ndarray`` (dtype ``Any``) at
each site. ``np.floating`` (rather than ``np.float64``) matches the result
type of numpy arithmetic, so it stays precise without forcing dtype casts.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.floating]
"""A floating-point numpy array (any shape, any float precision)."""
