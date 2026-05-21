"""Audit DB — persiste les résultats de validation + tags humains.

Permet de :
- Mesurer la précision du validateur (Gemini) vs vérité terrain (tag humain)
- Tracer l'évolution du taux de succès dans le temps
- Identifier les modes de failure récurrents par catégorie/hôtel
- Faire de l'A/B test rigoureux des changements de prompt

Backend portable via SQLAlchemy :
- **Local** : SQLite (`data/audit.db`, single-file, pas de serveur)
- **Prod Railway** : Postgres (DSN injecté via env var `DATABASE_URL`)

La même API publique fonctionne sur les 2 backends.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column, Integer, Float, String, Text, Boolean, Index, LargeBinary,
    PrimaryKeyConstraint, ForeignKey, create_engine, select, func, and_, or_, text,
    delete,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DSN — Configurable via env var DATABASE_URL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Railway injecte automatiquement `DATABASE_URL=postgres://...` quand on attache
# l'addon Postgres au projet. En local, on tombe sur SQLite file.
#
# Railway utilise `postgres://` (legacy) que SQLAlchemy 2.x rejette → on
# normalise vers `postgresql://`.
_DB_URL_RAW = os.getenv("DATABASE_URL", "").strip()
if _DB_URL_RAW.startswith("postgres://"):
    _DB_URL_RAW = _DB_URL_RAW.replace("postgres://", "postgresql://", 1)

DB_URL = _DB_URL_RAW or f"sqlite:///{DATA_DIR / 'audit.db'}"
IS_POSTGRES = DB_URL.startswith("postgresql")
IS_SQLITE = DB_URL.startswith("sqlite")

# Engine SQLAlchemy : pool_pre_ping pour Postgres (évite stale connections)
# pour SQLite : connect_args check_same_thread=False (Flask multi-threading)
_engine_kwargs: dict = {"future": True}
if IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_recycle"] = 300

engine = create_engine(DB_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Modèles
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PhotoResult(Base):
    """1 row par photo retouchée par le pipeline (append-only)."""
    __tablename__ = "photo_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(255), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    action = Column(String(64))                # ai_add_character / ai_lighting / etc.
    category = Column(String(64), index=True)  # piscine / chambre / rooftop / ...
    vibe = Column(String(64))                  # Family-Friendly / Luxe / ...
    ai_validation_json = Column(Text)          # raw ai_validation dict (JSON string)
    critical_fields_json = Column(Text)        # raw field_checks dict (14 champs)
    lois_json = Column(Text)                   # raw lois verdicts dict (13 lois)
    violations_json = Column(Text)             # JSON list of violations strings
    retry_attempted = Column(Boolean, default=False)
    fallback_to_original = Column(Boolean, default=False)
    cost_usd = Column(Float)
    duration_ms = Column(Integer)
    created_at = Column(Float, nullable=False, index=True)  # unix timestamp


class PhotoTag(Base):
    """1 row par tag humain ✅⚠️❌ (UPSERT par (slug, filename))."""
    __tablename__ = "photo_tags"

    slug = Column(String(255), nullable=False)
    filename = Column(String(255), nullable=False)
    tag = Column(String(16), nullable=False, index=True)  # 'good' | 'borderline' | 'bad'
    note = Column(Text)
    tagged_at = Column(Float, nullable=False, index=True)

    __table_args__ = (
        PrimaryKeyConstraint("slug", "filename"),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pipeline runs — historique des pipes complets (Martin 20/05/2026)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Permet de naviguer dans le menu "Base" :
#   - liste des hôtels avec leurs pipes (Pipe 1, Pipe 2, Pipe N)
#   - chaque pipe = un snapshot complet (scoring, prompts, LOI, audit, images)
#
# Stratégie storage (sans Volume Railway, Martin 20/05/2026) :
#   - metadata (response_json) → JSONB Postgres / TEXT SQLite, illimité
#   - images binaires → BYTEA Postgres / BLOB SQLite, COMPRESSÉES à 1280px
#     quality 80 (~200-400 KB par image au lieu de 5-10 MB)
#   - limite par défaut : 3 runs en images max par slug (purge auto du plus
#     vieux quand on en insère un 4e). Metadata gardée indéfiniment.

class PipelineRun(Base):
    """1 row par run COMPLET (resume_from=scrape) du pipeline."""
    __tablename__ = "pipeline_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(255), nullable=False, index=True)
    run_number = Column(Integer, nullable=False)  # 1, 2, 3... par slug
    hotel_name = Column(String(255))  # cache pour affichage rapide
    started_at = Column(Float, nullable=False, index=True)
    completed_at = Column(Float)
    duration_s = Column(Float)
    n_photos_uploaded = Column(Integer, default=0)
    n_photos_enhanced = Column(Integer, default=0)
    cost_usd = Column(Float, default=0)
    response_json = Column(Text)  # final_response.json complet (~50-200 KB)
    pool_floats_enabled = Column(Boolean, default=False)
    slowmo_enabled = Column(Boolean, default=False)
    has_slowmo = Column(Boolean, default=False)
    has_images = Column(Boolean, default=True)  # False si images purgées (gestion espace)

    __table_args__ = (
        Index("idx_pipeline_runs_slug_run_number", "slug", "run_number"),
    )


class PipelineFile(Base):
    """1 row par fichier binaire (image ou vidéo) associé à un PipelineRun.

    Compressé avant stockage pour réduire la taille DB. Servi via /api/db/file/<id>.
    """
    __tablename__ = "pipeline_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    slug = Column(String(255), nullable=False, index=True)
    kind = Column(String(32), nullable=False, index=True)  # 'uploads' | 'enhanced' | 'slowmo'
    filename = Column(String(255), nullable=False)
    mime_type = Column(String(64), nullable=False)
    size_bytes = Column(Integer, default=0)
    data = Column(LargeBinary, nullable=False)
    created_at = Column(Float, nullable=False)

    __table_args__ = (
        Index("idx_pipeline_files_run_kind_filename", "run_id", "kind", "filename"),
    )


def init_db():
    """Crée les tables si elles n'existent pas. Idempotent.

    SQLAlchemy create_all gère la migration "add table" automatiquement.
    Pour des changements de schéma plus complexes (renaming, type changes),
    utiliser Alembic plus tard quand le besoin se présentera.
    """
    Base.metadata.create_all(engine)


# Auto-init au import du module
init_db()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Save / read photo_results
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def save_photo_result(slug: str, filename: str, entry: dict) -> None:
    """Persiste le résultat d'une photo retouchée après pipeline.

    Ne JAMAIS bloquer le pipeline pour un échec de log → try/except interne.
    """
    ai_val = entry.get("ai_validation") or {}
    field_checks = ai_val.get("critical_fields") or {}
    lois = ai_val.get("lois") or {}
    violations = ai_val.get("violations") or []
    analysis = entry.get("gemini_analysis") or entry.get("analysis") or {}
    factual = (analysis.get("factual") if isinstance(analysis, dict) else {}) or {}

    try:
        with SessionLocal() as session:
            row = PhotoResult(
                slug=slug,
                filename=filename,
                action=entry.get("action") or "unknown",
                category=(factual.get("category") if isinstance(factual, dict) else None) or entry.get("category"),
                vibe=entry.get("vibe"),
                ai_validation_json=json.dumps(ai_val, ensure_ascii=False, default=str),
                critical_fields_json=json.dumps(field_checks, ensure_ascii=False, default=str),
                lois_json=json.dumps(lois, ensure_ascii=False, default=str),
                violations_json=json.dumps(violations, ensure_ascii=False),
                retry_attempted=bool(ai_val.get("retry_attempted")),
                fallback_to_original=bool(entry.get("fallback_to_original")),
                cost_usd=float(entry.get("cost_usd") or 0),
                duration_ms=int(entry.get("duration_ms") or 0),
                created_at=time.time(),
            )
            session.add(row)
            session.commit()
    except Exception as e:
        print(f"[audit_db] save_photo_result failed for {slug}/{filename}: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tag humain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

VALID_TAGS = {"good", "borderline", "bad"}


def set_photo_tag(slug: str, filename: str, tag: str, note: str | None = None) -> dict:
    """Crée/met à jour le tag humain pour une photo. UPSERT portable SQLite + Postgres."""
    if tag not in VALID_TAGS:
        return {"ok": False, "error": f"tag invalide '{tag}', attendu {VALID_TAGS}"}
    now = time.time()
    try:
        with SessionLocal() as session:
            # Pattern UPSERT portable : tente UPDATE, sinon INSERT.
            # (SQLAlchemy 2.x a `.merge()` mais on évite pour rester compatible)
            existing = session.get(PhotoTag, (slug, filename))
            if existing:
                existing.tag = tag
                existing.note = note
                existing.tagged_at = now
            else:
                session.add(PhotoTag(
                    slug=slug, filename=filename, tag=tag, note=note, tagged_at=now,
                ))
            session.commit()
        return {"ok": True, "slug": slug, "filename": filename, "tag": tag, "tagged_at": now}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def get_photo_tag(slug: str, filename: str) -> dict | None:
    """Récupère le tag actuel d'une photo, ou None."""
    with SessionLocal() as session:
        row = session.get(PhotoTag, (slug, filename))
        if not row:
            return None
        return {"tag": row.tag, "note": row.note, "tagged_at": row.tagged_at}


