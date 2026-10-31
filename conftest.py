"""Ensure the repository root is importable when pytest is run as a console script.

CI runs `pytest -q` (not `python -m pytest`), so the current working directory is
not automatically added to sys.path. `pytest.ini` also sets `pythonpath = .`;
this file is a belt-and-suspenders fallback for older pytest/config resolution.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
