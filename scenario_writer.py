"""Scenario Writer — Gemini Vision génère lui-même le scenario d'ajout perso (Martin 13/05/2026).

CONTEXTE
========
Pipeline historique (V1-V4) :
    analyze.py (Gemini Vision) → safe_zones JSON
        ↓
    enhance.py / Python catalog hardcoded (_SCENARIO_CATALOG)
        ↓ choisit scenario fixe basé sur classify(safe_zones[0])
    Nano Banana

Problème structurel : le catalogue Python est AVEUGLE à la photo réelle. Il devine
le scenario via mots-clés (ex: "gym" → yoga, même si la zone est un tapis de course).
D'où les bugs récurrents :
- Yoga sur tapis de course (Moxy gym)
- Transat flottant inventé dans la piscine (Moxy famille)
- Chaise inventée pour caser un humain (Moxy gym detail)

Nouveau pipeline (V5) :
    analyze.py (Gemini Vision) → safe_zones JSON
        ↓
    scenario_writer.py (Gemini Vision RE-soumet la photo) → scenario JSON adapté
        ↓ Gemini Vision a VU la photo, propose le scenario réel
    enhance.py / insère le scenario tel quel + garde-fous globaux
        ↓
    Nano Banana

Coût marginal : ~$0.001 par photo retouchée. Sur 100 photos = $0.10. Négligeable
vs gain de fiabilité estimé +30-50%.

ACTIVATION
==========
Activé par défaut (V5 = nouveau défaut). Pour basculer sur l'ancien catalogue :
    export USE_LEGACY_SCENARIO_CATALOG=1
"""

from __future__ import annotations

import json
import os
import re as _re
import time
from pathlib import Path
from PIL import Image
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

SCENARIO_WRITER_MODEL = "gemini-2.5-flash"

# Prix Gemini 2.5 Flash (cohérent avec analyze.py)
GEMINI_PRICE_INPUT_USD_PER_M = 0.30
GEMINI_PRICE_OUTPUT_USD_PER_M = 2.50

_GENAI_CONFIGURED = False


def _ensure_configured():
    global _GENAI_CONFIGURED
    if not _GENAI_CONFIGURED:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante")
        genai.configure(api_key=api_key)
        _GENAI_CONFIGURED = True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Identités Dayuse (réutilisées depuis enhance.py via duplication pour découplage)
# California influencer aesthetic — décrit dans enhance.py mais inliné ici pour
# que le méta-prompt soit autosuffisant.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_PERSONA_BRIEFS = {
    "solos": "une jeune femme adulte 24-26 ans, look California sun-kissed (peau bronzée, cheveux blonds sun-bleached, élégante naturelle)",
    "couples": "un couple jeune adulte (femme 24-26 + homme 26-28), look California sun-kissed naturel, complices mais non posés",
    "small_groups": "un petit groupe de 2 femmes + 1 homme (24-28 ans), look California sun-kissed, ambiance détendue entre amis",
    "groups": "un groupe de 4 amis (2 femmes + 2 hommes, 24-28 ans), look California sun-kissed, ambiance conviviale",
    "families": "une jeune famille (parents fin 20aine + 1 enfant 6-7 ans), look méditerranéen sun-kissed, vacances naturelles non staged",
}

