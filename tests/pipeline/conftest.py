"""Make the scripts/ python modules importable from the pipeline tests.

research_eval.py and research_phases.py are stdlib-only by design (the live
pipeline runs them under the host python3, which cannot import openjarvis);
the tests exercise them directly in addition to the bash seam.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
