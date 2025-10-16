from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dateutil import parser as dateparser


STATE_PATH = os.path.join("data", "state.json")


def ensure_dirs(*paths: str) -> None:
    for p in paths:
        os.makedirs(p, exist_ok=True)


def slugify(text: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:120] or "episode"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        ensure_dirs(os.path.dirname(STATE_PATH))
        return {"processed": {}, "episodes": []}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Normalize episode pub_date back to datetime objects
    episodes = data.get("episodes", []) or []
    for ep in episodes:
        pd = ep.get("pub_date")
        if isinstance(pd, str):
            try:
                dtv = dateparser.parse(pd)
                if dtv is not None and dtv.tzinfo is None:
                    dtv = dtv.replace(tzinfo=timezone.utc)
                ep["pub_date"] = dtv or datetime.now(timezone.utc)
            except Exception:
                ep["pub_date"] = datetime.now(timezone.utc)
    data["episodes"] = episodes
    if "processed" not in data:
        data["processed"] = {}
    return data


def _default(o: Any):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def save_state(state: Dict[str, Any]) -> None:
    ensure_dirs(os.path.dirname(STATE_PATH))
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=_default)
