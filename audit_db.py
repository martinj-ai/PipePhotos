"""Audit DB (SQLite) — persiste les résultats de validation + tags humains.

Permet de :
- Mesurer la précision du validateur (Gemini) vs vérité terrain (tag humain)
- Tracer l'évolution du taux de succès dans le temps
- Identifier les modes de failure récurrents par catégorie/hôtel
- Faire de l'A/B test rigoureux des changements de prompt

Schéma :
- `photo_results` : 1 row par photo retouchée (auto-écrit après chaque pipeline)
- `photo_tags` : tag humain ✅⚠️❌ optionnel par photo

Storage : `data/audit.db` (SQLite, single-file, pas de serveur).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "audit.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    filename TEXT NOT NULL,
    action TEXT,                    -- ai_add_character / ai_lighting / etc.
    category TEXT,                  -- piscine / chambre / rooftop / ...
    vibe TEXT,                      -- Family-Friendly / Luxe / ...
    ai_validation_json TEXT,        -- raw ai_validation dict (JSON)
    critical_fields_json TEXT,      -- raw field_checks dict (JSON, 14 champs)
    lois_json TEXT,                 -- raw lois verdicts dict (JSON, 13 lois)
    violations_json TEXT,           -- JSON list of violations strings
    retry_attempted INTEGER DEFAULT 0,  -- bool
    fallback_to_original INTEGER DEFAULT 0,  -- bool
    cost_usd REAL,
    duration_ms INTEGER,
    created_at REAL NOT NULL        -- unix timestamp
);

CREATE INDEX IF NOT EXISTS idx_photo_results_slug ON photo_results(slug);
CREATE INDEX IF NOT EXISTS idx_photo_results_created ON photo_results(created_at);
CREATE INDEX IF NOT EXISTS idx_photo_results_category ON photo_results(category);

CREATE TABLE IF NOT EXISTS photo_tags (
    slug TEXT NOT NULL,
    filename TEXT NOT NULL,
    tag TEXT NOT NULL,              -- 'good' | 'borderline' | 'bad'
    note TEXT,                      -- optional free text
    tagged_at REAL NOT NULL,
    PRIMARY KEY (slug, filename)    -- 1 tag par photo, dernier override
);

CREATE INDEX IF NOT EXISTS idx_photo_tags_tag ON photo_tags(tag);
CREATE INDEX IF NOT EXISTS idx_photo_tags_tagged ON photo_tags(tagged_at);
"""


