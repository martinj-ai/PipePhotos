"""Progress tracker — état partagé entre worker et endpoint /api/progress.

Stratégie : un fichier JSON par slug dans data/progress/{slug}.json, mis à jour
à chaque step. Le front poll /api/progress?slug=xxx toutes les 500ms.

Volontairement simple : pas de Redis, pas de pub/sub, juste du fs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).parent
PROGRESS_DIR = ROOT / "data" / "progress"
PROGRESS_DIR.mkdir(parents=True, exist_ok=True)

_locks: dict[str, Lock] = {}


def _path(slug: str) -> Path:
    return PROGRESS_DIR / f"{slug}.json"


def _lock_for(slug: str) -> Lock:
    if slug not in _locks:
        _locks[slug] = Lock()
    return _locks[slug]


def _atomic_write(p: Path, state: dict) -> None:
    """Write JSON atomiquement : write to .tmp, puis os.replace (atomic move)."""
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, p)


def init(slug: str, total: int, step: str = "starting") -> None:
    state = {
        "slug": slug,
        "step": step,
        "current": 0,
        "total": total,
        "message": "",
        "started_at": time.time(),
        "updated_at": time.time(),
        "done": False,
        "cost_usd_cumulated": 0.0,
        "input_tokens_cumulated": 0,
        "output_tokens_cumulated": 0,
    }
    with _lock_for(slug):
        _atomic_write(_path(slug), state)


def update(slug: str, **kwargs) -> None:
    """Merge kwargs dans l'état, met à jour updated_at. Atomic write."""
    p = _path(slug)
    if not p.exists():
        return
    with _lock_for(slug):
        try:
            with open(p) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Lecture concurrente avec un write partiel — on skip cette update
            return
        state.update(kwargs)
        state["updated_at"] = time.time()
        _atomic_write(p, state)


def increment(slug: str, current: int | None = None, cost_usd: float = 0,
              input_tokens: int = 0, output_tokens: int = 0, **kwargs) -> None:
    p = _path(slug)
    if not p.exists():
        return
    with _lock_for(slug):
        try:
            with open(p) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if current is not None:
            state["current"] = current
        else:
            state["current"] = state.get("current", 0) + 1
        state["cost_usd_cumulated"] = state.get("cost_usd_cumulated", 0) + cost_usd
        state["input_tokens_cumulated"] = state.get("input_tokens_cumulated", 0) + input_tokens
        state["output_tokens_cumulated"] = state.get("output_tokens_cumulated", 0) + output_tokens
        state.update(kwargs)
        state["updated_at"] = time.time()
        _atomic_write(p, state)


def finish(slug: str, **kwargs) -> None:
    update(slug, done=True, step="done", **kwargs)


def read(slug: str) -> dict | None:
    """Lecture défensive : retourne None si fichier absent ou JSON corrompu (race transitoire)."""
    p = _path(slug)
    if not p.exists():
        return None
    with _lock_for(slug):
        try:
            with open(p) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None  # silently skip — le prochain poll retombera dessus
