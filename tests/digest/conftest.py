"""Make the scripts/ python modules importable from the digest tests.

Mirrors tests/pipeline/conftest.py: digest.py and research_phases.py are
stdlib-only by design (the live pipeline runs them under the host python3);
the tests exercise them directly plus the launcher through the bash seam.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