# Tenues attendues par catégorie de scène
_OUTFIT_HINTS_BY_CATEGORY = {
    "piscine": "swimwear premium tasteful (bikini chic / one-piece moderne / boardshorts), jamais lingerie/transparent",
    "rooftop": "robe slip silk crème ou linen short, chemise linen ouverte, sandales heeled — élégant casual",
    "gym": "athleisure premium (legging haute taille, sports bra ou tank, sneakers training)",
    "interieur_commun": "smart casual (linen / silk slip / oat tones), élégant relaxé",
    "f_and_b": "smart casual cocktail-ready",
    "cabana": "swimwear premium + chapeau de paille + sunglasses",
    "transat": "swimwear premium + chapeau + sunglasses",
    "chambre": "casual confortable (peignoir blanc OK si c'est pour la chambre)",
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Méta-prompt envoyé à Gemini Vision pour qu'il rédige le scenario
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

META_PROMPT_TEMPLATE = """Tu es un directeur artistique expert qui rédige un BRIEF PRÉCIS pour un modèle d'édition d'image IA (Nano Banana / Gemini Image).

🎯 CONTEXTE BRAND : Dayuse, plateforme de réservation hôtel à la journée. Aesthetic = travel-magazine premium (Reformation / Solid&Striped / Aman), candid lifestyle, jamais staged catalog.

📸 PHOTO ANALYSÉE :
- Catégorie : {category}
- Vibe : {vibe}
- Persona cible : {persona} → {persona_brief}
- Tenue typique attendue : {outfit_hint}

🎯 TA MISSION : observer cette photo IDENTIQUE et rédiger un brief de scenario d'ajout d'humain qui RESPECTE :
1. **Aucune invention** : le sujet doit être placé sur une surface/équipement EXISTANT et visible dans la photo
2. **Cohérence sémantique** : la pose doit ÊTRE ADAPTÉE à l'équipement réel (ex: course sur tapis de course, PAS yoga sur tapis de course)
3. **Capacité réaliste** : ne demande JAMAIS plus de sujets que la photo peut accueillir SANS inventer
4. **Pièges spécifiques** : identifie les dangers propres à CETTE photo précise

⚠️ INTERDICTIONS ABSOLUES — tu ne dois JAMAIS recommander de :
- Placer un sujet dans / sur l'eau d'une piscine sur un transat / matelas flottant INVENTÉ (= meuble rigide qui flotte = impossible physiquement)
- Placer un sujet de l'autre côté d'une barrière de sécurité / garde-corps / balustrade
- Demander une posture incohérente avec l'équipement (yoga sur cardio, course sur banc, etc.)
- Inviter à ajouter MOBILIER, plante, déco, ombre, lampe — UNIQUEMENT l'humain et ses items portés (swimwear, chapeau, sunglasses, drink en main)

🚫 INTERDICTIONS DE FRAMING (Martin 13/05/2026 — bug Moxy rooftop trio recadré + plantes disparues) :
- Le scenario_block ne doit JAMAIS contenir les expressions suivantes qui poussent Nano Banana à recadrer/zoomer : "in the foreground" SEUL (sans description du fond), "close-up of", "centered on", "focused on this element", "framed by", "zoomed-in", "tight shot".
- Préfère décrire l'anchor en mentionnant ses VOISINS pour signaler à Nano Banana que le cadrage complet doit rester intact. Ex : au lieu de "sur le canapé d'angle au premier plan gauche" écris "sur le canapé d'angle situé à gauche, EN PRÉSERVANT la vue panoramique au fond et les plantes en bordure".
- Ajoute systématiquement dans le scenario_block une clause finale : "FRAMING: preserve the full original frame — keep ALL background elements visible (plants, planters, decor, distant view, skyline) — do NOT crop or zoom toward the subjects."

📝 RETOURNE STRICTEMENT CE JSON (aucun markdown, aucun texte hors JSON) :

{{
  "feasibility": "ok" | "skip",
  "skip_reason": null | "raison courte si skip",
  "max_subjects_realistic": int (1 à 5),
  "primary_anchor": "description ultra-précise de l'élément/zone EXISTANT où placer le sujet (ex: 'le transat blanc vide au premier plan à gauche, sous le parasol bleu')",
  "alternative_anchors": ["liste de 1-2 anchors fallback si le primary n'est pas exploitable, ou [] si aucun"],
  "pose_description": "description précise de la pose adaptée à l'anchor (ex: 'reclining on the lounger, propped on one elbow, reading a hardback book, soft natural smile')",
  "outfit_description": "tenue précise du sujet adaptée au contexte de la photo et à la persona",
  "subject_scale_hint": "small" | "medium" | "large",
  "pitfalls_specific_to_this_photo": ["liste de 1-3 pièges propres à cette photo précise"],
  "scenario_block": "TEXTE COMPLET du scenario prêt à coller dans le prompt Nano Banana — doit commencer par 'Place exactly N subject(s)...' avec N = max_subjects_realistic, et inclure : description du sujet, pose, anchor, outfit, lighting hint"
}}

⚠️ RAPPELS POUR LE SCENARIO_BLOCK :
- Format ATTENDU : "Place exactly N subject(s) [on/in/at] [PRIMARY ANCHOR EXACT] — [persona description compact]. [Subject look details]. [Pose details]. [Outfit]. [Lighting]. NEVER look at the camera."
- Le scenario_block est INSÉRÉ TEL QUEL dans le prompt Nano Banana, sois donc précis et opérationnel
- Si feasibility = "skip" → mets "scenario_block": "" et explique skip_reason

Exemple de scenario_block bien formé pour photo piscine + couple :
"Place exactly 2 subjects sitting at the existing pool edge on the bare concrete deck on the right of the frame, feet dangling in the water — a young adult couple. The woman 24-26 (slim toned, golden tan, sun-bleached blonde wavy hair) wears a sleek olive one-piece swimsuit. The man 26-28 (lean athletic, Mediterranean tan, tousled brown hair) wears navy tailored swim shorts. POSE : she sits cross-ankled looking down at the water with a soft smile ; he sits next to her, one arm relaxed behind, head slightly turned toward her. They don't touch. Natural mid-afternoon warm sun on their tanned skin. Neither looks at the camera."
"""


def write_scenario(
    image_path: Path,
    category: str,
    vibe: str | None,
    persona: str,
    model_name: str = SCENARIO_WRITER_MODEL,
    max_retries: int = 2,
) -> dict:
    """Demande à Gemini Vision de rédiger un scenario d'ajout perso adapté à la photo.

    Args:
        image_path : chemin vers l'image ORIGINALE (input du pipeline retouches)
        category : catégorie Gemini Vision (piscine, rooftop, gym, ...)
        vibe : vibe hôtel (Trendy, Luxe, Family-Friendly, ...)
        persona : persona cible (solos, couples, small_groups, families, ...)

    Returns:
        dict {
            "feasibility": "ok" | "skip",
            "skip_reason": str | None,
            "max_subjects_realistic": int,
            "primary_anchor": str,
            "alternative_anchors": list[str],
            "pose_description": str,
            "outfit_description": str,
            "subject_scale_hint": str,
            "pitfalls_specific_to_this_photo": list[str],
            "scenario_block": str,  # ← C'EST CE QU'ON INSÈRE DANS LE PROMPT
            "_meta": {"duration_ms": int, "cost_usd": float, "input_tokens": int, "output_tokens": int}
        }

        Si erreur → retourne {"feasibility": "skip", "skip_reason": "...", "scenario_block": ""}
        avec _meta cost=0.
    """
    _ensure_configured()

    if not image_path.exists():
        return {
            "feasibility": "skip",
            "skip_reason": f"image introuvable : {image_path.name}",
            "max_subjects_realistic": 0,
            "scenario_block": "",
            "_meta": {"duration_ms": 0, "cost_usd": 0, "input_tokens": 0, "output_tokens": 0},
        }

    persona_brief = _PERSONA_BRIEFS.get(persona, _PERSONA_BRIEFS["solos"])
    outfit_hint = _OUTFIT_HINTS_BY_CATEGORY.get(category, "tenue casual adaptée au contexte de la scène")

    meta_prompt = META_PROMPT_TEMPLATE.format(
        category=category or "non spécifiée",
        vibe=vibe or "non spécifiée",
        persona=persona,
        persona_brief=persona_brief,
        outfit_hint=outfit_hint,
    )

    model = genai.GenerativeModel(model_name)

    last_error = None
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            img = Image.open(image_path).convert("RGB")
            response = model.generate_content(
                [meta_prompt, img],
                generation_config={"response_mime_type": "application/json", "temperature": 0.0},
            )
            duration_ms = int((time.time() - t0) * 1000)

            data = json.loads(response.text)
            usage_meta = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0
            output_tokens = getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0
            cost_usd = (
                input_tokens * GEMINI_PRICE_INPUT_USD_PER_M / 1_000_000
                + output_tokens * GEMINI_PRICE_OUTPUT_USD_PER_M / 1_000_000
            )

            data["_meta"] = {
                "duration_ms": duration_ms,
                "cost_usd": round(cost_usd, 6),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model": model_name,
            }
            # Sanity checks sur la réponse
            if data.get("feasibility") not in ("ok", "skip"):
                data["feasibility"] = "skip"
                data["skip_reason"] = "réponse Gemini Vision malformée (feasibility invalide)"
                data["scenario_block"] = ""
            # Coerce max_subjects_realistic en int [1, 5]
            try:
                n = int(data.get("max_subjects_realistic", 1))
                data["max_subjects_realistic"] = max(1, min(5, n))
            except (ValueError, TypeError):
                data["max_subjects_realistic"] = 1
            # Force scenario_block en str
            data["scenario_block"] = (data.get("scenario_block") or "").strip()
            return data
        except Exception as e:
            err = str(e)
            last_error = e
            is_retryable = "429" in err or "500" in err or "503" in err
            if is_retryable and attempt < max_retries:
                m = _re.search(r"retry in (\d+(?:\.\d+)?)\s*s", err)
                wait = (float(m.group(1)) + 2) if m else min(2 ** attempt * 5, 30)
                time.sleep(wait)
                continue
            break

    # Échec final → on signale skip sans casser le pipeline
    return {
        "feasibility": "skip",
        "skip_reason": f"scenario_writer error: {str(last_error)[:200] if last_error else 'unknown'}",
        "max_subjects_realistic": 0,
        "primary_anchor": "",
        "alternative_anchors": [],
        "pose_description": "",
        "outfit_description": "",
        "subject_scale_hint": "medium",
        "pitfalls_specific_to_this_photo": [],
        "scenario_block": "",
        "_meta": {"duration_ms": 0, "cost_usd": 0, "input_tokens": 0, "output_tokens": 0, "error": str(last_error)[:200] if last_error else None},
    }
