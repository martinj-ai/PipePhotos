"""Coverage — compare ce qu'on a (analyses Gemini des photos uploadées) à ce qu'il FAUT (shopping list RP).

Sortie :
- couverture par catégorie : combien on a / combien on veut / combien manque ou trop
- alertes : "il manque X photos cabana", "il y a 6 photos pool, on en garde 4"
- shopping list active : ne demande que ce que l'hôtel propose vraiment selon RP

Usage :
    from coverage import compute_coverage
    result = compute_coverage(rp_data, analyses)
"""

from __future__ import annotations


# Min/max de photos finales par catégorie (cible 12-18 photos au total).
# Logique : amenities en priorité, intérieurs UNIQUEMENT en fallback si pas assez d'amenities.
TARGETS = {
    "pool":     {"min": 3, "max": 4, "required_amenity": "pool",    "is_amenity": True},
    "cabana":   {"min": 2, "max": 4, "required_amenity": "cabana",  "is_amenity": True},
    "rooftop":  {"min": 1, "max": 3, "required_amenity": "rooftop", "is_amenity": True},
    "spa":      {"min": 1, "max": 2, "required_amenity": "spa",     "is_amenity": True},
    "beach":    {"min": 1, "max": 3, "required_amenity": "beach",   "is_amenity": True},
    # Martin: bar/food/gym sont moins aspirationnels que pool/cabana/rooftop pour Day Pass.
    # On limite à 1 max chacun dans le pack final.
    "food":     {"min": 1, "max": 1, "required_amenity": "food",    "is_amenity": True},
    "bar":      {"min": 0, "max": 1, "required_amenity": "bar",     "is_amenity": True},
    "gym":      {"min": 0, "max": 1, "required_amenity": "gym",     "is_amenity": True},
    "hero_ext": {"min": 1, "max": 2, "required_amenity": None,      "is_amenity": False},
    "detail":   {"min": 0, "max": 2, "required_amenity": None,      "is_amenity": False},
}


# Mapping des catégories que Gemini retourne dans factual.category → notre catégorie cible
GEMINI_TO_TARGET = {
    "piscine": "pool",
    "piscine_vue_aerienne": "pool",
    "cabana": "cabana",
    "transat": "cabana",
    "rooftop": "rooftop",
    "spa": "spa",
    "gym": "gym",
    "f_and_b": "food",
    "beach": "beach",
    "exterieur": "hero_ext",
    "facade": "hero_ext",
    "interieur_commun": "detail",
    "detail": "detail",
    # chambre → ignoré (Day Access pas de chambre)
    # staff → ignoré
}


def gemini_categories_to_targets(analysis: dict) -> list[str]:
    """Multi-tagging : mappe les catégories Gemini (primary + secondary) vers nos cibles.

    Une photo "rooftop avec piscine" peut renvoyer ['rooftop', 'pool'].
    Permet de compter dans plusieurs buckets de la shopping list.

    🚫 Exception : si primary = chambre / staff → PAS de target (Day Pass n'inclut pas la chambre,
    et les photos staff ne sont jamais des contenus pour la fiche).
    Une chambre avec une vue extérieure superbe reste UNE chambre, on ne l'utilise pas comme hero_ext.
    """
    if not analysis or "factual" not in analysis:
        return []

    factual = analysis["factual"]
    cat_primary = (factual.get("category") or "").lower()

    # Hard exclusion : chambre & staff sont totalement exclus du pack Day Access
    if cat_primary in ("chambre", "staff"):
        return []

    cats_secondary = [c.lower() for c in (factual.get("categories_secondary") or []) if isinstance(c, str)]
    all_cats = [cat_primary] + cats_secondary

    targets: list[str] = []
    subjects = " ".join(factual.get("subjects") or []).lower()
    background = (factual.get("background") or "").lower()
    full_text = f"{subjects} {background}"

    for cat in all_cats:
        if not cat:
            continue
        # F&B : raffinage cocktail vs plat
        if cat == "f_and_b":
            target = "bar" if any(w in subjects for w in ("cocktail", "drink", "verre", "boisson", "wine", "beer", "spritz")) else "food"
        else:
            target = GEMINI_TO_TARGET.get(cat)
        if target and target not in targets:
            targets.append(target)

    # Heuristique de rattrapage : si une piscine est visible dans subjects/background
    # et qu'aucun target pool n'a été trouvé, on en ajoute un
    if "pool" not in targets and any(w in full_text for w in ("piscine", "swimming pool", "pool")):
        targets.append("pool")
    if "rooftop" not in targets and any(w in full_text for w in ("rooftop", "toit", "skyline")):
        targets.append("rooftop")

    return targets


