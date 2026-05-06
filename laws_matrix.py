"""Calcule les matrices de redondance et de conflit entre les 20 lois métier.

Énumère un échantillon stratifié de PhotoStates, applique les 20 lois sur chaque,
et calcule pour chaque paire (Li, Lj) :
- **Redondance** : % de cas où Li et Lj donnent la MÊME action sur le même state
- **Conflit** : % de cas où Li et Lj donnent des actions OPPOSÉES (ex: include vs exclude)

Output :
- `data/output/laws_audit/audit_results.json` : matrices + exemples par cellule
- `data/output/laws_audit/laws_audit.html` : visualisation interactive

Usage :
    .venv/bin/python laws_matrix.py
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from pathlib import Path

from laws import (
    PhotoState, Decision, LAWS, evaluate_all,
    CATEGORIES, TIMES_OF_DAY, SHOT_TYPES, FACE_VISIBILITY,
)

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "data" / "output" / "laws_audit"


# ============================================================
# Énumération des PhotoStates
# ============================================================

def enumerate_states() -> list[PhotoState]:
    """Génère un échantillon stratifié représentatif (~30 000 états)."""
    states = []
    # Valeurs significatives (réduites pour rester tractable)
    categories = ["piscine", "cabana", "rooftop", "beach", "spa", "bar", "f_and_b",
                  "gym", "chambre", "staff", "interieur_commun", "exterieur",
                  "hero_ext", "piscine_vue_aerienne"]
    times = ["jour", "nuit", "aube_crepuscule"]
    humans = [0, 2, 5]
    qualities = [40, 70]
    dominances = [25, 50, 80]
    shots = ["close_up", "medium", "wide", "drone"]
    faces = ["complete", "partial", "no_face"]
    pillar_scores = [100, 200]
    bonus_flags = [False, True]
    clutter_flags = [False, True]
    issue_amenity_flags = [False, True]
    slot1_flags = [False, True]
    prev_human_flags = [False, True]
    bucket_empty_flags = [False, True]

    for c in categories:
        for tod in times:
            for h in humans:
                for q in qualities:
                    for d in dominances:
                        for st in shots:
                            for fv in faces:
                                for ps in pillar_scores:
                                    for bonus in bonus_flags:
                                        for clutter in clutter_flags:
                                            # Réduction : on ne combine pas tout pour issues/slot/prev/bucket
                                            states.append(PhotoState(
                                                category=c, time_of_day=tod,
                                                human_count=h, hero_quality=q,
                                                amenity_dominance=d, shot_type=st,
                                                face_visibility=fv, pillar_score=ps,
                                                is_bonus_lifestyle=bonus, has_clutter=clutter,
                                                issues_absence_amenity=False,
                                                is_first_slot=False, prev_photo_has_human=True,
                                                bucket_empty=False,
                                            ))
    # On rajoute des états spécifiques pour activer les lois scope=meta partielles
    # (slot 1, alternance, bucket vide, issues absence amenity)
    for c in categories:
        for q in qualities:
            for d in dominances:
                for st in shots:
                    states.append(PhotoState(category=c, hero_quality=q, amenity_dominance=d,
                                             shot_type=st, is_first_slot=True))  # slot 1
        for bucket_empty in [True, False]:
            states.append(PhotoState(category=c, bucket_empty=bucket_empty))
        for issue in [True, False]:
            states.append(PhotoState(category=c, issues_absence_amenity=issue))
    print(f"📊 {len(states)} PhotoStates énumérés")
    return states


# ============================================================
# Définition de redondance et conflit
# ============================================================

# Domaines (pour décider quand 2 lois sont comparables)
DOMAIN_OF_EFFECT = {
    "exclude": "filter",
    "filter_eligibility": "filter",
    "transform_lighting": "transform",
    "transform_clutter": "transform",
    "transform_human": "transform",
    "transform_color": "transform",
    "transform_recompose": "transform",
    "score_modifier": "score",
    "include_bonus": "filter",       # accepte une photo (= filter inverse)
    "cardinality_constraint": "cardinality",
    "generation_authorization": "generation",
    "post_process_check": "post",
    "scope_meta": "meta",
}


def _action_family(action: str) -> str:
    """Retourne la 'famille' d'une action pour comparer des intentions équivalentes.
    Ex: 'exclude_chambre' et 'hard_exclude_chambre' → famille 'exclude_chambre'.
    """
    # Normaliser : enlever préfixes 'hard_', 'force_', etc.
    a = action
    for pref in ("hard_", "force_", "skip_"):
        if a.startswith(pref):
            a = a[len(pref):]
            break
    return a


def is_redundant(d1: Decision, d2: Decision) -> bool:
    """Deux décisions sont redondantes si même domaine ET même *intention* effective.

    On compare les familles d'actions (ex: 'exclude_chambre' == 'hard_exclude_chambre')
    et non les actions exactes — sinon on rate les vraies redondances entre lois et filets.
    """
    if not (d1 and d2):
        return False
    if DOMAIN_OF_EFFECT.get(d1.effect_type) != DOMAIN_OF_EFFECT.get(d2.effect_type):
        return False
    if d1.action == d2.action:
        return True
    # Familles d'actions normalisées
    if _action_family(d1.action) == _action_family(d2.action):
        return True
    # Cas spécial : toute action contenant 'exclude_X' avec même X est redondante
    if "exclude" in d1.action and "exclude" in d2.action:
        # Extraire le sujet (ex: chambre, staff)
        for keyword in ("chambre", "staff", "lifestyle", "drone_human", "urban_banal", "closeup_portrait"):
            if keyword in d1.action and keyword in d2.action:
                return True
    return False


def is_conflicting(d1: Decision, d2: Decision) -> bool:
    """Deux décisions sont en conflit si elles produisent des décisions OPPOSÉES
    sur le même PhotoState. On ne compte PAS comme conflit :
    - score_modifier + exclude (= superposition, pas contradiction)
    - 2 transformations différentes (= chaînage possible)
    - exclude + transform (= la transform sera no-op, pas un conflit pur)

    Vrai conflit :
    - exclude vs include_bonus (la photo est-elle dans le pack ?)
    - exclude vs filter_eligibility=accept (la photo est-elle éligible ?)
    - filter_eligibility accept vs reject (sur le même slot)
    - generation_authorization allowed vs forbidden (peut-on générer ?)
    """
    if not (d1 and d2):
        return False

    # Cas 1 : exclude vs include_bonus → CONFLIT (la photo est dans le pack ou pas ?)
    if (d1.effect_type == "exclude" and d2.effect_type == "include_bonus") or \
       (d2.effect_type == "exclude" and d1.effect_type == "include_bonus"):
        return True

    def is_accept(d: Decision) -> bool:
        return d.effect_type == "filter_eligibility" and "accept" in d.action

    def is_reject(d: Decision) -> bool:
        return d.effect_type == "filter_eligibility" and "reject" in d.action

    # Cas 2 : exclude vs filter_eligibility=accept → conflit (l'une exclut, l'autre accepte)
    if (d1.effect_type == "exclude" and is_accept(d2)) or (d2.effect_type == "exclude" and is_accept(d1)):
        return True

    # Cas 3 : 2 filter_eligibility avec actions opposées (accept vs reject sur même slot)
    dom1 = DOMAIN_OF_EFFECT.get(d1.effect_type)
    dom2 = DOMAIN_OF_EFFECT.get(d2.effect_type)
    if dom1 == "filter" and dom2 == "filter":
        if (is_accept(d1) and is_reject(d2)) or (is_reject(d1) and is_accept(d2)):
            return True

    # Cas 4 : 2 generation_authorization, allowed vs forbidden
    if d1.effect_type == "generation_authorization" and d2.effect_type == "generation_authorization":
        if ("allowed" in d1.action and "forbidden" in d2.action) or \
           ("forbidden" in d1.action and "allowed" in d2.action):
            return True

    return False


# ============================================================
# Calcul des matrices
# ============================================================

def state_to_dict(s: PhotoState) -> dict:
    """Convertit un PhotoState en dict compact (pour exemple JSON)."""
    return {
        "category": s.category, "time_of_day": s.time_of_day,
        "human_count": s.human_count, "hero_quality": s.hero_quality,
        "amenity_dominance": s.amenity_dominance, "shot_type": s.shot_type,
        "face_visibility": s.face_visibility, "is_bonus_lifestyle": s.is_bonus_lifestyle,
        "has_clutter": s.has_clutter, "pillar_score": s.pillar_score,
        "issues_absence_amenity": s.issues_absence_amenity,
        "is_first_slot": s.is_first_slot, "prev_photo_has_human": s.prev_photo_has_human,
        "bucket_empty": s.bucket_empty,
    }


def compute_matrices(states: list[PhotoState]) -> dict:
    """Calcule les matrices redondance/conflit + exemples par cellule."""
    n = len(LAWS)
    law_ids = [law.id for law in LAWS]

    # Pour chaque paire (i, j), on accumule :
    # - both: # cas où les 2 sont triggered
    # - either: # cas où au moins une est triggered
    # - redundant: # cas redondants
    # - conflict: # cas en conflit
    # - examples_redundancy: jusqu'à 3 PhotoStates exemples redondants
    # - examples_conflict: jusqu'à 3 PhotoStates exemples en conflit

    counters = {(li, lj): {"both": 0, "either": 0, "redundant": 0, "conflict": 0,
                           "examples_redundancy": [], "examples_conflict": []}
                for li in law_ids for lj in law_ids}

    # Trigger counts par loi
    trigger_count = {lid: 0 for lid in law_ids}

    for state in states:
        decisions = evaluate_all(state)
        for lid, d in decisions.items():
            if d is not None:
                trigger_count[lid] += 1

        # Pour chaque paire ordonnée (li, lj) avec li < lj
        for i in range(n):
            for j in range(i + 1, n):
                li, lj = law_ids[i], law_ids[j]
                d1, d2 = decisions[li], decisions[lj]
                key = (li, lj)
                key_rev = (lj, li)

                if d1 is None and d2 is None:
                    continue
                # Au moins une triggered
                counters[key]["either"] += 1
                counters[key_rev]["either"] += 1
                if d1 is not None and d2 is not None:
                    counters[key]["both"] += 1
                    counters[key_rev]["both"] += 1
                    if is_redundant(d1, d2):
                        counters[key]["redundant"] += 1
                        counters[key_rev]["redundant"] += 1
                        if len(counters[key]["examples_redundancy"]) < 3:
                            ex = {"state": state_to_dict(state),
                                  "d1": {"action": d1.action, "reason": d1.reason},
                                  "d2": {"action": d2.action, "reason": d2.reason}}
                            counters[key]["examples_redundancy"].append(ex)
                            counters[key_rev]["examples_redundancy"].append(
                                {"state": ex["state"], "d1": ex["d2"], "d2": ex["d1"]})
                    if is_conflicting(d1, d2):
                        counters[key]["conflict"] += 1
                        counters[key_rev]["conflict"] += 1
                        if len(counters[key]["examples_conflict"]) < 3:
                            ex = {"state": state_to_dict(state),
                                  "d1": {"action": d1.action, "reason": d1.reason,
                                         "effect": d1.effect_type},
                                  "d2": {"action": d2.action, "reason": d2.reason,
                                         "effect": d2.effect_type}}
                            counters[key]["examples_conflict"].append(ex)
                            counters[key_rev]["examples_conflict"].append(
                                {"state": ex["state"], "d1": ex["d2"], "d2": ex["d1"]})

    # Calcule les pourcentages
    redundancy_matrix = {}
    conflict_matrix = {}
    cell_details = {}

    for li in law_ids:
        redundancy_matrix[li] = {}
        conflict_matrix[li] = {}
        for lj in law_ids:
            if li == lj:
                redundancy_matrix[li][lj] = 100.0  # diagonale
                conflict_matrix[li][lj] = 0.0
                continue
            c = counters[(li, lj)]
            either = c["either"]
            both = c["both"]
            redundancy_matrix[li][lj] = round(c["redundant"] / either * 100, 1) if either else 0.0
            conflict_matrix[li][lj] = round(c["conflict"] / both * 100, 1) if both else 0.0
            cell_details[f"{li}_{lj}"] = {
                "either": either, "both": both,
                "redundant": c["redundant"], "conflict": c["conflict"],
                "examples_redundancy": c["examples_redundancy"],
                "examples_conflict": c["examples_conflict"],
            }

    laws_meta = [
        {"id": law.id, "label": law.label, "short_label": law.short_label,
         "family": law.family}
        for law in LAWS
    ]

    return {
        "n_states": len(states),
        "laws": laws_meta,
        "trigger_count": trigger_count,
        "redundancy_matrix": redundancy_matrix,
        "conflict_matrix": conflict_matrix,
        "cell_details": cell_details,
    }


def compute_top_pairs(results: dict, kind: str, top_n: int = 10) -> list[dict]:
    """Retourne les top N paires (i,j) ayant la plus haute valeur de redondance/conflit."""
    matrix = results[f"{kind}_matrix"]
    pairs = []
    seen = set()
    for li, row in matrix.items():
        for lj, val in row.items():
            if li == lj or val == 0:
                continue
            key = tuple(sorted([li, lj]))
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"li": key[0], "lj": key[1], "value": val})
    pairs.sort(key=lambda p: -p["value"])
    return pairs[:top_n]


# ============================================================
# CLI
# ============================================================

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("🔢 Énumération des PhotoStates...")
    states = enumerate_states()

    print(f"⚙️  Calcul des matrices ({len(states)} états × 20 lois)...")
    results = compute_matrices(states)

    print(f"🏆 Top 10 redondances + conflits...")
    results["top_redundancy"] = compute_top_pairs(results, "redundancy", top_n=10)
    results["top_conflict"] = compute_top_pairs(results, "conflict", top_n=10)

    # Save JSON
    audit_path = OUTPUT_DIR / "audit_results.json"
    with open(audit_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"✅ Résultats sauvegardés : {audit_path}")

    # Print quick summary
    print(f"\n📊 Résumé :")
    print(f"   - {results['n_states']} PhotoStates testés")
    print(f"   - Lois activées au moins 1 fois :")
    for lid, count in sorted(results["trigger_count"].items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"     {lid}: {count:,} états")

    print(f"\n🔁 TOP 10 REDONDANCES :")
    for p in results["top_redundancy"]:
        print(f"   {p['li']} ↔ {p['lj']}: {p['value']:.1f}%")
    print(f"\n⚔️  TOP 10 CONFLITS :")
    for p in results["top_conflict"]:
        print(f"   {p['li']} ↔ {p['lj']}: {p['value']:.1f}%")

    # Build HTML
    print(f"\n🎨 Build HTML...")
    build_html(results)
    print(f"✅ HTML généré : {OUTPUT_DIR / 'laws_audit.html'}")


def build_html(results: dict) -> Path:
    """Génère la page HTML interactive depuis les résultats."""
    template_path = ROOT / "templates" / "laws_matrix_template.html"
    template = template_path.read_text()
    html = (template
            .replace("__PAYLOAD__", json.dumps(results, default=str))
            .replace("__N_STATES__", f"{results['n_states']:,}")
            .replace("__N_LAWS__", str(len(results["laws"]))))
    out = OUTPUT_DIR / "laws_audit.html"
    out.write_text(html)
    return out


if __name__ == "__main__":
    main()
