"""FlowEvo demo case: self-evolving SQL agent over a SQLite database.

Mirrors the ALFWorld adapter layout (env / schemas / skill_library /
compiler / generator / runner) so that experience gathered here can be
ported back into the main framework.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the FlowEvo `src` package importable (runtime.config, runtime.llm_client)
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