# Compat : ancien nom utilisé ailleurs
def gemini_category_to_target(analysis: dict) -> str | None:
    targets = gemini_categories_to_targets(analysis)
    return targets[0] if targets else None


# Seuil de score brand (sur 300) en-dessous duquel une photo est exclue de la sélection
# Même si elle est "la meilleure" de sa catégorie. Mieux vaut "manque" qu'une photo hors-scope.
MIN_BRAND_SCORE = 80


def _is_transformable(analysis: dict) -> bool:
    """Photo qui peut bénéficier d'une transformation IA (nuit→jour ensoleillé, sombre→lumineux).

    Sert de filet de sécurité quand une catégorie n'a pas atteint son min : on récupère
    les photos transformables même si leur score brut est sous le seuil — elles seront
    ensoleillées par ai_lighting et le score "après transformation" sera bien meilleur.
    """
    if not analysis:
        return False
    factual = analysis.get("factual") or {}
    hints = analysis.get("technical_hints") or {}
    time_of_day = (factual.get("time_of_day") or "").lower()
    ambiance = (hints.get("ambiance") or "").lower()
    palette = (hints.get("palette_alignment") or "").lower()
    return (
        time_of_day in ("nuit", "aube_crepuscule")
        or ambiance.startswith("sombre")
        or ambiance == "lumineux-froid"
        or palette == "off-brand"
    )


# Mots-clés indiquant qu'une issue est nettoyable par ai_remove_clutter (Q3 Martin).
# Si TOUTES les issues d'une photo sont removables, on ne la pénalise pas — au contraire,
# on lui donne un petit bonus, car après clutter step elle sera propre.
REMOVABLE_ISSUE_KEYWORDS = (
    # Clutter classique
    "câble", "cable", "prise", "gobelet", "panneau", "détritus",
    "trash", "wire", "poubelle", "plastic", "fil", "poteau",
    # Objets parasites élargis
    "sceau", "seau", "bucket", "pelle", "spade", "jouet", "toy",
    "ballon", "sandale", "flip-flop", "serviette", "towel",
    "sac", "bag", "extincteur", "barrière", "hose", "tuyau",
    # Éléments structurels effaçables (Q3 — risque accepté)
    "caméra", "camera", "surveillance", "cctv",
    "escalier de secours", "fire escape", "issue de secours",
    "antenne", "antenna", "parabole", "satellite",
    "climatiseur", "ac unit", "air conditioner", "ventilation",
    "vmc", "extracteur", "grille", "gaine",
)


def _is_lifestyle_disguised_closeup(analysis: dict) -> bool:
    """Détecte les photos lifestyle close-up que Gemini classifie à tort (Q+ Martin).

    Gemini hallucine régulièrement sur les closeups bikini/torse :
      - shot_type='medium' alors que c'est un close-up
      - amenity_dominance=60% alors que l'amenity est en arrière-plan
      - is_slot1_worthy=true sur un torse en bikini

    Heuristique indépendante : on regarde les subjects + human_presence_type.
    Si on voit un humain proche en bikini/maillot/torse SANS contexte amenity évident,
    c'est un lifestyle closeup même si Gemini prétend le contraire.
    """
    if not analysis:
        return False
    factual = analysis.get("factual") or {}
    subjects = " ".join(s.lower() for s in (factual.get("subjects") or []))
    human_count = factual.get("human_count") or 0
    presence = (factual.get("human_presence_type") or "").lower()

    # Pas d'humain visible → ce n'est pas un closeup lifestyle
    if human_count == 0 or presence in ("none", "partial"):
        return False

    # Mots-clés indiquant focus humain proche
    BODY_FOCUS_KEYWORDS = (
        "maillot de bain", "maillot", "bikini", "swimsuit",
        "torse", "body", "abdos",
        "underwater", "sous l'eau", "sous-marin", "submerg",
        "portrait", "selfie",
    )
    has_body_focus = any(k in subjects for k in BODY_FOCUS_KEYWORDS)
    if not has_body_focus:
        return False

    # Sujets "amenity context" qui PAR EXCEPTION sauvent (vue d'ensemble piscine + nageur)
    AMENITY_CONTEXT_KEYWORDS = (
        "vue d'ensemble", "vue large", "vue panoramique", "skyline",
        "rooftop", "horizon", "ville", "vue mer",
    )
    has_amenity_context = any(k in subjects for k in AMENITY_CONTEXT_KEYWORDS)
    if has_amenity_context:
        return False

    # Heuristique forte : 1 seul humain (le sujet) + body focus + pas de contexte large
    return human_count <= 2