def get_all_tags_for_slug(slug: str) -> dict[str, dict]:
    """Tous les tags d'un slug, formatés en {filename: {tag, note, tagged_at}}."""
    with SessionLocal() as session:
        rows = session.execute(
            select(PhotoTag).where(PhotoTag.slug == slug)
        ).scalars().all()
    return {
        r.filename: {"tag": r.tag, "note": r.note, "tagged_at": r.tagged_at}
        for r in rows
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dashboard stats agrégées
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_dashboard_stats(slug: str | None = None, days: int = 30) -> dict:
    """Stats agrégées pour le dashboard. Filtres optionnels : slug, days.

    Stratégie : fetch toutes les rows + agrégation Python (pas de SQL natif spécifique).
    → 100% portable SQLite ↔ Postgres sans réécriture.
    """
    cutoff = time.time() - (days * 86400)

    with SessionLocal() as session:
        # SELECT avec LEFT JOIN tags
        stmt = (
            select(
                PhotoResult.slug, PhotoResult.filename,
                PhotoResult.violations_json, PhotoResult.critical_fields_json,
                PhotoResult.lois_json, PhotoResult.category,
                PhotoResult.action, PhotoResult.retry_attempted,
                PhotoResult.fallback_to_original, PhotoResult.cost_usd,
                PhotoResult.created_at,
                PhotoTag.tag, PhotoTag.tagged_at,
            )
            .outerjoin(
                PhotoTag,
                and_(
                    PhotoTag.slug == PhotoResult.slug,
                    PhotoTag.filename == PhotoResult.filename,
                ),
            )
            .where(PhotoResult.created_at >= cutoff)
            .order_by(PhotoResult.created_at.desc())
        )
        if slug:
            stmt = stmt.where(PhotoResult.slug == slug)
        rows = session.execute(stmt).all()

    # Conversion en dicts pour rester compat avec l'agrégation Python actuelle
    rows_dict = [
        {
            "slug": r[0], "filename": r[1],
            "violations_json": r[2], "critical_fields_json": r[3],
            "lois_json": r[4], "category": r[5], "action": r[6],
            "retry_attempted": r[7], "fallback_to_original": r[8],
            "cost_usd": r[9], "created_at": r[10],
            "tag": r[11], "tagged_at": r[12],
        }
        for r in rows
    ]

    # Dédup par (slug, filename) — garde la row la plus récente (déjà ORDER BY created_at DESC)
    seen: set[tuple[str, str]] = set()
    unique_rows = []
    for r in rows_dict:
        key = (r["slug"], r["filename"])
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(r)

    total = len(unique_rows)
    # Double tracking fallback : compté auto, exclu du calcul success_rate
    fallback_rows = [r for r in unique_rows if r["fallback_to_original"]]
    fallback_count = len(fallback_rows)
    fallback_cost_usd = round(sum((r["cost_usd"] or 0) for r in fallback_rows), 4)
    taggable_rows = [r for r in unique_rows if not r["fallback_to_original"]]

    tagged = [r for r in taggable_rows if r["tag"]]
    good_count = sum(1 for r in tagged if r["tag"] == "good")
    border_count = sum(1 for r in tagged if r["tag"] == "borderline")
    bad_count = sum(1 for r in tagged if r["tag"] == "bad")
    untagged = len(taggable_rows) - len(tagged)
    judged_non_border = [r for r in tagged if r["tag"] != "borderline"]
    good_for_rate = sum(1 for r in judged_non_border if r["tag"] == "good")
    success_rate = (good_for_rate / len(judged_non_border) * 100) if judged_non_border else 0.0

    kpis = {
        "total": total,
        "taggable": len(taggable_rows),
        "tagged": len(tagged),
        "untagged": untagged,
        "good": good_count,
        "borderline": border_count,
        "bad": bad_count,
        "fallback_auto": fallback_count,
        "fallback_cost_usd": fallback_cost_usd,
        "success_rate": round(success_rate, 1),
    }

    # By category
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
            "category": cat, **stats,
            "good_pct": round(stats["good"] / max(1, stats["total"] - stats["untagged"]) * 100, 1) if (stats["total"] - stats["untagged"]) else 0,
            "bad_pct": round(stats["bad"] / max(1, stats["total"] - stats["untagged"]) * 100, 1) if (stats["total"] - stats["untagged"]) else 0,
        }
        for cat, stats in sorted(cat_stats.items(), key=lambda x: -x[1]["total"])
    ]

    # By slug
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
            "slug": s, **stats,
            "good_pct": round(stats["good"] / max(1, stats["total"] - stats["untagged"]) * 100, 1) if (stats["total"] - stats["untagged"]) else 0,
            "bad_pct": round(stats["bad"] / max(1, stats["total"] - stats["untagged"]) * 100, 1) if (stats["total"] - stats["untagged"]) else 0,
        }
        for s, stats in sorted(slug_stats.items(), key=lambda x: -x[1]["total"])
    ]

    # Top violations
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

    # Field fail rates
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
            "field": fname, "total": s["total"], "fail": s["fail"],
            "pass": s["pass"], "na": s["na"],
            "fail_pct": round(s["fail"] / max(1, s["total"] - s["na"]) * 100, 1) if (s["total"] - s["na"]) else 0,
        }
        for fname, s in sorted(field_stats.items(), key=lambda x: -x[1]["fail"])
    ]

    # Validator precision/recall vs tags humains
    # Fallback exclus de la matrice (la photo finale = originale, faux signal sinon)
    tp = fp = tn = fn = 0
    for r in tagged:
        if r["tag"] == "borderline":
            continue
        if r["fallback_to_original"]:
            continue
        try:
            vs = json.loads(r["violations_json"] or "[]")
        except Exception:
            vs = []
        validator_ok = len(vs) == 0
        human_ok = r["tag"] == "good"
        if validator_ok and human_ok:
            tn += 1
        elif validator_ok and not human_ok:
            fn += 1
        elif not validator_ok and human_ok:
            fp += 1
        elif not validator_ok and not human_ok:
            tp += 1
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

    # LOI verdicts agrégés
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
    criticality_order = {"critical": 0, "major": 1, "minor": 2}
    loi_verdicts_agg = []
    for loi_id, s in loi_stats.items():
        evaluable = s["total"] - s["na"]
        fail_pct = round(s["fail"] / max(1, evaluable) * 100, 1) if evaluable else 0
        loi_verdicts_agg.append({
            "loi_id": loi_id, "label": s["label"], "criticality": s["criticality"],
            "description": s["description"], "total": s["total"],
            "pass": s["pass"], "fail": s["fail"], "na": s["na"],
            "fail_pct": fail_pct,
            "pass_pct": round(s["pass"] / max(1, evaluable) * 100, 1) if evaluable else 0,
        })
    loi_verdicts_agg.sort(key=lambda x: (criticality_order.get(x["criticality"], 99), -x["fail_pct"]))

    # Évolution dans le temps (par jour)
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
            "day": day, **stats,
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
        "backend": "postgresql" if IS_POSTGRES else "sqlite",
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Rétrocompat : `get_conn()` legacy context manager (sqlite3 raw)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cleanup à faire un jour : les scripts/tests qui appellent encore audit_db.get_conn()
# devraient passer par SessionLocal(). Pour l'instant on garde l'alias en mode
# DEPRECATED warning pour éviter de casser des chemins existants.

