"""Make the repo root importable so ``shared``, ``gateway``, ``pipeline`` and
``engines`` resolve in tests the same way they do inside the containers.

Without this, pytest inserts the *first non-package parent* of a test file, which
differs per suite: ``gateway/tests`` gets the root (gateway is a package) but
``engines/breeze/tests`` would get ``engines/`` and lose ``shared``.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
