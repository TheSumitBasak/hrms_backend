"""Convenience runner so seeding works from any directory:

    python backend/seed.py            # from the repo root
    python seed.py                    # from backend/

Use ``--reset`` to drop and recreate all tables before seeding.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from app.seed import main  # noqa: E402

if __name__ == "__main__":
    main()