from contextlib import contextmanager


@contextmanager
def get_conn():
    """DEPRECATED — utiliser SessionLocal() à la place.

    Fournit un context manager qui émule l'interface sqlite3 minimale
    (execute / executescript) pour rétrocompat avec le code legacy.
    """
    conn = engine.raw_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pipeline runs API — persist / list / fetch / delete (Martin 20/05/2026)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MAX_RUNS_WITH_IMAGES_PER_SLUG = int(os.getenv("MAX_RUNS_WITH_IMAGES_PER_SLUG", "3"))
IMAGE_COMPRESSION_MAX_WIDTH = int(os.getenv("PIPELINE_IMAGE_MAX_WIDTH", "1280"))
IMAGE_COMPRESSION_QUALITY = int(os.getenv("PIPELINE_IMAGE_QUALITY", "80"))


def _compress_image_for_storage(input_path: Path) -> tuple[bytes, str]:
    """Compresse une image pour stockage DB : redimensionne à 1280px max, JPEG quality 80.

    Returns: (compressed_bytes, mime_type). En cas d'échec, retourne le binaire brut + best-guess MIME.
    """
    try:
        from PIL import Image
        img = Image.open(input_path)
        # Convert to RGB pour JPEG (gère le PNG transparent → JPEG en aplatissant sur blanc)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # Resize si plus grand que max_width
        if img.width > IMAGE_COMPRESSION_MAX_WIDTH:
            ratio = IMAGE_COMPRESSION_MAX_WIDTH / img.width
            new_h = int(img.height * ratio)
            img = img.resize((IMAGE_COMPRESSION_MAX_WIDTH, new_h), Image.LANCZOS)
        # Encode JPEG
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=IMAGE_COMPRESSION_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[audit_db] compress failed for {input_path}: {e} — falling back to raw bytes")
        return input_path.read_bytes(), "image/jpeg"


