"""Formalisation des 20 lois métier du pipeline (11 lois + 9 filets de sécurité).

Chaque loi est une fonction pure : `PhotoState -> Decision | None`.
- `Decision` si la loi s'active sur ce state (avec son effet)
- `None` si la loi ne s'applique pas

Ce module est utilisé par `laws_matrix.py` pour calculer les matrices
de redondance et de conflit entre toutes les paires de lois.

NE MODIFIE PAS le pipeline existant — c'est uniquement une formalisation
en parallèle pour audit défendable (cf. demande manager Martin).

Référence dans la doc UI : `templates/index.html` sous-onglet "Règles métier"
(lignes 644-790).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional


# ============================================================
# Types
# ============================================================

# Catégories (cf. analyze.py prompt Gemini)
CATEGORIES = [
    "piscine", "cabana", "rooftop", "beach", "spa", "bar", "f_and_b", "gym",
    "chambre", "staff", "interieur_commun", "exterieur", "hero_ext",
    "detail", "transat", "piscine_vue_aerienne",
]

TIMES_OF_DAY = ["jour", "nuit", "aube_crepuscule"]
SHOT_TYPES = ["close_up", "medium", "wide", "drone"]
FACE_VISIBILITY = ["complete", "partial", "no_face"]


@dataclass(frozen=True)
class PhotoState:
    """Représentation minimale d'une photo pour évaluer toutes les lois.

    Toutes les valeurs ont des défauts neutres pour permettre l'instanciation
    avec uniquement les attributs significatifs.
    """
    category: str = "piscine"
    time_of_day: str = "jour"
    human_count: int = 0
    hero_quality: int = 70
    amenity_dominance: int = 50
    shot_type: str = "wide"
    face_visibility: str = "no_face"
    is_bonus_lifestyle: bool = False  # closeup bikini/torse + amenity en fond
    has_clutter: bool = False
    pillar_score: int = 130
    issues_absence_amenity: bool = False
    # Contexte séquentiel / pack
    is_first_slot: bool = False
    prev_photo_has_human: bool = True
    bucket_empty: bool = False  # déclencheur de génération full IA (L8)


# Effets possibles d'une loi sur une photo (= "domain")
EffectType = Literal[
    "exclude",              # photo retirée du pack
    "filter_eligibility",   # photo non-éligible à un slot précis (ex: slot 1)
    "transform_lighting",   # ai_lighting (nuit→jour)
    "transform_clutter",    # ai_remove_clutter
    "transform_human",      # ai_add_character ou ai_remove_people
    "transform_color",      # LUT brand
    "transform_recompose",  # ai_recompose
    "score_modifier",       # multiplie ou bonus le score
    "include_bonus",        # accept en bucket bonus_lifestyle
    "cardinality_constraint",  # contrainte sur le pack (taille, volumes par catégorie)
    "generation_authorization",  # autorise/interdit génération full IA
    "post_process_check",   # validation post-IA, retry/fallback
    "scope_meta",           # ne s'active jamais sur un PhotoState individuel (pack/séquence/post)
]


@dataclass
class Decision:
    """Décision d'une loi sur un PhotoState donné."""
    law_id: str
    effect_type: EffectType
    action: str  # détail effectif, ex: "exclude_chambre", "transform_lighting", "score_x0.45"
    reason: str  # raison lisible
    triggered: bool = True  # toujours True si retourné, mais permet le filtre côté loop


@dataclass
class LawMeta:
    """Métadonnées d'une loi (pour affichage UI)."""
    id: str
    label: str
    short_label: str
    family: str  # "loi" ou "filet"
    color_class: str  # CSS Tailwind (ex: "border-orange-400")
    doc_anchor: str  # ancre dans la doc index.html
    fn: Callable[[PhotoState], Optional[Decision]] = field(default=None)


# ============================================================
# 11 LOIS MÉTIER
# ============================================================

def L1_pack_size(s: PhotoState) -> Optional[Decision]:
    """L1 — Taille du pack (cible 15, min 12, max 18). Scope=pack, jamais par photo."""
    return None  # scope_meta : ne s'active pas sur un state individuel