@contextmanager
def get_conn():
    """Context manager qui ouvre une conn SQLite avec row factory."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Initialise les tables si elles n'existent pas. Idempotent.

    Inclut les migrations légères (ALTER TABLE ADD COLUMN) pour les colonnes
    ajoutées après le premier déploiement.
    """
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migrations : add colonnes si DB ancienne (idempotent via try/except)
        for col_sql in [
            "ALTER TABLE photo_results ADD COLUMN lois_json TEXT",  # P1, Martin 15/05/2026
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # colonne existe déjà


# Auto-init au import du module
init_db()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Save / read photo_results
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def save_photo_result(slug: str, filename: str, entry: dict) -> None:
    """Persiste le résultat d'une photo retouchée après pipeline.

    Appelé après que enhance_one() ait retourné, depuis app.py.
    `entry` est l'objet sérialisé envoyé au front (avec action, ai_validation, etc.).
    """
    ai_val = entry.get("ai_validation") or {}
    field_checks = ai_val.get("critical_fields") or {}
    lois = ai_val.get("lois") or {}
    violations = ai_val.get("violations") or []
    analysis = entry.get("gemini_analysis") or entry.get("analysis") or {}
    factual = (analysis.get("factual") if isinstance(analysis, dict) else {}) or {}

    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO photo_results
                (slug, filename, action, category, vibe, ai_validation_json,
                 critical_fields_json, lois_json, violations_json, retry_attempted,
                 fallback_to_original, cost_usd, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    slug,
                    filename,
                    entry.get("action") or "unknown",
                    (factual.get("category") if isinstance(factual, dict) else None) or entry.get("category"),
                    entry.get("vibe"),
                    json.dumps(ai_val, ensure_ascii=False, default=str),
                    json.dumps(field_checks, ensure_ascii=False, default=str),
                    json.dumps(lois, ensure_ascii=False, default=str),
                    json.dumps(violations, ensure_ascii=False),
                    int(bool(ai_val.get("retry_attempted"))),
                    int(bool(entry.get("fallback_to_original"))),
                    float(entry.get("cost_usd") or 0),
                    int(entry.get("duration_ms") or 0),
                    time.time(),
                ),
            )
    except Exception as e:
        # Ne JAMAIS bloquer le pipeline pour un échec de log
        print(f"[audit_db] save_photo_result failed for {slug}/{filename}: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tag humain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

VALID_TAGS = {"good", "borderline", "bad"}


def set_photo_tag(slug: str, filename: str, tag: str, note: str | None = None) -> dict:
    """Crée/met à jour le tag humain pour une photo. UPSERT.

    Returns: {ok: bool, slug, filename, tag, tagged_at}
    """
    if tag not in VALID_TAGS:
        return {"ok": False, "error": f"tag invalide '{tag}', attendu {VALID_TAGS}"}
    now = time.time()
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO photo_tags (slug, filename, tag, note, tagged_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(slug, filename) DO UPDATE SET
                     tag = excluded.tag,
                     note = excluded.note,
                     tagged_at = excluded.tagged_at""",
                (slug, filename, tag, note, now),
            )
        return {"ok": True, "slug": slug, "filename": filename, "tag": tag, "tagged_at": now}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def get_photo_tag(slug: str, filename: str) -> dict | None:
    """Récupère le tag actuel d'une photo, ou None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tag, note, tagged_at FROM photo_tags WHERE slug=? AND filename=?",
            (slug, filename),
        ).fetchone()
    if not row:
        return None
    return {"tag": row["tag"], "note": row["note"], "tagged_at": row["tagged_at"]}


def get_all_tags_for_slug(slug: str) -> dict[str, dict]:
    """Récupère tous les tags d'un slug. Returns {filename: {tag, note, tagged_at}}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT filename, tag, note, tagged_at FROM photo_tags WHERE slug=?",
            (slug,),
        ).fetchall()
    return {
        r["filename"]: {"tag": r["tag"], "note": r["note"], "tagged_at": r["tagged_at"]}
        for r in rows
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dashboard stats agrégées
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_dashboard_stats(slug: str | None = None, days: int = 30) -> dict:
    """Retourne toutes les stats agrégées pour le dashboard.

    Args:
        slug : filtre sur 1 hôtel précis, ou None pour tous
        days : fenêtre temporelle (par défaut 30 jours)

    Returns:
        {
          "kpis": { total, good, borderline, bad, untagged, success_rate },
          "by_category": [ {category, total, good_pct, bad_pct}, ... ],
          "by_slug": [ {slug, total, good_pct, bad_pct}, ... ],
          "top_violations": [ {violation, count}, ... ],
          "field_fail_rates": [ {field, fail_count, total, fail_pct}, ... ],
          "validator_precision": { tp, fp, tn, fn, precision, recall, ... },
          "evolution": [ {day, good_pct, total}, ... ],
        }
    """
    cutoff = time.time() - (days * 86400)
    slug_filter = ""
    args: list[Any] = [cutoff]
    if slug:
        slug_filter = "AND pr.slug = ?"
        args.append(slug)

    with get_conn() as conn:
        # ━━ KPIs globaux ━━
        rows = conn.execute(
            f"""SELECT pr.slug, pr.filename, pr.violations_json, pr.critical_fields_json,
                       pr.lois_json, pr.category, pr.action, pr.retry_attempted,
                       pr.fallback_to_original, pr.cost_usd, pr.created_at, pt.tag, pt.tagged_at
                FROM photo_results pr
                LEFT JOIN photo_tags pt ON pt.slug = pr.slug AND pt.filename = pr.filename
                WHERE pr.created_at >= ? {slug_filter}
                ORDER BY pr.created_at DESC""",
            args,
        ).fetchall()

    # Dédoublonnage : garde la dernière entry par (slug, filename)
    seen: set[tuple[str, str]] = set()
    unique_rows = []
    for r in rows:
        key = (r["slug"], r["filename"])
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(r)

    total = len(unique_rows)
    # ━━ Double tracking fallback (Martin 15/05/2026) ━━
    # Les fallback_to_original sont comptés AUTOMATIQUEMENT et exclus du calcul
    # de "% succès". Logique : la photo finale = originale propre (donc OK pour
    # publication), mais le PIPELINE IA a échoué — c'est ce qu'on veut mesurer
    # séparément sans demander à Martin de tagger manuellement.
    fallback_rows = [r for r in unique_rows if r["fallback_to_original"]]
    fallback_count = len(fallback_rows)
    fallback_cost_usd = round(sum((r["cost_usd"] or 0) for r in fallback_rows), 4)
    # Les "taggables" = total moins les fallback (que Martin n'a pas à tagger)
    taggable_rows = [r for r in unique_rows if not r["fallback_to_original"]]

    tagged = [r for r in taggable_rows if r["tag"]]
    good_count = sum(1 for r in tagged if r["tag"] == "good")
    border_count = sum(1 for r in tagged if r["tag"] == "borderline")
    bad_count = sum(1 for r in tagged if r["tag"] == "bad")
    untagged = len(taggable_rows) - len(tagged)
    # Success rate basé sur les taguées non-borderline ET hors fallback
    judged_non_border = [r for r in tagged if r["tag"] != "borderline"]
    good_for_rate = sum(1 for r in judged_non_border if r["tag"] == "good")
    success_rate = (good_for_rate / len(judged_non_border) * 100) if judged_non_border else 0.0

    kpis = {
        "total": total,
        "taggable": len(taggable_rows),       # = total - fallback
        "tagged": len(tagged),
        "untagged": untagged,                  # photos taggables non-taguées (à tagger)
        "good": good_count,
        "borderline": border_count,
        "bad": bad_count,
        "fallback_auto": fallback_count,       # ✨ comptabilisé AUTOMATIQUEMENT
        "fallback_cost_usd": fallback_cost_usd,
        "success_rate": round(success_rate, 1),
    }

    # ━━ By category ━━
    cat_stats: dict[str, dict] = {}
    for r in unique_rows:
        cat = r["category"] or "unknown"
        if cat not in cat_stats:
            cat_stats[cat] = {"total": 0, "good": 0, "borderline": 0, "bad": 0, "untagged": 0}
        cat_stats[cat]["total"] += 1
        if r["tag"]:
            cat_stats[cat][r["tag"]] += 1
        else:
            cat_stats[cat]["untagged"] += 1
    by_category = [
        {
            "category": cat,
            **stats,
            "good_pct": round(stats["good"] / max(1, stats["total"] - stats["untagged"]) * 100, 1) if (stats["total"] - stats["untagged"]) else 0,
            "bad_pct": round(stats["bad"] / max(1, stats["total"] - stats["untagged"]) * 100, 1) if (stats["total"] - stats["untagged"]) else 0,
        }
        for cat, stats in sorted(cat_stats.items(), key=lambda x: -x[1]["total"])
    ]

    # ━━ By slug ━━
    slug_stats: dict[str, dict] = {}
    for r in unique_rows:
        s = r["slug"]
        if s not in slug_stats:
            slug_stats[s] = {"total": 0, "good": 0, "borderline": 0, "bad": 0, "untagged": 0}
        slug_stats[s]["total"] += 1
        if r["tag"]:
            slug_stats[s][r["tag"]] += 1
        else:
            slug_stats[s]["untagged"] += 1
    by_slug = [
        {
            "slug": s,
            **stats,
            "good_pct": round(stats["good"] / max(1, stats["total"] - stats["untagged"]) * 100, 1) if (stats["total"] - stats["untagged"]) else 0,
            "bad_pct": round(stats["bad"] / max(1, stats["total"] - stats["untagged"]) * 100, 1) if (stats["total"] - stats["untagged"]) else 0,
        }
        for s, stats in sorted(slug_stats.items(), key=lambda x: -x[1]["total"])
    ]

    # ━━ Top violations ━━
    violation_counts: dict[str, int] = {}
    for r in unique_rows:
        try:
            vs = json.loads(r["violations_json"] or "[]")
        except Exception:
            vs = []
        for v in vs:
            violation_counts[v] = violation_counts.get(v, 0) + 1
    top_violations = [
        {"violation": v, "count": c}
        for v, c in sorted(violation_counts.items(), key=lambda x: -x[1])
    ][:15]

    # ━━ Field fail rates (critical fields validator) ━━
    field_stats: dict[str, dict] = {}
    for r in unique_rows:
        try:
            fc = json.loads(r["critical_fields_json"] or "{}")
        except Exception:
            fc = {}
        for fname, fdata in fc.items():
            if not isinstance(fdata, dict):
                continue
            status = (fdata.get("status") or "").upper()
            if fname not in field_stats:
                field_stats[fname] = {"total": 0, "fail": 0, "pass": 0, "na": 0}
            field_stats[fname]["total"] += 1
            if status == "FAIL":
                field_stats[fname]["fail"] += 1
            elif status == "PASS":
                field_stats[fname]["pass"] += 1
            elif status == "N/A":
                field_stats[fname]["na"] += 1
    field_fail_rates = [
        {
            "field": fname,
            "total": s["total"],
            "fail": s["fail"],
            "pass": s["pass"],
            "na": s["na"],
            "fail_pct": round(s["fail"] / max(1, s["total"] - s["na"]) * 100, 1) if (s["total"] - s["na"]) else 0,
        }
        for fname, s in sorted(field_stats.items(), key=lambda x: -x[1]["fail"])
    ]

    # ━━ Validator precision/recall vs tags humains ━━
    # Validator says OK ↔ ai_validation.violations is empty
    # Validator says FAIL ↔ ai_validation.violations not empty
    # Human truth : tag = 'good' → OK; tag = 'bad' → not OK; 'borderline' → exclu
    # ⚠️ Fallback exclus de la matrice (la photo finale = originale, donc validator
    # passe naturellement → si Martin tag 'bad', ce serait un faux signal car le
    # validator a CORRECTEMENT déclenché le fallback. Comptés séparément.)
    tp = fp = tn = fn = 0
    for r in tagged:
        if r["tag"] == "borderline":
            continue
        if r["fallback_to_original"]:
            continue  # voir comment au-dessus
        try:
            vs = json.loads(r["violations_json"] or "[]")
        except Exception:
            vs = []
        validator_ok = len(vs) == 0
        human_ok = r["tag"] == "good"
        if validator_ok and human_ok:
            tn += 1   # vrai négatif (= validator dit OK, humain confirme OK)
        elif validator_ok and not human_ok:
            fn += 1   # faux négatif (= validator a manqué une erreur)
        elif not validator_ok and human_ok:
            fp += 1   # faux positif (= validator a flag à tort)
        elif not validator_ok and not human_ok:
            tp += 1   # vrai positif (= validator a bien flag)
    total_judged = tp + fp + tn + fn
    precision = (tp / max(1, tp + fp)) if (tp + fp) else 0
    recall = (tp / max(1, tp + fn)) if (tp + fn) else 0
    f1 = (2 * precision * recall / max(1e-9, precision + recall)) if (precision + recall) else 0
    validator_precision = {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "total_judged": total_judged,
        "precision": round(precision * 100, 1),
        "recall": round(recall * 100, 1),
        "f1": round(f1 * 100, 1),
    }

    # ━━ LOI verdicts agrégés (Martin 15/05/2026, P1) ━━
    # Pour chaque LOI, on compte les PASS / FAIL / N/A sur l'ensemble du window.
    loi_stats: dict[str, dict] = {}
    for r in unique_rows:
        try:
            lois = json.loads(r["lois_json"] or "{}")
        except Exception:
            lois = {}
        verdicts = (lois.get("verdicts") if isinstance(lois, dict) else {}) or {}
        for loi_id, vdict in verdicts.items():
            if not isinstance(vdict, dict):
                continue
            status = (vdict.get("status") or "N/A").upper()
            if loi_id not in loi_stats:
                loi_stats[loi_id] = {
                    "label": vdict.get("label", loi_id),
                    "criticality": vdict.get("criticality", "minor"),
                    "description": vdict.get("description", ""),
                    "total": 0, "pass": 0, "fail": 0, "na": 0,
                }
            loi_stats[loi_id]["total"] += 1
            if status == "PASS":
                loi_stats[loi_id]["pass"] += 1
            elif status == "FAIL":
                loi_stats[loi_id]["fail"] += 1
            elif status == "N/A":
                loi_stats[loi_id]["na"] += 1
    # Tri : criticité (critical → major → minor) puis taux FAIL desc
    criticality_order = {"critical": 0, "major": 1, "minor": 2}
    loi_verdicts_agg = []
    for loi_id, s in loi_stats.items():
        evaluable = s["total"] - s["na"]
        fail_pct = round(s["fail"] / max(1, evaluable) * 100, 1) if evaluable else 0
        loi_verdicts_agg.append({
            "loi_id": loi_id,
            "label": s["label"],
            "criticality": s["criticality"],
            "description": s["description"],
            "total": s["total"],
            "pass": s["pass"],
            "fail": s["fail"],
            "na": s["na"],
            "fail_pct": fail_pct,
            "pass_pct": round(s["pass"] / max(1, evaluable) * 100, 1) if evaluable else 0,
        })
    loi_verdicts_agg.sort(key=lambda x: (criticality_order.get(x["criticality"], 99), -x["fail_pct"]))

    # ━━ Évolution dans le temps (par jour) ━━
    from collections import defaultdict
    daily: dict[str, dict] = defaultdict(lambda: {"total": 0, "good": 0, "bad": 0, "border": 0, "untagged": 0})
    for r in unique_rows:
        day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
        daily[day]["total"] += 1
        if r["tag"] == "good":
            daily[day]["good"] += 1
        elif r["tag"] == "bad":
            daily[day]["bad"] += 1
        elif r["tag"] == "borderline":
            daily[day]["border"] += 1
        else:
            daily[day]["untagged"] += 1
    evolution = [
        {
            "day": day,
            **stats,
            "good_pct": round(stats["good"] / max(1, stats["total"] - stats["untagged"]) * 100, 1) if (stats["total"] - stats["untagged"]) else None,
        }
        for day, stats in sorted(daily.items())
    ]

    return {
        "kpis": kpis,
        "by_category": by_category,
        "by_slug": by_slug,
        "top_violations": top_violations,
        "field_fail_rates": field_fail_rates,
        "loi_verdicts": loi_verdicts_agg,
        "validator_precision": validator_precision,
        "evolution": evolution,
        "window_days": days,
    }