def _all_issues_are_removable(analysis: dict) -> bool:
    """Vrai si la photo a des issues ET TOUTES les issues sont du clutter nettoyable IA.
    Permet de ne pas pénaliser une photo qui n'a que des défauts effaçables.
    """
    if not analysis:
        return False
    issues = analysis.get("issues") or []
    if not issues:
        return False  # pas d'issue → pas de bonus (déjà parfaite)
    for issue in issues:
        issue_lower = (issue or "").lower()
        if not any(k in issue_lower for k in REMOVABLE_ISSUE_KEYWORDS):
            return False  # une issue non removable → on ne bonus pas
    return True


def compute_score_components(entry: dict) -> dict:
    """Décompose le score d'une photo en composantes lisibles (debug + UI).

    Top-level pour pouvoir l'importer dans app.py et l'exposer sur les photos non sélectionnées.
    """
    a = entry.get("analysis") or {}
    factual = a.get("factual") or {}
    scores = (a.get("emotional") or {}).get("pillar_scores") or {}
    pillar = scores.get("wellness", 0) + scores.get("experience", 0) + scores.get("freedom", 0)

    dominance = (a.get("amenity_dominance") or {}).get("primary_amenity_visible_pct") or 0
    if not isinstance(dominance, (int, float)):
        dominance = 0
    hero = (a.get("hero_quality") or {}).get("score") or 0
    if not isinstance(hero, (int, float)):
        hero = 0
    shot = ((a.get("shot_type") or {}).get("type") or "").lower()
    human_count = factual.get("human_count") or 0
    primary_cat = (factual.get("category") or "").lower()

    # Exemption : pour les catégories food/F&B, le close-up sur assiette/cocktail est légitime.
    # On ne déclenche pas le filtre lifestyle même si shot=close_up + main visible.
    closeup_exempt = primary_cat in ("f_and_b",)

    gemini_says_closeup = (
        not closeup_exempt
        and shot == "close_up" and human_count > 0 and dominance < 30
    )
    python_says_disguised = (not closeup_exempt) and _is_lifestyle_disguised_closeup(a)
    is_lifestyle_closeup = gemini_says_closeup or python_says_disguised

    effective_dominance = dominance
    if python_says_disguised:
        effective_dominance = min(dominance, 20)

    if effective_dominance >= 60:
        dominance_mod = int((effective_dominance - 30) * 0.8)
    elif effective_dominance < 30:
        dominance_mod = -int((30 - effective_dominance) * 1.5)
    else:
        dominance_mod = 0

    hero_bonus = 0 if python_says_disguised else int(hero * 0.3)
    removable_bonus = 30 if _all_issues_are_removable(a) else 0
    transformable_bonus = 40 if _is_transformable(a) else 0

    subtotal = pillar + dominance_mod + hero_bonus + removable_bonus + transformable_bonus

    if is_lifestyle_closeup:
        total = int(subtotal * 0.45)
        lifestyle_penalty = total - subtotal
    else:
        total = subtotal
        lifestyle_penalty = 0

    return {
        "pillar": pillar,
        "dominance_pct": dominance,
        "effective_dominance_pct": effective_dominance,
        "dominance_mod": dominance_mod,
        "hero_score": hero,
        "hero_bonus": hero_bonus,
        "removable_bonus": removable_bonus,
        "transformable_bonus": transformable_bonus,
        "is_lifestyle_closeup": is_lifestyle_closeup,
        "gemini_said_closeup": gemini_says_closeup,
        "python_said_disguised": python_says_disguised,
        "lifestyle_penalty": lifestyle_penalty,
        "shot_type": shot,
        "human_count": human_count,
        "subtotal": subtotal,
        "total": total,
    }