def L2_slot1(s: PhotoState) -> Optional[Decision]:
    """L2 — Slot 1 : tier strict (pool/cabana/rooftop/beach > spa > bar/food/gym),
    + hard requirements amenity_dominance≥50%, shot_type≠close_up, hero_quality≥50.
    Humain natif non discriminant."""
    if not s.is_first_slot:
        return None
    tier1 = {"piscine", "cabana", "rooftop", "beach"}
    tier2 = {"spa"}
    tier3 = {"bar", "f_and_b", "gym"}
    if s.category not in (tier1 | tier2 | tier3):
        return Decision("L2", "filter_eligibility", "slot1_reject_category",
                        f"L2: catégorie {s.category} non éligible slot 1")
    if s.amenity_dominance < 50:
        return Decision("L2", "filter_eligibility", "slot1_reject_dominance",
                        f"L2: dominance {s.amenity_dominance}% < 50% requis pour slot 1")
    if s.shot_type == "close_up":
        return Decision("L2", "filter_eligibility", "slot1_reject_closeup",
                        "L2: shot_type=close_up interdit en slot 1")
    if s.hero_quality < 50:
        return Decision("L2", "filter_eligibility", "slot1_reject_quality",
                        f"L2: hero_quality {s.hero_quality} < 50 pour slot 1")
    return Decision("L2", "filter_eligibility", "slot1_accept",
                    f"L2: éligible slot 1 (tier {1 if s.category in tier1 else 2 if s.category in tier2 else 3})")


def L3_ajout_humain(s: PhotoState) -> Optional[Decision]:
    """L3 — Ajout humain : alternance 1/2 stricte, slot 1 obligatoire,
    catégories autorisées : pool, cabana, rooftop, beach, exterieur, interieur_commun, gym."""
    allowed_categories = {"piscine", "cabana", "rooftop", "beach", "exterieur",
                          "interieur_commun", "gym"}
    forbidden_categories = {"f_and_b", "chambre", "staff", "piscine_vue_aerienne"}

    if s.is_bonus_lifestyle:
        return Decision("L3", "transform_human", "bonus_lifestyle_no_add",
                        "L3: bonus lifestyle garde son humain natif, pas d'ajout IA")

    if s.human_count > 0:
        return None  # déjà un humain natif → L3 ne déclenche pas d'ajout

    if s.category in forbidden_categories:
        return Decision("L3", "transform_human", "no_add_forbidden_category",
                        f"L3: ajout humain interdit en {s.category}")

    if s.category not in allowed_categories:
        return None  # catégorie hors-scope (detail, hero_ext, etc.)

    # Slot 1 obligatoire OU alternance (prev sans humain → on doit en ajouter)
    if s.is_first_slot:
        return Decision("L3", "transform_human", "add_character_slot1",
                        "L3: ajout humain forcé en slot 1")
    if not s.prev_photo_has_human:
        return Decision("L3", "transform_human", "add_character_alternance",
                        "L3: ajout humain pour alternance (prev sans humain)")
    return None  # alternance OK, pas d'ajout obligatoire


def L4_exclusions_absolues(s: PhotoState) -> Optional[Decision]:
    """L4 — Exclusions absolues : chambre, staff, décapité, close-up portrait,
    drone+humain, F&B+IA implicite, immeuble urbain banal."""
    if s.category == "chambre":
        return Decision("L4", "exclude", "exclude_chambre",
                        "L4: chambre exclue (Day Pass n'inclut pas la nuitée)")
    if s.category == "staff":
        return Decision("L4", "exclude", "exclude_staff",
                        "L4: staff exclu (non aspirationnel)")
    if s.face_visibility != "complete" and s.human_count > 0:
        return Decision("L4", "exclude", "exclude_face_incomplete",
                        f"L4: face_visibility={s.face_visibility} avec humain → décapité")
    # Close-up portrait sur amenity (lifestyle déguisé)
    if (s.shot_type == "close_up" and s.human_count > 0
            and s.face_visibility == "complete"
            and s.category in {"piscine", "beach", "rooftop", "spa"}
            and not s.is_bonus_lifestyle):
        return Decision("L4", "exclude", "exclude_closeup_portrait_amenity",
                        "L4: close-up portrait sur amenity = lifestyle déguisé")
    # Vue aérienne avec humain plein cadre
    if s.category == "piscine_vue_aerienne" and s.human_count > 0:
        return Decision("L4", "exclude", "exclude_drone_human",
                        "L4: vue aérienne avec humain plein cadre")
    # Immeuble urbain banal
    if s.category == "hero_ext" and (s.pillar_score < 130 or s.issues_absence_amenity):
        return Decision("L4", "exclude", "exclude_urban_banal",
                        f"L4: hero_ext pillar={s.pillar_score} → immeuble urbain banal")
    return None


