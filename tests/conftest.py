from __future__ import annotations

import sys
from pathlib import Path


# Ensure repository root is importable even when pytest runs in importlib mode.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
