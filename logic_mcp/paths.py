from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORE_DIR = PROJECT_ROOT / "logic_store"

STORE_DIR.mkdir(parents=True, exist_ok=True)