def L5_lighting_transform(s: PhotoState) -> Optional[Decision]:
    """L5 — Photos nuit/sombre → forcées en jour via ai_lighting + bonus +40."""
    if s.time_of_day in ("nuit", "aube_crepuscule"):
        return Decision("L5", "transform_lighting", "force_to_day",
                        f"L5: time_of_day={s.time_of_day} → ai_lighting (jour ensoleillé)")
    return None


def L6_clutter_removal(s: PhotoState) -> Optional[Decision]:
    """L6 — Clutter removal IA (objets, eyesores techniques, logos tiers).
    Règle ADDITION-FREE."""
    if s.has_clutter:
        return Decision("L6", "transform_clutter", "remove_clutter",
                        "L6: ai_remove_clutter (ADDITION-FREE)")
    return None


def L7_volumes_categorie(s: PhotoState) -> Optional[Decision]:
    """L7 — Volumes par catégorie (pool 3-4, food max 1, etc.). Scope=pack."""
    return None  # scope_meta : contraintes globales sur le pack


def L8_generation_full_ia(s: PhotoState) -> Optional[Decision]:
    """L8 — Génération photo 100% IA si bucket vide.
    Spa/Bar autorisés, autres INTERDITS."""
    if not s.bucket_empty:
        return None
    allowed_for_gen = {"spa", "bar"}
    forbidden_for_gen = {"f_and_b", "piscine", "cabana", "rooftop", "beach", "gym"}
    if s.category in allowed_for_gen:
        return Decision("L8", "generation_authorization", "gen_allowed",
                        f"L8: génération autorisée pour {s.category}")
    if s.category in forbidden_for_gen:
        return Decision("L8", "generation_authorization", "gen_forbidden",
                        f"L8: génération INTERDITE pour {s.category}")
    return None


def L9_bonus_lifestyle(s: PhotoState) -> Optional[Decision]:
    """L9 — Bonus lifestyle : photos focus humain (close-up bikini, etc.) avec
    face_visibility=complete, max 2/hôtel en queue de pack."""
    if s.is_bonus_lifestyle and s.face_visibility == "complete":
        return Decision("L9", "include_bonus", "bonus_lifestyle_accept",
                        "L9: photo bonus lifestyle acceptée (queue de pack)")
    return None


def L10_validation_post_ia(s: PhotoState) -> Optional[Decision]:
    """L10 — Validation post-IA + retry/fallback. Scope=output (post-traitement)."""
    return None  # scope_meta : appliqué après génération IA


def L11_lut_brand(s: PhotoState) -> Optional[Decision]:
    """L11 — LUT brand Dayuse (homogénéité). Appliqué à TOUTES les photos finales."""
    return Decision("L11", "transform_color", "apply_lut_brand",
                    "L11: LUT brand appliquée (post-traitement systématique)")


# ============================================================
# 9 FILETS DE SÉCURITÉ
# ============================================================

def F1_hard_exclude_room_staff(s: PhotoState) -> Optional[Decision]:
    """F1 — Hard exclude chambre/staff dès le mapping (jamais dans aucun bucket)."""
    if s.category in ("chambre", "staff"):
        return Decision("F1", "exclude", f"hard_exclude_{s.category}",
                        f"F1: hard exclude {s.category} dès le mapping")
    return None


