from __future__ import annotations

from pathlib import Path
import sys

# Ensure repository root is importable even when pytest runs in importlib mode.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