def score_of_entry(entry: dict) -> int:
    """Score brand total d'une photo (utilisable hors compute_coverage)."""
    return compute_score_components(entry)["total"]


def compute_coverage(rp_data: dict, analyses: list[dict]) -> dict:
    """
    Args:
        rp_data : sortie du scraper RP (amenities_normalized, vibe_primary, ...)
        analyses : liste de dicts {filename, analysis: {...}, ...} — sortie de analyze.py

    Returns:
        dict avec :
            shopping_list : catégories à couvrir selon RP
            by_category   : pour chaque cat → {target_min, target_max, found, photos, status, message}
            global        : {total_kept_estimate, total_target, alerts}
    """
    amenities = rp_data.get("amenities_normalized", {})

    # 1) Shopping list active : on ne garde que les catégories pertinentes pour cet hôtel
    active_targets = {}
    for cat, conf in TARGETS.items():
        req = conf["required_amenity"]
        # required_amenity = None → toujours actif (detail, hero_ext)
        if req is None or amenities.get(req):
            active_targets[cat] = conf

    # 2) Bucket les photos par catégories cibles (multi-tagging : 1 photo peut être dans plusieurs buckets)
    buckets: dict[str, list[dict]] = {cat: [] for cat in active_targets}
    unmapped: list[dict] = []
    for entry in analyses:
        targets = gemini_categories_to_targets(entry.get("analysis") or {})
        matched_buckets = [t for t in targets if t in buckets]
        if matched_buckets:
            for t in matched_buckets:
                buckets[t].append(entry)
        else:
            unmapped.append(entry)

    # 3) Sort par score brand décroissant — utilise compute_score_components au top-level
    score_of = score_of_entry

    # Catégories où le close-up est LÉGITIME (pas un lifestyle déguisé) :
    # food = close-up sur assiette/cocktail = format attendu pour vendre la cuisine
    # bar  = close-up sur cocktail/comptoir = pareil
    # Pour ces catégories, on ne penalise pas le close-up.
    CATEGORIES_WHERE_CLOSEUP_IS_OK = {"food", "bar"}

    rejected_low_score: list[dict] = []
    rescued_transformable: list[dict] = []
    for cat in buckets:
        # 3 niveaux de tri dans chaque bucket :
        #   ok : score >= seuil ET PAS un lifestyle closeup → top du bucket
        #   lifestyle_supplemental : closeups bikini/torse/etc. → mis en queue (jamais top-N)
        #   fallback : score sous seuil mais transformable (nuit/sombre)
        #   rejected : score sous seuil et non transformable
        ok: list[dict] = []
        lifestyle_supplemental: list[dict] = []
        fallback: list[dict] = []

        # Pour food/bar, on désactive le filtre close-up (close-up sur assiette = légitime)
        closeup_filter_active = cat not in CATEGORIES_WHERE_CLOSEUP_IS_OK

        for entry in buckets[cat]:
            a = entry.get("analysis") or {}
            score = score_of(entry)
            is_closeup = closeup_filter_active and (
                _is_lifestyle_disguised_closeup(a) or (
                    ((a.get("shot_type") or {}).get("type") or "").lower() == "close_up"
                    and (a.get("factual") or {}).get("human_count", 0) > 0
                )
            )

            if is_closeup:
                # Photos lifestyle close-up : on les garde mais en queue de bucket (jamais top-N)
                lifestyle_supplemental.append(entry)
            elif score >= MIN_BRAND_SCORE:
                ok.append(entry)
            elif _is_transformable(a):
                fallback.append(entry)
            else:
                rejected_low_score.append({
                    "filename": entry["input"]["filename"],
                    "category": cat,
                    "score": score,
                    "reason": "score brand < %d (hors-scope, non transformable)" % MIN_BRAND_SCORE,
                })
        ok.sort(key=score_of, reverse=True)
        lifestyle_supplemental.sort(key=score_of, reverse=True)
        fallback.sort(key=score_of, reverse=True)

        # Si on n'a pas atteint le min de la catégorie, on pioche dans les transformables
        target_min = active_targets.get(cat, {}).get("min", 0)
        if target_min > 0 and len(ok) < target_min and fallback:
            n_rescue = min(target_min - len(ok), len(fallback))
            for e in fallback[:n_rescue]:
                ok.append(e)
                rescued_transformable.append({
                    "filename": e["input"]["filename"],
                    "category": cat,
                    "score": score_of(e),
                    "reason": "rattrapage : catégorie sous-couverte, photo nuit/sombre récupérée (sera transformée par IA)",
                })
            for e in fallback[n_rescue:]:
                rejected_low_score.append({
                    "filename": e["input"]["filename"],
                    "category": cat,
                    "score": score_of(e),
                    "reason": "transformable mais cat. %s déjà couverte au min" % cat,
                })
        else:
            for e in fallback:
                rejected_low_score.append({
                    "filename": e["input"]["filename"],
                    "category": cat,
                    "score": score_of(e),
                    "reason": "score brand < %d (transformable mais non requise)" % MIN_BRAND_SCORE,
                })

        # ━ Lifestyle closeups en QUEUE de bucket : disponibles seulement si pas assez de "vraies" photos ━
        # Si ok a déjà ≥ target_min, les closeups ne sont pas ajoutés au bucket (filtrés out).
        # Si ok est sous-couvert et qu'il n'y a pas de transformable, on pioche dans les closeups en dernier recours.
        if target_min > 0 and len(ok) < target_min and lifestyle_supplemental:
            n_closeup_rescue = min(target_min - len(ok), len(lifestyle_supplemental))
            for e in lifestyle_supplemental[:n_closeup_rescue]:
                ok.append(e)  # ils seront en fin de tri grâce à leur score plombé

        buckets[cat] = ok

    # 4) Calcul couverture par catégorie
    by_category = {}
    alerts = []
    total_kept = 0

    for cat, conf in active_targets.items():
        found = len(buckets[cat])
        keep = min(found, conf["max"])
        total_kept += keep

        if found < conf["min"]:
            status = "missing"
            msg = f"manque {conf['min'] - found} photo(s) {cat} (min {conf['min']})"
            alerts.append({"level": "warning", "category": cat, "message": msg})
        elif found > conf["max"]:
            status = "excess"
            msg = f"{found} photos {cat} disponibles, on garde les {conf['max']} meilleures"
        else:
            status = "ok"
            msg = f"{found} photo(s) {cat} — OK"

        by_category[cat] = {
            "target_min": conf["min"],
            "target_max": conf["max"],
            "found": found,
            "kept_estimate": keep,
            "photos": [
                {
                    "filename": e["input"]["filename"],
                    "score_brand_total": score_of(e),
                    "score_components": compute_score_components(e),
                    "selected_in_top": idx < keep,
                }
                for idx, e in enumerate(buckets[cat])
            ],
            "status": status,
            "message": msg,
        }

    # 5) Photos non-mappées (chambre, staff, catégorie absente du shopping list...)
    unmapped_summary = [
        {"filename": e["input"]["filename"], "category": (e.get("analysis") or {}).get("factual", {}).get("category", "?")}
        for e in unmapped
    ]

    return {
        "hotel_name": rp_data.get("name"),
        "vibe": rp_data.get("vibe_primary"),
        "personas_allowed": rp_data.get("personas_allowed"),
        "shopping_list": list(active_targets.keys()),
        "by_category": by_category,
        "unmapped": unmapped_summary,
        "rejected_low_score": rejected_low_score,
        "rescued_transformable": rescued_transformable,
        "global": {
            "total_kept_estimate": total_kept,
            "total_target_min": sum(c["min"] for c in active_targets.values()),
            "total_target_max": sum(c["max"] for c in active_targets.values()),
            "alerts": alerts,
        },
    }