def F2_dedup_vlm_jour_nuit(s: PhotoState) -> Optional[Decision]:
    """F2 — Pré-filtre dédup VLM (jour/nuit différents → garde les 2). Scope=paire."""
    return None  # scope_meta : agit sur paires, pas state individuel


def F3_verifier_amenity(s: PhotoState) -> Optional[Decision]:
    """F3 — Verifier amenity 2e passe top-3 buckets → corrige hallucinations Gemini.
    Si amenity_dominance ≤ 30, on plombe ; si confirmée, on conserve."""
    # Représentation simplifiée : si la dominance est faible (<= 30), F3 corrige
    if s.amenity_dominance <= 30 and s.category in {"piscine", "cabana", "rooftop", "spa"}:
        return Decision("F3", "score_modifier", "dominance_corrected_down",
                        f"F3: dominance {s.amenity_dominance}% non confirmée → plombée")
    return None


def F4_heuristique_anti_lifestyle(s: PhotoState) -> Optional[Decision]:
    """F4 — Heuristique Python anti-lifestyle : closeup bikini/torse/portrait OU
    drone+humain → score ×0.45 + dominance plombée à 20%."""
    has_human = s.human_count > 0
    is_closeup_with_amenity = (
        s.shot_type == "close_up" and has_human
        and s.face_visibility == "complete"
        and s.category in {"piscine", "beach", "rooftop", "spa"}
    )
    is_drone_human = s.category == "piscine_vue_aerienne" and has_human
    if is_closeup_with_amenity or is_drone_human:
        return Decision("F4", "score_modifier", "lifestyle_penalty_x0.45",
                        "F4: anti-lifestyle ×0.45 + dominance plombée à 20%")
    return None


def F5_slot1_hard_requirements(s: PhotoState) -> Optional[Decision]:
    """F5 — Slot 1 hard requirements : dominance ≥50%, shot_type ≠close_up, hero_quality ≥50.
    (Doublon avec L2 — la matrice doit le révéler.)"""
    if not s.is_first_slot:
        return None
    if s.amenity_dominance < 50 or s.shot_type == "close_up" or s.hero_quality < 50:
        return Decision("F5", "filter_eligibility", "slot1_reject_hard_requirements",
                        f"F5: slot1 hard reqs failed (dom={s.amenity_dominance}, shot={s.shot_type}, q={s.hero_quality})")
    return Decision("F5", "filter_eligibility", "slot1_accept_hard_requirements",
                    "F5: slot1 hard requirements OK")


def F6_smart_crop_skip(s: PhotoState) -> Optional[Decision]:
    """F6 — Smart crop skip si humain visible → évite couper la tête."""
    if s.human_count > 0 and s.face_visibility == "complete":
        return Decision("F6", "transform_recompose", "skip_crop_protect_human",
                        "F6: smart crop skip (humain plein-visible)")
    return None


def F7_validation_post_ia(s: PhotoState) -> Optional[Decision]:
    """F7 — Validation post-IA (invented_furniture, scene_regenerated, etc.). Scope=output."""
    return None  # scope_meta : agit après génération


def F8_whitelist_par_action(s: PhotoState) -> Optional[Decision]:
    """F8 — Whitelist par action (ai_lighting accepte scene_regenerated, etc.). Scope=output."""
    return None  # scope_meta : agit après génération


def F9_lut_brand_systematique(s: PhotoState) -> Optional[Decision]:
    """F9 — LUT brand appliquée à toutes (= L11)."""
    return Decision("F9", "transform_color", "apply_lut_brand",
                    "F9: LUT brand appliquée systématiquement")


# ============================================================
# Registre des lois (ordre = ordre d'affichage matrice)
# ============================================================

