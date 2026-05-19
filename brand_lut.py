"""LUT brand Dayuse — applique un ton colorimétrique COHÉRENT à toutes les photos finales.

Objectif : que toutes les photos d'un pack ET tous les packs (tous les hôtels, dans le temps)
aient le même ton brand "soleil de jour ensoleillé Dayuse" — ciel bleu vif, tons chauds golden,
saturation premium-accessible, jamais HDR ni sur-saturé.

LUT ADAPTATIVE (12/05/2026) — Martin a observé que la LUT v2 (warmth R+8% B-8%) virait
sépia/jaune sur les photos DÉJÀ chaudes (lumineux-chaud + aligned-warm). Solution :
3 profils différents qu'on choisit selon `analysis.technical_hints.ambiance` :
  - "soft"   → photo déjà chaude/aligned-warm : on retient le warmth (juste un "pop")
  - "medium" → photo neutre/mixte : warmth modéré
  - "strong" → photo lumineux-froid / off-brand : full bump (correction tonale forte)
La sélection se fait via `pick_profile(analysis)` puis `apply_brand_lut(..., profile=...)`.
"""

from __future__ import annotations

import json
from pathlib import Path
from PIL import Image, ImageEnhance

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config" / "brand_lut.json"

# ━━ LUT BRAND v3 (Martin 19/05/2026, retour équipe Brand) ━━━━━━━━━━━━━━━━━━━━
# Bug v2 : la LUT v2 (warmth_r 1.05-1.10, warmth_b 0.88-0.93) donnait un rendu
# trop jaune / sépia, façon golden hour fin d'après-midi. L'équipe Brand veut un
# look "soleil de midi" : ensoleillé, énergique, blancs blancs, ciel bleu vif,
# couleurs vives — mais PAS de dominante jaune dorée.
#
# Stratégie v3 :
#   - Warmth_r réduit ~40-50% (R*1.01-1.06 au lieu de 1.03-1.10) → moins de push rouge
#   - Warmth_b proche neutre (B*0.93-0.99 au lieu de 0.88-0.96) → moins de pull bleu
#     = ciel bleu préservé, blancs blancs, pas de virage jaune
#   - Saturation augmentée (compense la "richesse perçue" perdue en réduisant le warmth)
#   - Contraste augmenté (le soleil zénithal donne des ombres marquées vs douces de golden hour)
#   - Brightness un poil plus haute (forte exposition midi)
#
# Référence visuelle : photographie de piscine "soleil de midi" = palette saturée
# bleu profond / blanc franc / vert palmiers vif, ombres courtes nettes. À l'opposé :
# golden hour = palette dorée chaude, ombres longues. On part vers midi.

# Paramètres par défaut — utilisés en fallback si `profile` n'est pas fourni.
# On reste sur le profil "medium" pour ne pas casser les call sites existants.
DEFAULT_PARAMS = {
    "saturation": 1.18,
    "contrast": 1.12,
    "brightness": 1.05,
    "warmth_r": 1.03,
    "warmth_b": 0.97,
}

# ━━ 3 profils LUT adaptatifs v3 (soleil de midi) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LUT_PROFILES = {
    "soft": {
        # Photo déjà chaude/aligned-warm : on neutralise quasi-totalement le warmth
        # (la photo est déjà naturellement chaude) et on POSE le pop sat/contrast pour
        # l'effet ensoleillé sans virer sépia.
        "saturation": 1.14,
        "contrast": 1.10,
        "brightness": 1.04,
        "warmth_r": 1.01,  # ← quasi-neutre (était 1.03)
        "warmth_b": 0.99,  # ← neutre (était 0.96)
    },
    "medium": {
        # Photo neutre/mixte : warmth légèrement présent (le soleil de midi a quand
        # même une légère dominante chaude vs lumière studio), mais 2× moins fort que v2.
        "saturation": 1.18,
        "contrast": 1.12,
        "brightness": 1.05,
        "warmth_r": 1.03,  # ← réduit (était 1.05)
        "warmth_b": 0.97,  # ← moins de pull bleu (était 0.93)
    },
    "strong": {
        # Photo lumineux-froid / off-brand / sortie de ai_lighting (Martin 19/05/2026,
        # photo Element Marriott Orlando hero_ext montrée jaune malgré v3) :
        # le profil strong s'applique sur les photos sombre-froid / off-brand → ce sont
        # justement celles où ai_lighting a transformé la lumière. Le combo (ai_lighting
        # qui ajoute du warm + LUT strong v3 R+6%) restait trop jaune. v3.1 : on baisse
        # encore le warmth_r (1.06→1.03) et on remonte le bleu (0.93→0.96). La saturation
        # haute (1.22) garde l'effet ensoleillé sans pousser le jaune.
        "saturation": 1.22,
        "contrast": 1.13,
        "brightness": 1.06,
        "warmth_r": 1.03,  # ← v3.1 baissé (était 1.06 en v3, 1.10 en v2)
        "warmth_b": 0.96,  # ← v3.1 remonté (était 0.93 en v3, 0.88 en v2)
    },
}