def _store_file(session: Session, run_id: int, slug: str, kind: str,
                filename: str, file_path: Path) -> dict | None:
    """Lit un fichier disque + compresse (images) + insère en BD. Returns dict {id, size, mime} ou None."""
    if not file_path.exists():
        return None
    try:
        suffix = file_path.suffix.lower()
        if suffix in (".jpg", ".jpeg", ".png", ".webp"):
            data, mime = _compress_image_for_storage(file_path)
        elif suffix == ".mp4":
            data = file_path.read_bytes()
            mime = "video/mp4"
        else:
            data = file_path.read_bytes()
            mime = "application/octet-stream"
        row = PipelineFile(
            run_id=run_id, slug=slug, kind=kind, filename=filename,
            mime_type=mime, size_bytes=len(data), data=data, created_at=time.time(),
        )
        session.add(row)
        session.flush()  # pour récupérer l'id
        return {"id": row.id, "size": len(data), "mime": mime}
    except Exception as e:
        print(f"[audit_db] _store_file failed for {file_path}: {e}")
        return None


def _purge_old_runs_with_images(session: Session, slug: str, keep: int = MAX_RUNS_WITH_IMAGES_PER_SLUG) -> int:
    """Garde les `keep` runs les plus récents AVEC images pour ce slug. Purge les images des autres.
    Metadata des runs est conservée (juste has_images=False + delete des PipelineFile).
    Returns: nb de runs purgés.
    """
    runs = session.execute(
        select(PipelineRun).where(
            PipelineRun.slug == slug,
            PipelineRun.has_images == True,
        ).order_by(PipelineRun.started_at.desc())
    ).scalars().all()
    if len(runs) <= keep:
        return 0
    to_purge = runs[keep:]
    n_purged = 0
    for r in to_purge:
        session.execute(delete(PipelineFile).where(PipelineFile.run_id == r.id))
        r.has_images = False
        n_purged += 1
    return n_purged