LAWS: list[LawMeta] = [
    # Lois métier
    LawMeta("L1", "Taille du pack (12-18, cible 15)", "Pack size",
            "loi", "border-orange-400", "subtab-rules", L1_pack_size),
    LawMeta("L2", "Slot 1 (tier + hard requirements)", "Slot 1",
            "loi", "border-orange-400", "subtab-rules", L2_slot1),
    LawMeta("L3", "Ajout humain (alternance 1/2)", "Ajout humain",
            "loi", "border-orange-400", "subtab-rules", L3_ajout_humain),
    LawMeta("L4", "Exclusions absolues", "Exclusions",
            "loi", "border-red-500", "subtab-rules", L4_exclusions_absolues),
    LawMeta("L5", "Photos nuit → forcées jour (ai_lighting)", "Lighting",
            "loi", "border-blue-400", "subtab-rules", L5_lighting_transform),
    LawMeta("L6", "Clutter removal (ADDITION-FREE)", "Clutter",
            "loi", "border-emerald-500", "subtab-rules", L6_clutter_removal),
    LawMeta("L7", "Volumes par catégorie", "Volumes",
            "loi", "border-purple-400", "subtab-rules", L7_volumes_categorie),
    LawMeta("L8", "Génération 100% IA (spa/bar OK)", "Génération IA",
            "loi", "border-fuchsia-500", "subtab-rules", L8_generation_full_ia),
    LawMeta("L9", "Bonus lifestyle (max 2)", "Bonus lifestyle",
            "loi", "border-fuchsia-500", "subtab-rules", L9_bonus_lifestyle),
    LawMeta("L10", "Validation post-IA + retry/fallback", "Validation post-IA",
            "loi", "border-rose-500", "subtab-rules", L10_validation_post_ia),
    LawMeta("L11", "LUT brand (homogénéité)", "LUT brand",
            "loi", "border-amber-500", "subtab-rules", L11_lut_brand),
    # Filets de sécurité
    LawMeta("F1", "Hard exclude chambre/staff", "Hard exclude",
            "filet", "border-red-300", "subtab-rules", F1_hard_exclude_room_staff),
    LawMeta("F2", "Pré-filtre dédup VLM jour/nuit", "Dedup VLM",
            "filet", "border-blue-300", "subtab-rules", F2_dedup_vlm_jour_nuit),
    LawMeta("F3", "Verifier amenity 2e passe", "Verifier amenity",
            "filet", "border-emerald-300", "subtab-rules", F3_verifier_amenity),
    LawMeta("F4", "Heuristique anti-lifestyle (×0.45)", "Anti-lifestyle",
            "filet", "border-pink-300", "subtab-rules", F4_heuristique_anti_lifestyle),
    LawMeta("F5", "Slot 1 hard requirements", "Slot1 reqs",
            "filet", "border-orange-300", "subtab-rules", F5_slot1_hard_requirements),
    LawMeta("F6", "Smart crop skip (humain plein-visible)", "Crop skip",
            "filet", "border-purple-300", "subtab-rules", F6_smart_crop_skip),
    LawMeta("F7", "Validation post-IA", "Post-IA check",
            "filet", "border-rose-300", "subtab-rules", F7_validation_post_ia),
    LawMeta("F8", "Whitelist par action", "Whitelist",
            "filet", "border-rose-300", "subtab-rules", F8_whitelist_par_action),
    LawMeta("F9", "LUT brand systématique", "LUT brand sys",
            "filet", "border-amber-300", "subtab-rules", F9_lut_brand_systematique),
]


def all_laws() -> list[LawMeta]:
    """Retourne la liste de toutes les lois (11 + 9 = 20)."""
    return LAWS


def evaluate_all(state: PhotoState) -> dict[str, Optional[Decision]]:
    """Applique les 20 lois sur un PhotoState, retourne {law_id: Decision|None}."""
    return {law.id: law.fn(state) for law in LAWS}


# ============================================================
# Self-test rapide
# ============================================================

if __name__ == "__main__":
    # Test sur un PhotoState exemple
    test_state = PhotoState(
        category="piscine",
        time_of_day="nuit",
        human_count=0,
        is_first_slot=True,
        has_clutter=True,
    )
    print(f"Test sur : {test_state}\n")
    for law in LAWS:
        d = law.fn(test_state)
        if d:
            print(f"  ✅ {law.id} ({law.short_label:<22}) → {d.action:<35} | {d.reason}")
        else:
            print(f"  ⚪ {law.id} ({law.short_label:<22}) — non déclenchée")