def pick_profile(analysis: dict | None) -> str:
    """Choisit le profil LUT (soft/medium/strong) selon l'analyse Gemini Vision.

    Heuristique :
      - 'lumineux-chaud' + 'aligned-warm' → soft (déjà ensoleillée, juste polir)
      - 'lumineux-froid' ou 'off-brand'   → strong (correction tonale forte)
      - 'sombre-*'                         → soft (ai_lighting a déjà transformé en jour;
                                              la LUT ne doit pas re-surchauffer)
      - autres / mixte / unknown          → medium
    """
    if not analysis:
        return "medium"
    hints = analysis.get("technical_hints") or {}
    ambiance = (hints.get("ambiance") or "").lower()
    palette = (hints.get("palette_alignment") or "").lower()
    if ambiance == "lumineux-chaud" and palette == "aligned-warm":
        return "soft"
    if ambiance == "lumineux-froid" or palette == "off-brand":
        return "strong"
    if ambiance.startswith("sombre"):
        return "soft"
    return "medium"


def load_params() -> dict:
    """Lit les paramètres LUT depuis config/brand_lut.json (avec fallback défauts).

    Note : depuis l'introduction des profils adaptatifs, ce JSON n'est plus la source
    primaire — il sert uniquement quand `apply_brand_lut` est appelé sans `profile`.
    On garde la rétrocompat pour les call sites externes éventuels.
    """
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                user = json.load(f)
            return {**DEFAULT_PARAMS, **user}
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_PARAMS.copy()


def apply_brand_lut(
    input_path: Path,
    output_path: Path,
    params: dict | None = None,
    profile: str | None = None,
) -> dict:
    """Applique la LUT brand sur l'image. Déterministe, reproductible.

    Priorité des params :
      1. `profile` ∈ {soft, medium, strong} si fourni → LUT_PROFILES[profile]
      2. `params` explicite si fourni
      3. Fallback : `load_params()` (config/brand_lut.json ou DEFAULT_PARAMS)

    Si input_path == output_path, on travaille en place (lecture + écriture sur le même fichier).
    """
    if profile is not None and profile in LUT_PROFILES:
        params = LUT_PROFILES[profile]
    elif params is None:
        params = load_params()

    img = Image.open(input_path).convert("RGB")

    # 1. Saturation
    img = ImageEnhance.Color(img).enhance(params.get("saturation", 1.12))
    # 2. Contraste
    img = ImageEnhance.Contrast(img).enhance(params.get("contrast", 1.06))
    # 3. Luminosité
    img = ImageEnhance.Brightness(img).enhance(params.get("brightness", 1.02))
    # 4. Warmth (push R, pull B)
    warmth_r = params.get("warmth_r", 1.04)
    warmth_b = params.get("warmth_b", 0.97)
    if warmth_r != 1.0 or warmth_b != 1.0:
        r, g, b = img.split()
        if warmth_r != 1.0:
            r = r.point(lambda v: max(0, min(255, int(v * warmth_r))))
        if warmth_b != 1.0:
            b = b.point(lambda v: max(0, min(255, int(v * warmth_b))))
        img = Image.merge("RGB", (r, g, b))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, quality=92)
    return {"params_applied": params, "profile": profile}
