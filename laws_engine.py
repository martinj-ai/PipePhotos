"""Laws Engine — système de LOI métier structuré pour photo Dayuse.

Inspiré du système LOI du manager pour la vidéo Veo/Kling, adapté au cas photo
Dayuse (préservation décor + ajout sujet éphémère).

Chaque LOI :
- a un ID unique (LOI_P1, LOI_P2, ...)
- a une criticité (critical / major / minor)
- a une description courte
- a un evaluator(field_checks: dict, violations: list) → 'PASS' / 'FAIL' / 'N/A'

L'agrégation des verdicts donne un audit-trail traçable par photo + dashboard.
"""

from __future__ import annotations


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers evaluators (sucre syntactique)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _field_status(fields: dict, fname: str) -> str:
    """Retourne le status d'un field check, ou 'N/A' si absent."""
    f = fields.get(fname) or {}
    return (f.get("status") or "N/A").upper()


def _all_pass(fields: dict, fnames: list[str]) -> str:
    """PASS si TOUS les fields sont PASS (ou N/A). FAIL si AU MOINS UN est FAIL.
    N/A si TOUS sont N/A."""
    statuses = [_field_status(fields, fn) for fn in fnames]
    if any(s == "FAIL" for s in statuses):
        return "FAIL"
    if all(s == "N/A" for s in statuses):
        return "N/A"
    return "PASS"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Définition des LOI Dayuse Photo
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LAWS: dict[str, dict] = {
    # ━━━━ CRITICAL (🔴) — 0% tolérance, retry obligatoire ou fallback original ━
    "LOI_P1_pool_integrity": {
        "criticality": "critical",
        "label": "Pool integrity",
        "description": "La nappe d'eau de la piscine doit rester 100% intacte en forme et superficie",
        "evaluator": lambda f, v: _all_pass(f, ["pool_shape_preserved", "pool_surface_preserved"]),
    },
    "LOI_P2_no_invented_furniture": {
        "criticality": "critical",
        "label": "No invented furniture",
        "description": "Aucun mobilier inventé sous le sujet (transat, daybed, plateforme, coussin)",
        "evaluator": lambda f, v: _field_status(f, "no_invented_support_under_subject"),
    },
    "LOI_P3_subject_count_exact": {
        "criticality": "critical",
        "label": "Subject count exact",
        "description": "Nombre exact de sujets ajoutés = cible du scenario",
        "evaluator": lambda f, v: _field_status(f, "subject_count_added"),
    },
    "LOI_P4_safety_barrier_respected": {
        "criticality": "critical",
        "label": "Safety barrier respected",
        "description": "Sujets toujours du côté SAFE de toute barrière (rooftop / piscine)",
        "evaluator": lambda f, v: _field_status(f, "barrier_side_correct"),
    },
    "LOI_P5_dry_wet_boundary": {
        "criticality": "critical",
        "label": "Dry/wet boundary respected",
        "description": "Sujet sec = pieds sur deck. Sujet wet = piscine intacte. Pas d'extension de deck.",
        "evaluator": lambda f, v: _field_status(f, "subject_water_boundary_respected"),
    },

    # ━━━━ MAJOR (🟠) — Tolérance limitée, retry une fois ━━━━━━━━━━━━━━━━━━━━━
    "LOI_P6_existing_furniture_preserved": {
        "criticality": "major",
        "label": "Existing furniture preserved",
        "description": "Tous les meubles préexistants sont présents au même endroit",
        "evaluator": lambda f, v: _field_status(f, "furniture_existing_preserved"),
    },
    "LOI_P7_decor_preserved": {
        "criticality": "major",
        "label": "Decor preserved",
        "description": "Plantes, lampes, art, décor préservés",
        "evaluator": lambda f, v: _field_status(f, "decor_elements_preserved"),
    },
    "LOI_P8_frame_preserved": {
        "criticality": "major",
        "label": "Frame preserved",
        "description": "Aucun crop / zoom / changement d'angle",
        "evaluator": lambda f, v: _field_status(f, "framing_preserved"),
    },
    "LOI_P9_subject_scale_realistic": {
        "criticality": "major",
        "label": "Subject scale realistic",
        "description": "Sujet à l'échelle réaliste vs mobilier voisin (≈ 2× hauteur lounger)",
        "evaluator": lambda f, v: _field_status(f, "subject_scale_realistic"),
    },
    "LOI_P10_pool_float_realistic": {
        "criticality": "major",
        "label": "Pool float realistic",
        "description": "Bouée taille (< 15% surface eau) et perspective réalistes",
        "evaluator": lambda f, v: _field_status(f, "pool_float_realistic"),
    },

    # ━━━━ MINOR (🟡) — Variance acceptable, warning sans retry ━━━━━━━━━━━━━━━
    "LOI_P11_subject_anatomy_clean": {
        "criticality": "minor",
        "label": "Subject anatomy clean",
        "description": "Anatomie correcte : 2 bras, 5 doigts par main, pas de membre dupliqué",
        "evaluator": lambda f, v: _field_status(f, "subject_anatomy_intact"),
    },
    "LOI_P12_face_photoreal": {
        "criticality": "minor",
        "label": "Face photoreal",
        "description": "Faces photoréalistes (yeux/nez/bouche clairs, pas mannequin)",
        "evaluator": lambda f, v: _field_status(f, "subject_face_photoreal"),
    },
    "LOI_P13_outfit_contextual": {
        "criticality": "minor",
        "label": "Outfit contextual",
        "description": "Tenue adaptée au contexte (swimwear sur piscine, etc.)",
        "evaluator": lambda f, v: _field_status(f, "outfit_appropriate"),
    },
}


CRITICALITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Évaluation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def evaluate_lois(field_checks: dict | None, violations: list | None = None) -> dict:
    """Évalue chaque LOI à partir des field_checks structurés + violations narratives.

    Args:
        field_checks: dict retourné par validate_critical_fields() — {fname: {status, evidence, ...}}
        violations: list de strings de violations narratives (validate_ai_output)

    Returns:
        {
          "verdicts": {loi_id: {status, criticality, label, description}, ...},
          "summary": {
            "total": int,
            "pass": int,
            "fail": int,
            "na": int,
            "critical_fail": int,
            "major_fail": int,
            "minor_fail": int,
            "global_verdict": "PASS" | "FAIL_CRITICAL" | "FAIL_MAJOR" | "FAIL_MINOR_ONLY",
          },
        }
    """
    field_checks = field_checks or {}
    violations = violations or []

    verdicts = {}
    for loi_id, loi_def in LAWS.items():
        try:
            status = loi_def["evaluator"](field_checks, violations)
        except Exception as e:
            status = "N/A"
        verdicts[loi_id] = {
            "status": status,
            "criticality": loi_def["criticality"],
            "label": loi_def["label"],
            "description": loi_def["description"],
        }

    # Summary
    summary = {
        "total": len(verdicts),
        "pass": sum(1 for v in verdicts.values() if v["status"] == "PASS"),
        "fail": sum(1 for v in verdicts.values() if v["status"] == "FAIL"),
        "na": sum(1 for v in verdicts.values() if v["status"] == "N/A"),
        "critical_fail": sum(1 for v in verdicts.values() if v["status"] == "FAIL" and v["criticality"] == "critical"),
        "major_fail": sum(1 for v in verdicts.values() if v["status"] == "FAIL" and v["criticality"] == "major"),
        "minor_fail": sum(1 for v in verdicts.values() if v["status"] == "FAIL" and v["criticality"] == "minor"),
    }

    if summary["critical_fail"] > 0:
        summary["global_verdict"] = "FAIL_CRITICAL"
    elif summary["major_fail"] > 0:
        summary["global_verdict"] = "FAIL_MAJOR"
    elif summary["minor_fail"] > 0:
        summary["global_verdict"] = "FAIL_MINOR_ONLY"
    else:
        summary["global_verdict"] = "PASS"

    return {"verdicts": verdicts, "summary": summary}
