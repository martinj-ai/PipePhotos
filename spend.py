"""Tracker de dépenses cumulées (Gemini vision + Nano Banana 2 retouche).

Stockage : data/spend.json — cumul sur TOUS les runs.
Source de vérité : ce qu'on envoie à l'API (calculé via tokens / prix par image).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).parent
SPEND_FILE = ROOT / "data" / "spend.json"
_LOCK = Lock()


def _atomic_write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)


def _read() -> dict:
    if not SPEND_FILE.exists():
        return {
            "total_usd": 0.0,
            "by_op": {},
            "by_run": [],
            "first_run_at": None,
            "last_run_at": None,
        }
    try:
        with open(SPEND_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"total_usd": 0.0, "by_op": {}, "by_run": [], "first_run_at": None, "last_run_at": None}


def add_run(slug: str, breakdown: dict) -> dict:
    """Ajoute un run au cumul.

    Args:
        slug : identifiant hôtel
        breakdown : {analysis_usd: float, enhancement_usd: float,
                     ai_lighting: int, ai_add_character: int, ai_remove_people: int,
                     ai_recompose: int, local_smart_crop: int, local_warm_boost: int}
    """
    with _LOCK:
        data = _read()
        run_total = float(breakdown.get("analysis_usd", 0)) + float(breakdown.get("enhancement_usd", 0))
        data["total_usd"] = round(data.get("total_usd", 0) + run_total, 6)

        by_op = data.setdefault("by_op", {})
        by_op["analysis_usd"] = round(by_op.get("analysis_usd", 0) + breakdown.get("analysis_usd", 0), 6)
        by_op["enhancement_usd"] = round(by_op.get("enhancement_usd", 0) + breakdown.get("enhancement_usd", 0), 6)
        for k in ("ai_lighting", "ai_add_character", "ai_remove_people", "ai_recompose", "local_smart_crop", "local_warm_boost"):
            by_op[k] = by_op.get(k, 0) + int(breakdown.get(k, 0))

        now = time.time()
        if not data.get("first_run_at"):
            data["first_run_at"] = now
        data["last_run_at"] = now

        run_record = {
            "slug": slug,
            "ts": now,
            "total_usd": round(run_total, 6),
            **{k: breakdown.get(k, 0) for k in (
                "analysis_usd", "enhancement_usd", "ai_lighting", "ai_add_character",
                "ai_remove_people", "ai_recompose", "local_smart_crop", "local_warm_boost",
            )},
        }
        runs = data.setdefault("by_run", [])
        runs.append(run_record)
        # On garde uniquement les 100 derniers runs (évite que le fichier explose)
        data["by_run"] = runs[-100:]

        _atomic_write(SPEND_FILE, data)
        return data


def read_summary() -> dict:
    """Retourne le cumul actuel."""
    return _read()
