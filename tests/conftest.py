from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import pytest

# Ensure repository root is importable even when pytest runs in importlib mode.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def mock_async_get_translations():
    """Mock Home Assistant's translation fetching across all tests."""
    with patch(
        "homeassistant.helpers.translation.async_get_translations",
        new_callable=AsyncMock,
        return_value={},
    ) as mock_get_translations:
        yield mock_get_translations
