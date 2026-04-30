"""Ordering — décide l'ordre final d'apparition des 12-18 photos sur la fiche.

Règles brand Dayuse (validées Martin) :
1. Photo n°1 = MEILLEURE photo amenity (pool > cabana > rooftop > beach > spa). Jamais intérieur.
2. Alternance avec/sans humain : 1 sur 2 idéalement.
3. Round-robin entre les amenities (pas 3 cabanas à la suite).
4. Intérieurs (detail / interieur_commun) UNIQUEMENT en fin de pack, et seulement si
   pas assez d'amenities/extérieurs pour atteindre la cible.

Usage :
    from ordering import order_final_pack
    ordered = order_final_pack(selected_analyses, target_count_min=12, target_count_max=18)
"""

from __future__ import annotations


# Priorité des amenities pour le slot 1 et l'ordre général
AMENITY_PRIORITY = ["pool", "cabana", "rooftop", "beach", "spa", "bar", "food"]
INTERIOR_TARGETS = {"detail"}  # interieur_commun → mappé sur detail
HERO_EXT_TARGETS = {"hero_ext"}

# ━ Tiers pour le SLOT 1 (Martin : photo de couverture = WOW amenity, jamais bar/food) ━
# Tier 1 = "hero amenities" : ce qu'on attend en couverture d'une fiche Day Pass
SLOT1_TIER_1 = {"pool", "cabana", "rooftop", "beach"}
SLOT1_TIER_2 = {"spa"}
SLOT1_TIER_3 = {"bar", "food"}  # exclus du slot 1 sauf fallback ultime


def _score(analysis: dict) -> int:
    """Score brand global (0-300) — utilisé pour le tri général dans les slots non-slot-1."""
    ps = (analysis.get("emotional") or {}).get("pillar_scores") or {}
    return int(ps.get("freedom", 0) + ps.get("wellness", 0) + ps.get("experience", 0))


def _hero_score(entry: dict) -> int:
    """Score 'wow factor' (0-100) — utilisé EN PRIORITÉ pour choisir le slot 1.
    Si pas dispo, fallback sur score brand."""
    a = entry.get("analysis") or {}
    h = a.get("hero_quality") or {}
    s = h.get("score")
    if isinstance(s, (int, float)):
        return int(s)
    return _score(a) // 3  # fallback : approximation depuis score brand


def _is_slot1_worthy(entry: dict) -> bool:
    """Vrai si Gemini a marqué cette photo comme apte à être slot 1 (hero)."""
    a = entry.get("analysis") or {}
    h = a.get("hero_quality") or {}
    return bool(h.get("is_slot1_worthy"))


def _human_can_be_prominent(entry: dict) -> bool:
    """Vrai si la composition permet d'afficher un humain de façon visible (close_up/medium).
    Pour slot 1 : si on doit AJOUTER un humain IA, on refuse les vues aériennes/larges
    où la personne sera minuscule (cas Grand Beach Hotel piscine_vue_aerienne).
    """
    a = entry.get("analysis") or {}
    shot = a.get("shot_type") or {}
    # Champ explicite Gemini si dispo
    if isinstance(shot.get("human_can_be_prominent"), bool):
        return shot["human_can_be_prominent"]
    shot_type = (shot.get("type") or "").lower()
    if shot_type in ("close_up", "medium"):
        return True
    if shot_type in ("wide", "aerial"):
        return False
    # Fallback heuristique sur la catégorie : piscine_vue_aerienne = aerial
    cat = ((a.get("factual") or {}).get("category") or "").lower()
    if cat in ("piscine_vue_aerienne", "facade"):
        return False
    return True  # par défaut, on assume que c'est ok (pas pénalisé)


