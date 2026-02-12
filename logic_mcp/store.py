from __future__ import annotations

import json
import re
import threading
from typing import Dict, List

from .errors import LogicError
from .paths import STORE_DIR


def sanitize_namespace(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name) or "default"


class Store:
    def __init__(self, namespace_id: str):
        self.namespace_id = namespace_id
        self.session_id = sanitize_namespace(namespace_id)
        self.session_dir = STORE_DIR / self.session_id
        self.path = self.session_dir / "session.json"
        self._lock = threading.Lock()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {
                "symbols": {},
                "bundles": {},
                "rules": {},
                "expectations": {},
                "defaults": {},
                "context": {"concepts": {}, "code_bindings": {}},
            }
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {
                "symbols": {},
                "bundles": {},
                "rules": {},
                "expectations": {},
                "defaults": {},
                "context": {"concepts": {}, "code_bindings": {}},
            }

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                # Preserve insertion order; ordering is part of active-set semantics.
                json.dump(self.data, f, indent=2)
            tmp.replace(self.path)

    def _get_items(self, kind: str) -> Dict[str, List[dict]]:
        return self.data.setdefault(kind, {})

    def get_active_items(self, kind: str) -> Dict[str, dict]:
        items = self._get_items(kind)
        active: Dict[str, dict] = {}
        for item_id, versions in items.items():
            for entry in reversed(versions):
                if entry.get("enabled"):
                    active[item_id] = entry
                    break
        return active

    def get_item_versions(self, kind: str, item_id: str) -> List[dict]:
        items = self._get_items(kind)
        return items.get(item_id, [])

    def get_latest_item(self, kind: str, item_id: str) -> dict:
        versions = self.get_item_versions(kind, item_id)
        if not versions:
            raise LogicError("E_UNKNOWN_ID", f"{kind} id does not exist", {"id": item_id})
        return versions[-1]
