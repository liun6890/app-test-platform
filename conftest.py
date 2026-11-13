"""Repository-wide pytest path setup.

The frontend test suite imports ``server`` as a top-level module because it
is also run directly from ``frontend``.  When pytest is started at the
repository root, that directory is not otherwise on ``sys.path``.
"""

from __future__ import annotations

import sys
from pathlib import Path


FRONTEND_ROOT = Path(__file__).resolve().parent / "frontend"
if str(FRONTEND_ROOT) not in sys.path:
    sys.path.insert(0, str(FRONTEND_ROOT))