def save_pipeline_run(
    slug: str,
    response_data: dict,
    uploads_dir: Path,
    enhanced_dir: Path,
    slowmo_path: Path | None = None,
    pool_floats_enabled: bool = False,
    slowmo_enabled: bool = False,
) -> dict:
    """Persiste un run complet en DB : metadata (response_json) + images compressées.

    Args:
        slug : hôtel slug
        response_data : le dict response_json complet (final_response.json)
        uploads_dir : Path vers data/uploads/<slug>/ pour récupérer les "avant"
        enhanced_dir : Path vers data/output/<slug>/enhanced/ pour les "après"
        slowmo_path : Path optionnel vers le mp4 slowmo loop
        pool_floats_enabled : Si bouées étaient activées (pour affichage)
        slowmo_enabled : Si slowmo était demandé

    Returns:
        dict {ok: bool, run_id, run_number, n_files, error?}
    """
    try:
        hotel = response_data.get("hotel") or {}
        hotel_name = hotel.get("name") or slug
        stats = response_data.get("stats") or {}
        cost = response_data.get("cost") or {}
        enhanced_list = response_data.get("enhanced") or []
        slowmo_meta = response_data.get("slowmo")

        with SessionLocal() as session:
            # Auto-increment run_number par slug
            max_n = session.execute(
                select(func.max(PipelineRun.run_number)).where(PipelineRun.slug == slug)
            ).scalar() or 0
            run_number = max_n + 1

            run = PipelineRun(
                slug=slug,
                run_number=run_number,
                hotel_name=hotel_name,
                started_at=float(response_data.get("pipeline_started_at") or time.time()),
                completed_at=float(response_data.get("pipeline_completed_at") or time.time()),
                duration_s=float(response_data.get("pipeline_duration_s") or 0),
                n_photos_uploaded=int(stats.get("uploaded") or 0),
                n_photos_enhanced=int(stats.get("enhanced") or 0),
                cost_usd=float(cost.get("total_usd") or 0),
                response_json=json.dumps(response_data, ensure_ascii=False, default=str),
                pool_floats_enabled=bool(pool_floats_enabled),
                slowmo_enabled=bool(slowmo_enabled),
                has_slowmo=bool(slowmo_meta and slowmo_meta.get("video_url")),
                has_images=True,
            )
            session.add(run)
            session.flush()  # pour récupérer run.id
            run_id = run.id

            # Persist les fichiers (uploads + enhanced + slowmo)
            n_files = 0
            for entry in enhanced_list:
                fn = entry.get("filename")
                if not fn:
                    continue
                # uploads (avant)
                up_path = uploads_dir / fn
                if up_path.exists():
                    if _store_file(session, run_id, slug, "uploads", fn, up_path):
                        n_files += 1
                # enhanced (après)
                enh_path = enhanced_dir / fn
                if enh_path.exists():
                    if _store_file(session, run_id, slug, "enhanced", fn, enh_path):
                        n_files += 1

            # Slowmo mp4
            if slowmo_path and slowmo_path.exists():
                if _store_file(session, run_id, slug, "slowmo", slowmo_path.name, slowmo_path):
                    n_files += 1

            # Purge des anciens runs avec images (garde MAX_RUNS_WITH_IMAGES_PER_SLUG max)
            n_purged = _purge_old_runs_with_images(session, slug, keep=MAX_RUNS_WITH_IMAGES_PER_SLUG)

            session.commit()
            print(f"[audit_db] save_pipeline_run OK : slug={slug} run_number={run_number} n_files={n_files} n_purged={n_purged}")
            return {"ok": True, "run_id": run_id, "run_number": run_number, "n_files": n_files, "n_purged": n_purged}

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[audit_db] save_pipeline_run FAILED for {slug}: {e}")
        return {"ok": False, "error": str(e)}


