"""Vercel-compatible ASGI entrypoint.

Keeps the existing `backend/app` package layout while giving the Python runtime a
stable import path under `/api`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.main import app  # noqa: E402

__all__ = ["app"]