def _is_disguised_closeup(entry: dict) -> bool:
    """Heuristique Python identique à coverage._is_lifestyle_disguised_closeup,
    dupliquée ici pour éviter la dépendance circulaire ordering ↔ coverage.

    Détecte les closeups bikini/torse/underwater même si Gemini prétend shot_type=medium.
    """
    a = entry.get("analysis") or {}
    factual = a.get("factual") or {}
    subjects = " ".join(s.lower() for s in (factual.get("subjects") or []))
    human_count = factual.get("human_count") or 0
    presence = (factual.get("human_presence_type") or "").lower()

    if human_count == 0 or presence in ("none", "partial"):
        return False

    BODY_FOCUS = ("maillot de bain", "maillot", "bikini", "swimsuit",
                  "torse", "body", "abdos", "underwater", "sous l'eau",
                  "sous-marin", "submerg", "portrait", "selfie")
    if not any(k in subjects for k in BODY_FOCUS):
        return False

    AMENITY_CONTEXT = ("vue d'ensemble", "vue large", "vue panoramique",
                        "skyline", "rooftop", "horizon", "vue mer")
    if any(k in subjects for k in AMENITY_CONTEXT):
        return False

    return human_count <= 2


def _is_slot1_eligible(entry: dict) -> bool:
    """Hard requirements pour qu'une photo soit candidate slot 1 (Q1+Q2 Martin).

    Règles :
      - L'amenity doit être le sujet principal (dominance >= 50%)
      - Pas un close-up portrait (Gemini OU heuristique Python)
      - Hero quality score >= 50 (pas de photo banale en couverture)
    Photos lifestyle close-up sont exclues par cette règle.
    Si AUCUNE photo ne passe le filtre, on relâche (fallback).
    """
    a = entry.get("analysis") or {}
    factual = a.get("factual") or {}
    dom = (a.get("amenity_dominance") or {}).get("primary_amenity_visible_pct") or 0
    shot = ((a.get("shot_type") or {}).get("type") or "").lower()
    hero = (a.get("hero_quality") or {}).get("score") or 0
    human_count = factual.get("human_count") or 0

    if not isinstance(dom, (int, float)):
        dom = 0
    if not isinstance(hero, (int, float)):
        hero = 0

    # ━ EXCLUSION ABSOLUE : disguised closeup (heuristique Python, indépendante de Gemini) ━
    if _is_disguised_closeup(entry):
        return False

    if dom < 50:
        return False
    # Close-up portrait sur humain (lifestyle Instagram) = exclu
    if shot == "close_up" and human_count > 0:
        return False
    if hero < 50:
        return False
    return True


def _human_count(analysis: dict) -> int:
    return (analysis.get("factual") or {}).get("human_count") or 0


def _has_human(entry: dict) -> bool:
    """Présence humaine narrative (full_visible). Mains/bras seuls = pas humain pour l'alternance."""
    factual = (entry.get("analysis") or {}).get("factual") or {}
    presence = (factual.get("human_presence_type") or "").lower()
    if presence in ("full_visible", "fully visible", "complete"):
        return True
    if presence in ("partial", "none"):
        return False
    # Fallback (champ absent) : compte si humain présent ET visage visible
    return bool(factual.get("human_face_visible")) and (factual.get("human_count") or 0) > 0


