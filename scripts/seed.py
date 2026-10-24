"""Backwards-compatible entry point.

The real implementation lives in :mod:`app.seed`. Run either of:

    python -m scripts.seed
    python -m app.seed
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.seed import main  # noqa: E402

if __name__ == "__main__":
    main()