def list_hotels_with_runs() -> list[dict]:
    """Liste tous les hôtels qui ont au moins 1 PipelineRun, avec metadata agrégée."""
    try:
        with SessionLocal() as session:
            stmt = (
                select(
                    PipelineRun.slug,
                    func.max(PipelineRun.hotel_name).label("name"),
                    func.count(PipelineRun.id).label("n_runs"),
                    func.max(PipelineRun.started_at).label("last_run_at"),
                    func.sum(func.cast(PipelineRun.has_images, Integer)).label("n_runs_with_images"),
                )
                .group_by(PipelineRun.slug)
                .order_by(func.max(PipelineRun.started_at).desc())
            )
            rows = session.execute(stmt).all()
            return [
                {
                    "slug": r[0],
                    "name": r[1] or r[0],
                    "n_runs": int(r[2] or 0),
                    "last_run_at": float(r[3] or 0),
                    "n_runs_with_images": int(r[4] or 0),
                }
                for r in rows
            ]
    except Exception as e:
        print(f"[audit_db] list_hotels_with_runs failed: {e}")
        return []


def list_runs_for_slug(slug: str) -> list[dict]:
    """Liste tous les runs d'un hôtel (sans le response_json complet, juste metadata)."""
    try:
        with SessionLocal() as session:
            rows = session.execute(
                select(PipelineRun)
                .where(PipelineRun.slug == slug)
                .order_by(PipelineRun.run_number.desc())
            ).scalars().all()
            return [
                {
                    "run_id": r.id,
                    "run_number": r.run_number,
                    "hotel_name": r.hotel_name,
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                    "duration_s": r.duration_s,
                    "n_photos_uploaded": r.n_photos_uploaded,
                    "n_photos_enhanced": r.n_photos_enhanced,
                    "cost_usd": r.cost_usd,
                    "pool_floats_enabled": r.pool_floats_enabled,
                    "slowmo_enabled": r.slowmo_enabled,
                    "has_slowmo": r.has_slowmo,
                    "has_images": r.has_images,
                }
                for r in rows
            ]
    except Exception as e:
        print(f"[audit_db] list_runs_for_slug failed for {slug}: {e}")
        return []