def order_final_pack(
    selected: list[dict],
    photo_targets: dict[str, list[dict]],
    target_count_min: int = 12,
    target_count_max: int = 18,
) -> list[dict]:
    """Ordonne les photos sélectionnées selon les règles brand.

    Args:
        selected         : liste des analyses sélectionnées (kept_estimate=True dans coverage)
        photo_targets    : mapping filename → list[target_category] (multi-tagging)
        target_count_min : min total souhaité
        target_count_max : max total souhaité

    Returns:
        Liste ordonnée des entrées (avec un champ ajouté: 'final_order_pos').
    """
    if not selected:
        return []

    by_filename = {e["input"]["filename"]: e for e in selected}

    # 1) Sépare amenities / extérieur / intérieur
    amenity_buckets: dict[str, list[dict]] = {a: [] for a in AMENITY_PRIORITY}
    hero_ext_bucket: list[dict] = []
    interior_bucket: list[dict] = []

    INTERIOR_PRIMARY = {"interieur_commun", "detail", "chambre", "facade"}
    for entry in selected:
        filename = entry["input"]["filename"]
        targets = photo_targets.get(filename, [])
        primary_cat = ((entry.get("analysis") or {}).get("factual") or {}).get("category", "").lower()

        # ━━ Garde-fou : si la catégorie PRIMAIRE Gemini est interior/detail, on la met dans interior_bucket
        # peu importe les multi-tags secondaires. Évite que les photos lounge taggées "pool" en secondaire
        # (parce qu'on voit un coin de piscine au fond) remontent dans le bucket pool. ━━
        if primary_cat in INTERIOR_PRIMARY:
            interior_bucket.append(entry)
            continue

        # Sinon : assignation par priorité amenity → hero_ext → interior
        assigned = None
        for amenity in AMENITY_PRIORITY:
            if amenity in targets:
                amenity_buckets[amenity].append(entry)
                assigned = amenity
                break
        if assigned is None:
            if any(t in HERO_EXT_TARGETS for t in targets):
                hero_ext_bucket.append(entry)
            elif any(t in INTERIOR_TARGETS for t in targets):
                interior_bucket.append(entry)
            else:
                interior_bucket.append(entry)

    # Tri par score brand desc dans chaque bucket
    for k in amenity_buckets:
        amenity_buckets[k].sort(key=lambda e: _score(e.get("analysis") or {}), reverse=True)
    hero_ext_bucket.sort(key=lambda e: _score(e.get("analysis") or {}), reverse=True)
    interior_bucket.sort(key=lambda e: _score(e.get("analysis") or {}), reverse=True)

    ordered: list[dict] = []
    used = set()

    # ---- Slot 1 : photo aspirationnelle AVEC HUMAIN OBLIGATOIRE (règle brand) ----
    # Priorité : amenity + humain narratif + hero_score + worthy
    # Si aucune photo amenity n'a d'humain, on prend la meilleure amenity et on lui ajoutera
    # un personnage IA (l'app.py a la responsabilité de l'inclure dans add_character_filenames).
    all_amenity_candidates = []
    for amenity in AMENITY_PRIORITY:
        for entry in amenity_buckets[amenity]:
            all_amenity_candidates.append((entry, amenity))

    # ━━ SLOT 1 ━━
    # Stratégie en 3 tiers (Martin : la 1ère photo doit être une amenity WOW, pas bar/food) :
    #   Tier 1 = pool/cabana/rooftop/beach
    #   Tier 2 = spa
    #   Tier 3 = bar/food (fallback ultime uniquement si rien d'autre)
    # Dans chaque tier : hard filter (dominance/closeup/hero) puis tri par hero_score.
    # Humain natif n'est pas discriminant (on en ajoute en IA si besoin).

    def _amenity_dominance(entry: dict) -> int:
        a = entry.get("analysis") or {}
        d = (a.get("amenity_dominance") or {}).get("primary_amenity_visible_pct") or 0
        return int(d) if isinstance(d, (int, float)) else 0

    def _slot1_sort_key(t):
        entry = t[0]
        return (
            -_hero_score(entry),                       # WOW factor en premier
            -_amenity_dominance(entry),                # amenity bien visible
            -1 if _is_slot1_worthy(entry) else 0,
            -1 if (_has_human(entry) or _human_can_be_prominent(entry)) else 0,
            -_score(entry.get("analysis") or {}),
        )

    def _pick_slot1_from_tier(tier_amenities: set) -> dict | None:
        """Sélectionne le meilleur slot 1 candidat dans les amenities de ce tier."""
        candidates = [(e, am) for (e, am) in all_amenity_candidates if am in tier_amenities]
        eligible = [(e, am) for (e, am) in candidates if _is_slot1_eligible(e)]
        if eligible:
            eligible.sort(key=_slot1_sort_key)
            return eligible[0][0]
        # Pas d'éligible strict → on relâche les hard requirements pour ce tier
        if candidates:
            candidates.sort(key=_slot1_sort_key)
            return candidates[0][0]
        return None

    slot1 = _pick_slot1_from_tier(SLOT1_TIER_1)
    if not slot1:
        slot1 = _pick_slot1_from_tier(SLOT1_TIER_2)
    if not slot1:
        slot1 = _pick_slot1_from_tier(SLOT1_TIER_3)
    if not slot1 and hero_ext_bucket:
        slot1 = max(hero_ext_bucket, key=_hero_score)
    if not slot1 and interior_bucket:
        slot1 = max(interior_bucket, key=_hero_score)

    if slot1:
        ordered.append(slot1)
        used.add(slot1["input"]["filename"])

    # ---- Slots 2..N : alternance avec/sans humain + round-robin amenities ----
    # Round-robin en 2 phases :
    #   Phase 1 : hero amenities seulement (pool/cabana/rooftop/beach/spa) → on remplit en priorité
    #   Phase 2 : bar/food si pas plein (ils sont moins aspirationnels)
    # Cette logique respecte le souhait Martin : bar/food en queue de pack, pas mélangés au top.
    HERO_AMENITY_ORDER = ["pool", "cabana", "rooftop", "beach", "spa"]
    SUPPLEMENTAL_AMENITY_ORDER = ["bar", "food"]

    target_with_human = (target_count_max // 2)
    placed_with_human = 1 if _has_human(ordered[0].get("analysis") or {}) else 0
    placed_total = len(ordered)

    amenity_round_idx = 0
    consecutive_same_amenity = 0
    last_amenity_used: str | None = None

    def _try_pick(amenity_order: list[str], want_with_human: bool, allow_relax_human: bool):
        """Cherche la meilleure photo dispo dans l'ordre donné. Retourne (entry, amenity) ou None."""
        nonlocal amenity_round_idx
        for i in range(len(amenity_order)):
            amenity = amenity_order[(amenity_round_idx + i) % len(amenity_order)]
            if amenity == last_amenity_used and consecutive_same_amenity >= 1:
                continue
            for entry in amenity_buckets[amenity]:
                if entry["input"]["filename"] in used:
                    continue
                if _has_human(entry) == want_with_human:
                    return (entry, amenity)
        if allow_relax_human:
            for i in range(len(amenity_order)):
                amenity = amenity_order[(amenity_round_idx + i) % len(amenity_order)]
                for entry in amenity_buckets[amenity]:
                    if entry["input"]["filename"] not in used:
                        return (entry, amenity)
        return None

    while placed_total < target_count_max:
        ratio_with = placed_with_human / max(placed_total, 1)
        want_with_human = ratio_with < 0.5

        # Phase 1 : hero amenities (pool/cabana/rooftop/beach/spa)
        chosen = _try_pick(HERO_AMENITY_ORDER, want_with_human, allow_relax_human=False)
        # Phase 1 bis : relâche la contrainte humain si phase 1 ne trouve pas
        if not chosen:
            chosen = _try_pick(HERO_AMENITY_ORDER, want_with_human, allow_relax_human=True)
        # Phase 2 : supplemental (bar/food) seulement si plus rien en phase 1
        if not chosen:
            chosen = _try_pick(SUPPLEMENTAL_AMENITY_ORDER, want_with_human, allow_relax_human=True)

        if chosen:
            entry, amenity = chosen
            ordered.append(entry)
            used.add(entry["input"]["filename"])
            placed_total += 1
            if _has_human(entry):
                placed_with_human += 1
            if amenity == last_amenity_used:
                consecutive_same_amenity += 1
            else:
                consecutive_same_amenity = 0
            last_amenity_used = amenity
            amenity_round_idx = (amenity_round_idx + 1) % len(AMENITY_PRIORITY)
            continue

        # Plus aucune amenity dispo : on injecte hero_ext si on n'a pas atteint le min
        if hero_ext_bucket:
            entry = next((e for e in hero_ext_bucket if e["input"]["filename"] not in used), None)
            if entry:
                ordered.append(entry)
                used.add(entry["input"]["filename"])
                placed_total += 1
                if _has_human(entry):
                    placed_with_human += 1
                continue

        # Si toujours pas atteint le min, on pioche dans intérieur (fallback ultime)
        if placed_total < target_count_min and interior_bucket:
            entry = next((e for e in interior_bucket if e["input"]["filename"] not in used), None)
            if entry:
                ordered.append(entry)
                used.add(entry["input"]["filename"])
                placed_total += 1
                if _has_human(entry):
                    placed_with_human += 1
                continue

        # Plus rien à placer
        break

    # Annote la position finale
    for pos, entry in enumerate(ordered, 1):
        entry["final_order_pos"] = pos

    return ordered
