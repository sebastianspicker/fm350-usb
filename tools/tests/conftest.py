"""Put ``tools/`` (for fm350_diag) and fm350mac/src (for fm350mac) on
sys.path, so these tests work whether pytest is run from fm350mac/ (its own
venv already has fm350mac installed editable) or from the repo root.
"""

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
_FM350MAC_SRC = _TOOLS_DIR.parent / "fm350mac" / "src"

for _path in (_TOOLS_DIR, _FM350MAC_SRC):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