def get_pipeline_run(run_id: int) -> dict | None:
    """Récupère le détail complet d'un run : metadata + response_json parsé + map des files.

    Returns: dict avec keys : run_id, slug, run_number, ..., response (dict), files (list).
    """
    try:
        with SessionLocal() as session:
            r = session.get(PipelineRun, run_id)
            if not r:
                return None
            files = session.execute(
                select(PipelineFile.id, PipelineFile.kind, PipelineFile.filename, PipelineFile.mime_type, PipelineFile.size_bytes)
                .where(PipelineFile.run_id == run_id)
            ).all()
            response_parsed = None
            try:
                response_parsed = json.loads(r.response_json) if r.response_json else {}
            except json.JSONDecodeError:
                response_parsed = {}
            return {
                "run_id": r.id,
                "slug": r.slug,
                "run_number": r.run_number,
                "hotel_name": r.hotel_name,
                "started_at": r.started_at,
                "completed_at": r.completed_at,
                "duration_s": r.duration_s,
                "n_photos_uploaded": r.n_photos_uploaded,
                "n_photos_enhanced": r.n_photos_enhanced,
                "cost_usd": r.cost_usd,
                "pool_floats_enabled": r.pool_floats_enabled,
                "slowmo_enabled": r.slowmo_enabled,
                "has_slowmo": r.has_slowmo,
                "has_images": r.has_images,
                "response": response_parsed,
                "files": [
                    {
                        "id": f[0], "kind": f[1], "filename": f[2],
                        "mime_type": f[3], "size_bytes": f[4],
                    }
                    for f in files
                ],
            }
    except Exception as e:
        print(f"[audit_db] get_pipeline_run failed for {run_id}: {e}")
        return None


def get_pipeline_file_blob(file_id: int) -> tuple[bytes, str] | None:
    """Récupère le binaire + mime d'un PipelineFile pour /api/db/file/<id>."""
    try:
        with SessionLocal() as session:
            r = session.get(PipelineFile, file_id)
            if not r:
                return None
            return r.data, r.mime_type or "application/octet-stream"
    except Exception as e:
        print(f"[audit_db] get_pipeline_file_blob failed for {file_id}: {e}")
        return None


def delete_pipeline_run(run_id: int) -> dict:
    """Supprime un PipelineRun + cascade ses PipelineFile."""
    try:
        with SessionLocal() as session:
            r = session.get(PipelineRun, run_id)
            if not r:
                return {"ok": False, "error": "Run not found"}
            session.execute(delete(PipelineFile).where(PipelineFile.run_id == run_id))
            session.delete(r)
            session.commit()
            return {"ok": True, "run_id": run_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}
