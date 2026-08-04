"""Make scripts/discovery/ importable from the discovery tests.

Mirrors tests/pipeline/conftest.py: the discovery engine is stdlib-only by
design (host python3, no openjarvis import); the tests exercise the modules
directly and the launcher through the bash seam.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "discovery"))
