"""Module de retouche photo pour le pipeline DayAccess.

Deux modes de retouche, choisis automatiquement selon l'analyse Gemini :
  - LOCAL  : Pillow (luminosité, saturation, balance blancs) — gratuit, instantané
  - AI     : Nano Banana 2 (Gemini 3.1 Flash Image) — transformation éclairage majeure

Usage programmatique :
    from enhance import enhance_one, pick_strategy
    strategy = pick_strategy(analysis)
    output_path = enhance_one(input_path, strategy, output_dir)
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import time
from pathlib import Path
from PIL import Image, ImageEnhance
from google import genai
from google.genai import types
from dotenv import load_dotenv

import brand_lut
import ai_validator

load_dotenv()

# --- Config ---

# Modèle image edit. Nano Banana 2 = sweet spot (5k free / mois, $0.067 paid).
# Alternatives : "gemini-3-pro-image-preview" ($0.134, 4K), "gemini-2.5-flash-image" ($0.039, basique)
NANO_BANANA_MODEL = os.getenv("NANO_BANANA_MODEL", "gemini-3.1-flash-image-preview")

# Pricing Nano Banana 2 (USD/image)
NANO_BANANA_PRICE_USD = 0.067

# --- Prompts (basés sur les exemples Martin) ---

PROMPT_ENSOLEILLEMENT = (
    "Transform this scene into a bright sunny daytime scene with clear natural sunlight. "
    "Keep the original composition, framing, and all objects unchanged. "
    "Replace the current lighting with strong daytime sun, realistic natural shadows, "
    "bright warm daylight, and a clean sunlit atmosphere. "
    "The image should feel fully illuminated by daylight, with crisp highlights, "
    "balanced contrast, and natural warm tones. "
    "Create a realistic, inviting, premium look with a clear sunny daytime ambiance."
)

PROMPT_ENHANCEMENT = (
    "Enhance the lighting of this hotel photo. "
    "Make the sky clearer and bluer, reduce overall contrast, soften shadows. "
    "Keep a natural realistic look. Preserve the original composition. "
    "No HDR effect, no over-saturation."
)

PROMPT_WARM_BOOST = (
    "Slightly warm and brighten this hotel photo. "
    "Add subtle golden-hour warmth, very gentle saturation boost. "
    "Keep all elements, composition, and realism perfectly intact. "
    "Minimal, natural enhancement only."
)

# === Suppression clutter (parasites : objets, mais aussi équipements techniques visibles) ===
PROMPT_REMOVE_CLUTTER = """Remove non-aspirational clutter and technical eyesores from this image while keeping the scene EXACTLY identical otherwise.

REMOVE (if visible) — anything that breaks a premium editorial feel:

Loose objects:
- Electrical cables, wires, exposed pipes, sockets, plugs on walls or floor
- Forgotten items (cups, water bottles, used glasses, crumpled towels, trash, plastic bags, beach toys, plastic toys, sand buckets/spades, beach balls, kid floats abandoned on furniture)
- Personal belongings left behind (piled sandals/flip-flops, scattered clothing, open beach bags, sunscreen bottles)
- Unsightly signage, posters, "out of order" notices, plastic A-boards, price tags
- Construction debris, hazard barriers, tape, hose, fire-extinguisher boxes on a wall

Technical / structural eyesores (Q3 Martin: accept the small risk of bavure):
- Visible surveillance cameras / CCTV (mounted on poles, walls, ceilings)
- Fire-escape staircases visible on neighbouring buildings, fire-escape doors
- Antennas, satellite dishes, telecom poles
- Outdoor air-conditioner units, AC compressors, vents, ventilation grilles, gutter pipes, downspouts (only the obvious eyesore ones — keep architectural details)
- Ugly handrails painted in non-brand colors (plain galvanized steel, etc.) — replace with discreet matching railing if removable risk too high
- Drainage covers, manholes when in plain sight in a key area

KEEP EXACTLY IDENTICAL:
- All hospitality furniture (loungers, daybeds, parasols, tables, chairs, sofas)
- All structures, lighting fixtures, plants, water, sky, architecture proper
- Any food/drinks SERVED on a dining table (cocktail, plate of food → keep; abandoned dirty glass on a lounger → remove)
- All people present in the scene

Reconstruct the underlying surface (sand, tile, wood, fabric, wall, sky) seamlessly where the clutter was. If a structural eyesore is too embedded to remove cleanly, leave it rather than create a glitch.

Photorealistic editorial lifestyle photography. The result must look like a professional cleanup crew passed and the technical building services had been hidden — same scene, just polished.

NEGATIVE PROMPT: removed furniture, altered architecture, missing decor, ghost outlines, blurred patches, CGI artifacts, structural deformation, removed served food or cocktails on a dining table, removed people."""


# === Suppression humain (cas trop chargé > 4 personnes) ===
PROMPT_REMOVE_PEOPLE = """Remove all people from this image while keeping the environment EXACTLY identical.

Composition: preserve the framing, all furniture, decor, plants, structures, lighting, and shadows.
Reconstruction: seamlessly fill the areas where people were standing/sitting using surrounding context.
Style: editorial lifestyle photography, photorealistic, the venue captured during a quiet moment.

Negative prompt: artifacts, ghosting, blurred patches, missing furniture, altered architecture, CGI look."""


# === Recadrage / recomposition ===
PROMPT_RECOMPOSE = """Recompose this image: align the main subject along the rule of thirds, straighten any tilted horizon, balance the frame.

Rules: Keep ALL visible elements — do not crop out furniture or important structures. Subtle adjustments only (max 10% on each side).
Style: photorealistic editorial lifestyle, no artistic filter, no over-processing.

Negative prompt: aggressive cropping, lost elements, distortion, artistic filter."""


# === Templates personnages — basés sur les prompts validés Martin ===

PERSONA_TEMPLATES = {
    "couples": (
        "EITHER one couple (man + woman, mixed-race, 30s) wearing swimsuits/leisure attire, "
        "naturally placed on existing seats in the scene (one each on adjacent loungers, "
        "or sharing one daybed), mid-action (chatting, sharing a moment), "
        "no eye contact with camera. "
        "OR if only one good empty spot exists, ONE single person (woman or man, 30s) is better — "
        "do not force a couple if it requires inventing a second seat."
    ),
    "solos": (
        "ONE single relaxed adult woman (late 20s/early 30s, mixed-race or any ethnicity that fits the scene), "
        "wearing chic leisure attire (one-piece swimsuit, light dress, summer hat optional), "
        "naturally placed on the SINGLE most prominent existing empty lounger or daybed in the foreground, "
        "elegantly reclining or sitting, holding a glass / drink / sunglasses, "
        "looking off-scene (away from camera, profile or 3/4 angle), "
        "mid-action candid moment (sipping, contemplating the view), "
        "warm afternoon golden-hour light on her, premium-accessible mood, lifestyle editorial feel."
    ),
    "families": (
        "a young family of 3-4 (two parents mixed-race + 1-2 children ages 5-10) "
        "wearing casual vacation/beach attire, naturally interacting in the scene, "
        "children mid-action (playing, reaching, talking, slight motion blur), "
        "parents casually engaged (laughing, helping kids, looking at children — not at camera), "
        "candid moment, no posing, no eye contact with camera"
    ),
    "small_groups": (
        "2-3 trendy friends (urban-leisure look, 20s-30s, mixed ethnicities) "
        "naturally placed in the scene, mid-conversation, casual relaxed posture, "
        "looking at each other or off-scene, no eye contact with camera, candid moment"
    ),
    "groups": (
        "3-5 friends (20s-30s, mixed ethnicities, festive but tasteful look) enjoying a daytime moment together, "
        "naturally placed on existing furniture, mid-action (toasting, laughing, chatting), "
        "looking at each other, no eye contact with camera, candid party energy"
    ),
}


# Action contextuelle selon catégorie photo
CATEGORY_ACTION_HINT = {
    "cabana":   "lounging on the daybed/cabana sofa, sunglasses on, relaxed posture",
    "transat":  "reclining on the existing sun lounger(s), correct contact with the chair, natural weight",
    "piscine":  "by the pool edge, on a lounger, OR realistically IN the water (swimming, wading waist-deep, on a pool float) — vary naturally, swimwear, relaxed mood",
    "rooftop":  "standing at the rooftop with the view in background, holding a drink, looking at horizon",
    "f_and_b":  "around the existing dining table, mid-meal moment (passing food, pouring drink), one or two glasses on the table",
    "beach":    "on the existing sun lounger or beach chair, swimwear, relaxed beach moment",
    "exterieur": "naturally placed in the existing outdoor space, casual moment",
    "interieur_commun": "naturally placed in the existing interior space (seated on chairs/sofas, gathered near tables, walking through), casual conversation, fitting the venue type — leisure/business-casual attire, no formal black-tie",
}


def build_persona_prompt(persona: str, category: str, vibe: str | None = None,
                         safe_zones: list[str] | None = None,
                         unsafe_zones: list[str] | None = None,
                         max_humans: int | None = None) -> str:
    """Construit le prompt ajout personnage à partir des templates validés Martin.

    Pattern issu de ses prompts Higgsfield :
      [Enhance & add subjects]
      → Subjects (description détaillée)
      → Action & mood
      → Lighting & realism
      → Integration rules (perspective, scale, contact)
      → Style (camera ref + film stock)
      → Negative prompt
    """
    persona_desc = PERSONA_TEMPLATES.get(persona, PERSONA_TEMPLATES["couples"])
    action_hint = CATEGORY_ACTION_HINT.get(category, "naturally placed in the scene, candid relaxed moment")

    vibe_mood = {
        "Family-Friendly": "warm family vacation energy, playful but tasteful",
        "Party":           "festive daytime vibe, friends having fun, never crowded",
        "Serene":          "quiet contemplative moment, peaceful luxury",
        "Luxe":            "effortless luxury, refined casual elegance",
        "Trendy":          "urban-leisure vibe, lifestyle editorial mood",
    }.get(vibe or "", "warm relaxed daytime moment, premium-accessible feel")

    # Bloc safe zones (analyse spécifique de cette photo par Gemini)
    safe_zones_block = ""
    if safe_zones or unsafe_zones:
        safe_list = "\n".join(f"  - {z}" for z in (safe_zones or []))
        unsafe_list = "\n".join(f"  - {z}" for z in (unsafe_zones or []))
        max_h = max_humans or 3
        safe_zones_block = f"""

SCENE-SPECIFIC SAFE ZONES (analyzed by Gemini Vision on THIS exact photo — non-negotiable):

ALLOWED placements (use ONLY these zones for the new subject(s)):
{safe_list or "  - (no specific safe zones identified — apply generic physical rules)"}

FORBIDDEN placements in this scene (do NOT place subjects here under any circumstance):
{unsafe_list or "  - (none specific)"}

Maximum subjects to add for this scene: {max_h} (less is better).
"""

    return f"""ABSOLUTE FRAMING LOCK (MOST IMPORTANT RULE):
- DO NOT zoom in or out. DO NOT crop. DO NOT change the camera angle, height, or focal length.
- Preserve the EXACT same field of view and image dimensions as the input.
- If you cannot honor this constraint, return the image unchanged.
- The output MUST look like the SAME photograph, just with a few subjects added.

Now, naturally add {persona_desc} to this exact scene.
{safe_zones_block}

ACTION & CONTEXT:
{action_hint}. {vibe_mood}.
Mid-action, candid moment, slight asymmetry — feels like a real captured moment, not staged.

QUANTITY & SCALE (CRITICAL — bias toward LESS):
- DEFAULT: add ONE single subject only (or one couple if persona explicitly is "couple" or "couples"). MORE PEOPLE IS RARELY BETTER.
- HARD MAXIMUM: 3 people total, but use this only if the scene clearly has 3+ obviously empty existing seats and adding more would feel natural.
- TRADE-OFF RULE: if you cannot place the requested number of people on EXISTING furniture without inventing, place FEWER people. A single person on the foreground lounger is far better than 2 people requiring a fabricated daybed.
- ALL added subjects MUST share the SAME camera-relative scale: a person at 10m looks twice smaller than a person at 5m. Respect perspective rigorously.
- Place subjects in ONE coherent group. Do not scatter people in 2+ disconnected zones.

PHYSICAL SAFETY & PLAUSIBILITY (CRITICAL — non-negotiable):
- Subjects MUST be placed on plausible, safe supports: seated on chairs / loungers / sofas / daybeds **THAT ALREADY EXIST IN THE PHOTO**, OR standing on solid floor/ground/decking, OR realistically immersed IN water (swimming, wading waist-deep).
- **DO NOT INVENT OR ADD any furniture, daybed, lounger, raft, platform, float, or any object that is not visibly present in the original input image.** If there is no plausible existing seat for a subject, place them standing on solid ground, OR swimming in the water, OR DO NOT add the subject in that area.
- NEVER ON the water surface as if standing on it. NEVER walking on water. NEVER floating dry without a visible flotation device.
- If a subject is IN the pool, ensure realistic immersion: body partially submerged (waist-deep or more), water displacement around them, wet hair/skin if relevant, splashes acceptable. The water surface MUST react to their presence.
- NEVER standing or sitting ON TOP of furniture meant for lying (no standing on daybeds, sun loungers, or sofas).
- NEVER on the wrong side of any safety barrier, railing, glass panel, or balustrade. Subjects must always be on the safe interior side of any rooftop/balcony/pool railing.
- NEVER in physically dangerous, awkward, or improbable positions (no climbing, no leaning over edges, no unsupported balancing).
- Respect human-scale physics: feet touch ground or seat, hands rest on plausible surfaces, weight is correctly supported.
- If the scene has a railing/barrier (rooftop, balcony, pool edge, terrace), keep ALL subjects on the SAME safe side as the existing furniture.

LIGHTING & REALISM:
Strong natural daytime sunlight, consistent with the existing scene direction.
Natural highlights on skin and clothing, crisp shadows that match the rest of the image.
Skin tones warm, consistent with sunlight, photorealistic textures (not over-sharpened).

INTEGRATION RULES:
- Match exact perspective, scale, and angle of the existing furniture
- Bodies interact correctly with chairs/loungers/tables: natural weight, correct contact, no floating
- Cast shadows MUST match the existing lighting direction
- Do not add or remove any object from the scene; do not alter the architecture or decor
- Preserve original composition and framing exactly

STYLE:
Editorial luxury lifestyle photography. Kodak Vision3 500T look: warm highlights, neutral skin tones,
subtle film grain, soft contrast, slight natural lens flare if relevant. Sony A7R IV / Canon R5
aesthetic: 35mm, f/4, ISO 100, crisp natural detail, candid "caught moment" feel.
Premium-accessible, never catalog-style, never stock-photo-style.

NEGATIVE PROMPT (HARD avoid):
- ANY zoom-in, ANY crop, ANY camera angle change vs input
- inventing, adding, or hallucinating new furniture (especially: floating daybeds, rafts, platforms, beds on the water, new chairs/loungers placed where there were none in the original)
- subjects standing on top of water as if walking on it, or floating dry without a flotation device
- standing on daybeds / sun loungers / sofas / tables / any furniture meant for sitting or lying
- subjects on the wrong side of railings, barriers, glass panels, balustrades
- leaning over rooftop edges, climbing structures, unsupported balancing
- impossible / dangerous / acrobatic poses, levitation, floating bodies
- inconsistent scale between subjects (one person twice the size of another at the same distance)
- more than 3 people total, scattered groups in 3+ disconnected zones
- doubled limbs, distorted anatomy, extra fingers, mismatched shadows
- posed models, looking at camera, fake smiles, stiff postures, crowded scene
- business attire, drunk/loud party, recognizable faces
- harsh HDR, over-saturation, CGI look, over-sharpened plastic skin, glowing edges
"""


# --- Stratégie : router selon l'analyse Gemini ---

def _pick_main_action(
    analysis: dict | None,
    category: str | None = None,
    personas_allowed: list[str] | None = None,
    vibe: str | None = None,
    add_character: bool = False,
) -> dict:
    """Choisit l'action principale (hors crop) à appliquer à la photo."""
    if not analysis:
        return {"action": "local_warm_boost", "prompt": None, "reason": "pas d'analyse Gemini, fallback local"}

    factual = analysis.get("factual", {})
    hints = analysis.get("technical_hints", {})
    cat = category or factual.get("category", "")
    ambiance = (hints.get("ambiance") or "").lower()
    palette = (hints.get("palette_alignment") or "").lower()
    human_count = factual.get("human_count", 0) or 0
    time_of_day = (factual.get("time_of_day") or "").lower()
    issues = analysis.get("issues") or []
    issues_str = " ".join(issues).lower()

    # Mots-clés signalant un problème de lumière (cross-checks ambiance Gemini parfois incohérente)
    DARK_KEYWORDS = ("sombre", "manque de lumière", "manque de lumiere", "peu lumineux", "obscur", "ombrageux")
    NIGHT_KEYWORDS = ("nuit", "nocturne", "couché de soleil", "couche de soleil", "crépuscule", "crepuscule", "twilight")
    has_dark_issue = any(k in issues_str for k in DARK_KEYWORDS)
    has_night_clue = time_of_day in ("nuit", "aube_crepuscule") or any(k in issues_str for k in NIGHT_KEYWORDS)

    # ---- Règles métier intransgressibles (priorité décroissante) ----

    # 1. F&B plats : JAMAIS d'IA générative
    is_food_only = (
        cat == "f_and_b"
        and "cocktail" not in " ".join(factual.get("subjects") or []).lower()
    )
    if is_food_only:
        return {
            "action": "local_warm_boost",
            "prompt": None,
            "reason": "F&B plats : retouche IA interdite (risque inventer plat inexistant)",
        }

    # 2. Règle stricte NUIT : on transforme en jour via IA (pas de photo de nuit en sortie)
    if has_night_clue:
        return {
            "action": "ai_lighting",
            "prompt": PROMPT_ENSOLEILLEMENT,
            "reason": f"photo de nuit/crépuscule → forcée en jour ensoleillé (règle brand stricte)",
        }

    # 3. Ajout personnage forcé en amont (pipeline calcule l'alternance)
    if add_character and personas_allowed:
        persona = personas_allowed[0] if personas_allowed else "couples"
        # Récupère les safe zones décrites par Gemini sur cette photo précise
        safe_zones_block = analysis.get("safe_zones_for_humans") or {}
        safe_zones = safe_zones_block.get("safe_areas") or []
        unsafe_zones = safe_zones_block.get("unsafe_areas") or []
        max_h_raw = safe_zones_block.get("max_recommended")
        try:
            max_h = int(max_h_raw) if max_h_raw is not None else None
        except (ValueError, TypeError):
            max_h = None
        return {
            "action": "ai_add_character",
            "prompt": build_persona_prompt(persona, cat, vibe, safe_zones=safe_zones,
                                           unsafe_zones=unsafe_zones, max_humans=max_h),
            "reason": f"ajout personnage IA ({persona}) sur {cat or 'scène vide'}",
        }

    # 4. Trop de monde (> 4 personnes) → suppression
    if human_count > 4:
        return {
            "action": "ai_remove_people",
            "prompt": PROMPT_REMOVE_PEOPLE,
            "reason": f"{human_count} personnes → photo trop chargée, on retire",
        }

    # 4b. Clutter visible (Gemini a identifié des éléments parasites concrets)
    clutter_list = analysis.get("clutter_to_remove") or []
    clutter_keywords_in_issues = (
        "câble", "cable", "prise", "gobelet", "panneau", "détritus",
        "trash", "wire", "poubelle", "plastic", "fil", "poteau",
        # objets non-aspirationnels élargis
        "sceau", "seau", "bucket", "pelle", "spade", "jouet", "toy",
        "ballon", "sandale", "flip-flop", "serviette", "towel",
        "sac", "bag", "extincteur", "barrière", "hose", "tuyau",
        # Eyesores techniques/structurels (Q3 Martin)
        "caméra", "camera", "surveillance", "cctv",
        "escalier de secours", "fire escape", "issue de secours",
        "antenne", "antenna", "parabole", "satellite",
        "climatiseur", "ac unit", "air conditioner", "ventilation",
        "vmc", "extracteur", "grille", "gaine",
    )
    has_clutter_in_issues = any(k in issues_str for k in clutter_keywords_in_issues)
    if clutter_list or has_clutter_in_issues:
        clutter_desc = ", ".join(clutter_list[:3]) if clutter_list else "éléments parasites mentionnés en issues"
        return {
            "action": "ai_remove_clutter",
            "prompt": PROMPT_REMOVE_CLUTTER,
            "reason": f"nettoyage clutter : {clutter_desc}",
        }

    # 5b. Cadrage off détecté dans les issues mais pas de crop précis recommandé → on tente IA recompose
    cadrage_keywords = ("cadrage", "horizon penché", "horizon penche", "asymétrique", "asymetrique", "mal centré", "mal centre", "non centré", "non centre", "tilted")
    if any(k in issues_str for k in cadrage_keywords):
        return {
            "action": "ai_recompose",
            "prompt": PROMPT_RECOMPOSE,
            "reason": f"issue cadrage détectée : {issues[0] if issues else ''}",
        }

    # 6. Routage lumière — on regarde aussi les issues car Gemini est parfois incohérent
    #    (ex: ambiance="lumineux-chaud" mais issue="ambiance sombre")
    if ambiance.startswith("sombre") or has_dark_issue:
        why = f"ambiance {ambiance}" if ambiance.startswith("sombre") else f"issue '{issues[0]}'"
        return {
            "action": "ai_lighting",
            "prompt": PROMPT_ENSOLEILLEMENT,
            "reason": f"{why} → ensoleillement IA",
        }

    if ambiance == "lumineux-froid" or palette == "off-brand":
        return {
            "action": "ai_lighting",
            "prompt": PROMPT_ENHANCEMENT,
            "reason": f"{ambiance} / palette {palette} → enhancement IA",
        }

    # 7. Déjà OK → micro-warm local
    if ambiance == "lumineux-chaud" and palette in ("aligned-warm", "neutral"):
        return {
            "action": "local_warm_boost",
            "prompt": None,
            "reason": "déjà bien aligné → micro-warm local pour homogénéité",
        }

    return {
        "action": "local_warm_boost",
        "prompt": None,
        "reason": "fallback warm boost local",
    }


def _maybe_crop_step(analysis: dict | None) -> dict | None:
    """Retourne un step crop si Gemini a recommandé un crop pertinent (60-95% conservé)."""
    if not analysis:
        return None
    rec = analysis.get("recommended_crop") or {}
    if not rec.get("should_crop"):
        return None
    x_min = rec.get("x_min_pct", 0)
    y_min = rec.get("y_min_pct", 0)
    x_max = rec.get("x_max_pct", 100)
    y_max = rec.get("y_max_pct", 100)
    area_ratio = max(0.0, ((x_max - x_min) * (y_max - y_min)) / (100 * 100))
    if not (0.60 <= area_ratio <= 0.95):
        return None
    return {
        "action": "local_smart_crop",
        "prompt": None,
        "crop_box_pct": {
            "x_min_pct": x_min, "y_min_pct": y_min,
            "x_max_pct": x_max, "y_max_pct": y_max,
        },
        "reason": f"recadrage Gemini ({int(area_ratio * 100)}% conservé) : {rec.get('reason') or 'cadrage à améliorer'}",
    }


def _has_clutter(analysis: dict | None) -> bool:
    """Vrai si Gemini a identifié du clutter (objets ou eyesores techniques) à retirer."""
    if not analysis:
        return False
    clutter_list = analysis.get("clutter_to_remove") or []
    if clutter_list:
        return True
    issues = analysis.get("issues") or []
    issues_str = " ".join(issues).lower()
    keywords = (
        "câble", "cable", "prise", "gobelet", "panneau", "détritus",
        "trash", "wire", "poubelle", "plastic", "fil", "poteau",
        "sceau", "seau", "bucket", "pelle", "jouet", "toy",
        "ballon", "sandale", "serviette", "towel", "sac", "bag",
        # Eyesores techniques (Q3 Martin)
        "caméra", "camera", "surveillance", "cctv",
        "escalier de secours", "fire escape", "issue de secours",
        "antenne", "antenna", "parabole",
        "climatiseur", "ac unit", "air conditioner", "ventilation",
        "vmc", "extracteur", "grille", "gaine",
    )
    return any(k in issues_str for k in keywords)


def pick_strategy(
    analysis: dict | None,
    category: str | None = None,
    personas_allowed: list[str] | None = None,
    vibe: str | None = None,
    add_character: bool = False,
) -> dict:
    """Construit la séquence d'actions à appliquer à une photo (chaînage possible, max 2 IA).

    Returns:
        {
            "steps": [step1, step2, ...],
            "action": <action principale>,
            "prompt": <prompt principal>,
            "reason": "step1_reason + step2_reason",
            "crop_box_pct": ...,
        }

    Logique :
    - Crop local optionnel (gratuit) en pré-traitement
    - Si clutter détecté ET ajout personnage demandé → chaînage `ai_remove_clutter` → `ai_add_character`
      (on nettoie d'abord, puis on ajoute la personne dans la scène propre)
    - Sinon : step principale unique
    """
    crop_step = _maybe_crop_step(analysis)
    main_step = _pick_main_action(analysis, category, personas_allowed, vibe, add_character)

    steps: list[dict] = []
    if crop_step:
        steps.append(crop_step)

    # Cas spécial : ajout perso ET clutter détecté → on chaîne clutter avant add_character
    # (le PROMPT_REMOVE_CLUTTER ne touche pas la composition donc l'output sert d'input propre)
    if main_step["action"] == "ai_add_character" and _has_clutter(analysis):
        clutter_list = (analysis or {}).get("clutter_to_remove") or []
        clutter_desc = ", ".join(clutter_list[:3]) if clutter_list else "objets parasites détectés"
        steps.append({
            "action": "ai_remove_clutter",
            "prompt": PROMPT_REMOVE_CLUTTER,
            "reason": f"pré-nettoyage clutter avant ajout perso : {clutter_desc}",
        })

    # Si on a déjà cropé ET que l'action principale est juste warm_boost (rien d'urgent),
    # on saute le warm — le crop est déjà une amélioration suffisante.
    if not (crop_step and main_step["action"] == "local_warm_boost"):
        steps.append(main_step)

    # Sécurité : si la liste est vide (cas dégénéré), au moins warm_boost
    if not steps:
        steps.append(main_step)

    # L'action "principale" pour le badge front = la plus marquante (priorité IA > smart_crop > warm)
    priority = {"ai_add_character": 7, "ai_remove_people": 6, "ai_remove_clutter": 5,
                "ai_lighting": 4, "ai_recompose": 3, "local_smart_crop": 2, "local_warm_boost": 1}
    primary_step = max(steps, key=lambda s: priority.get(s["action"], 0))
    combined_reason = " + ".join(s["reason"] for s in steps)

    out = {
        "steps": steps,
        "action": primary_step["action"],
        "prompt": primary_step.get("prompt"),
        "reason": combined_reason,
    }
    if "crop_box_pct" in primary_step:
        out["crop_box_pct"] = primary_step["crop_box_pct"]
    return out


# Heuristique pour repérer les photos candidates ajout personnage quand Gemini ne le retourne pas explicitement.
# Catégorie compatible : amenities + intérieur commun (sauf chambre, interdite par règle métier).
AI_ADD_OK_CATEGORIES = {
    "cabana", "transat", "piscine", "rooftop", "beach", "exterieur", "interieur_commun"
}


def has_narrative_human(analysis: dict) -> bool:
    """Vrai si la photo a une vraie présence humaine narrative (pas juste mains/bras/pieds)."""
    if not analysis:
        return False
    factual = analysis.get("factual") or {}
    presence = (factual.get("human_presence_type") or "").lower()
    if presence in ("full_visible", "fully visible", "complete"):
        return True
    if presence in ("partial", "none"):
        return False
    # Fallback si Gemini ne retourne pas le champ : on regarde human_count ET human_face_visible
    human_count = factual.get("human_count", 0) or 0
    face_visible = bool(factual.get("human_face_visible"))
    return human_count > 0 and face_visible


def is_add_character_candidate(analysis: dict) -> bool:
    """Vrai si la photo est candidate à un ajout personnage IA.

    Source de vérité (par ordre) :
      1. Champ explicite Gemini ai_add_character_candidate.is_candidate
      2. Heuristique : catégorie ∈ AI_OK + pas de présence humaine narrative
    """
    if not analysis:
        return False
    ai_block = analysis.get("ai_add_character_candidate")
    if isinstance(ai_block, dict) and isinstance(ai_block.get("is_candidate"), bool):
        return ai_block["is_candidate"]

    factual = analysis.get("factual") or {}
    cat = (factual.get("category") or "").lower()
    if cat not in AI_ADD_OK_CATEGORIES:
        return False
    # Si déjà un humain narratif (pas juste mains) → pas besoin d'ajouter
    if has_narrative_human(analysis):
        return False
    # Heuristique sujets : si on voit transat/daybed/cabana/chaise/table → candidate
    subjects = " ".join(factual.get("subjects") or []).lower()
    keywords = ("transat", "daybed", "cabana", "chaise", "lounger", "lit", "fauteuil",
                "banquette", "sofa", "table", "chair", "couch", "salon")
    return any(k in subjects for k in keywords) or cat in ("cabana", "transat", "interieur_commun")


# --- Implémentation : retouches locales (Pillow) ---

def enhance_local_crop(input_path: Path, output_path: Path, crop_box_pct: dict) -> dict:
    """Recadrage local via Pillow à partir d'une box recommandée par Gemini (en %).

    Args:
        crop_box_pct : {x_min_pct, y_min_pct, x_max_pct, y_max_pct} (0-100)

    Garantie : aucune régénération, juste un découpage de l'image existante.
    """
    t0 = time.time()
    img = Image.open(input_path).convert("RGB")
    w, h = img.size

    x_min = max(0, min(int(w * crop_box_pct.get("x_min_pct", 0) / 100), w))
    y_min = max(0, min(int(h * crop_box_pct.get("y_min_pct", 0) / 100), h))
    x_max = max(x_min + 1, min(int(w * crop_box_pct.get("x_max_pct", 100) / 100), w))
    y_max = max(y_min + 1, min(int(h * crop_box_pct.get("y_max_pct", 100) / 100), h))

    # Garde-fou : si la box prend moins de 60% de l'image, on rejette (Gemini a probablement halluciné)
    area_ratio = ((x_max - x_min) * (y_max - y_min)) / (w * h)
    if area_ratio < 0.6:
        # Trop agressif → on tombe sur un warm boost simple
        img.save(output_path, quality=92)
        return {
            "duration_ms": int((time.time() - t0) * 1000),
            "cost_usd": 0,
            "method": "pillow_crop_skipped (box too aggressive)",
            "framing_changed": False,
        }

    cropped = img.crop((x_min, y_min, x_max, y_max))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_path, quality=92)
    return {
        "duration_ms": int((time.time() - t0) * 1000),
        "cost_usd": 0,
        "method": f"pillow_crop ({x_max - x_min}x{y_max - y_min} de {w}x{h}, {area_ratio:.0%} conservé)",
        "framing_changed": True,  # le cadrage est volontairement modifié
        "framing_warning": None,  # pas un warning, c'est intentionnel
    }


def enhance_local_warm(input_path: Path, output_path: Path) -> dict:
    """Applique la LUT brand Dayuse (= ton cible cohérent inter-photos / inter-hôtels).

    Paramètres dans config/brand_lut.json. C'est ce qu'on applique aux photos déjà
    conformes brand : pas besoin d'IA, juste l'harmonisation tonale.
    """
    t0 = time.time()
    res = brand_lut.apply_brand_lut(input_path, output_path)
    return {
        "duration_ms": int((time.time() - t0) * 1000),
        "cost_usd": 0,
        "method": "brand_lut",
        "params": res.get("params_applied"),
    }


# --- Implémentation : retouche IA Nano Banana ---

_GENAI_CLIENT = None


def _get_genai_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante")
        _GENAI_CLIENT = genai.Client(api_key=api_key)
    return _GENAI_CLIENT


def enhance_ai(input_path: Path, output_path: Path, prompt: str,
               model: str = NANO_BANANA_MODEL) -> dict:
    """Retouche via Nano Banana 2. Préserve la composition."""
    t0 = time.time()
    client = _get_genai_client()

    with open(input_path, "rb") as f:
        image_bytes = f.read()

    # MIME type
    suffix = input_path.suffix.lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
        suffix.lstrip("."), "image/jpeg"
    )

    response = client.models.generate_content(
        model=model,
        contents=[
            prompt,
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
        ],
    )

    # Extraction de l'image générée depuis la réponse
    image_data = None
    text_response = None
    for part in response.candidates[0].content.parts:
        if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
            image_data = part.inline_data.data
            break
        if hasattr(part, "text") and part.text:
            text_response = part.text

    if not image_data:
        raise RuntimeError(
            f"Nano Banana n'a pas retourné d'image. Texte reçu : {text_response[:200] if text_response else 'aucun'}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(image_data)

    # Check post-IA : si dimensions output != input, c'est que Nano Banana a recadré/zoomé.
    # On flag (pour que le pipeline puisse décider de rejeter ou accepter).
    framing_changed = False
    framing_warning = None
    try:
        with Image.open(input_path) as before:
            in_w, in_h = before.size
        with Image.open(output_path) as after:
            out_w, out_h = after.size
        in_ratio = in_w / max(in_h, 1)
        out_ratio = out_w / max(out_h, 1)
        ratio_diff = abs(in_ratio - out_ratio) / max(in_ratio, 0.01)
        if ratio_diff > 0.05:  # >5% de différence d'aspect ratio = recadrage non voulu
            framing_changed = True
            framing_warning = f"aspect ratio modifié : {in_w}x{in_h} → {out_w}x{out_h} (Δratio={ratio_diff:.0%})"
    except Exception:
        pass

    return {
        "duration_ms": int((time.time() - t0) * 1000),
        "cost_usd": NANO_BANANA_PRICE_USD,
        "method": f"nano_banana_2 ({model})",
        "framing_changed": framing_changed,
        "framing_warning": framing_warning,
    }


# --- Orchestrateur ---

def _apply_step(input_path: Path, output_path: Path, step: dict) -> dict:
    """Applique UNE step. Retourne le dict de résultat de la fonction sous-jacente."""
    action = step["action"]
    if action.startswith("ai_"):
        if not step.get("prompt"):
            raise RuntimeError(f"action {action} sans prompt")
        return enhance_ai(input_path, output_path, step["prompt"])
    if action == "local_smart_crop":
        return enhance_local_crop(input_path, output_path, step.get("crop_box_pct") or {})
    if action == "local_warm_boost":
        return enhance_local_warm(input_path, output_path)
    raise RuntimeError(f"action inconnue : {action}")


# ━━━ Renforcement de prompt par type de violation détectée par le validateur post-IA ━━━
# Quand une violation est détectée, on retry l'étape IA avec un texte additionnel ciblant
# précisément la violation. Coût retry : 1 appel Nano Banana + 1 re-validation Gemini Flash
# ≈ $0.068. Ne déclenche que sur les ~5-10% de cas en violation.
VIOLATION_REINFORCEMENT = {
    "invented_furniture": (
        "ABSOLUTE PROHIBITION ON NEW FURNITURE: in your previous output you ADDED furniture (daybed/lounger/raft/platform) "
        "that does NOT exist in the original photo. THIS IS FORBIDDEN. "
        "If there is no plausible existing seat for a subject, place that subject standing on the solid ground/floor/sand/deck, "
        "OR realistically immersed waist-deep in water (if water is present), "
        "OR DO NOT add the subject at all in that area. "
        "Re-examine the original image: only loungers/daybeds/chairs/tables that are clearly visible in the input may be used as supports."
    ),
    "subject_on_water": (
        "ABSOLUTE PROHIBITION ON WALKING-ON-WATER: in your previous output you placed a subject ON TOP of the water surface "
        "(or on a fabricated floating object). FORBIDDEN. "
        "Place subjects either fully IN the water (swimming, body partially submerged, water displacement around them) "
        "OR on the solid pool deck / sand / floor — never floating dry, never standing on the water surface."
    ),
    "subject_on_furniture_top": (
        "ABSOLUTE PROHIBITION: do NOT place subjects standing or sitting on top of daybeds, sun loungers, or sofas — these are for lying. "
        "Subjects sit, recline, or stand on solid ground. Never use horizontal soft furniture as a platform to stand on."
    ),
    "subject_wrong_side_barrier": (
        "ABSOLUTE PROHIBITION: subjects must always remain on the SAFE INTERIOR side of any railing, glass panel, balustrade, or barrier visible in the scene."
    ),
    "scene_regenerated": (
        "PRESERVE THE EXACT ORIGINAL SCENE: in your previous output, the camera angle / perspective / decor changed. "
        "FORBIDDEN. The output must look like the SAME photograph as the input — same framing, same architecture, same lighting direction, same furniture positions. "
        "Only minimally modify what was requested (subjects, lighting, clutter), NOTHING else."
    ),
    "inconsistent_scale": (
        "MAINTAIN CONSISTENT SCALE: any subjects added must share the same camera-relative scale. A person at 10m from camera is roughly half the apparent size of a person at 5m. Verify perspective rigorously. If unsure, add fewer subjects."
    ),
    "architecture_changed": (
        "DO NOT ALTER THE ARCHITECTURE: walls, structures, decor, plants, water shape, sky, and overall composition must remain identical to the input. Only requested transformations apply."
    ),
    "lighting_break": (
        "MATCH EXISTING LIGHTING: shadows on added subjects must follow the same direction and softness as the existing shadows in the photo. No mismatched key light, no different time of day on the subject vs the scene."
    ),
}


def _reinforced_prompt(original_prompt: str, violations: list[str]) -> str:
    """Construit un prompt 'durci' en concaténant les renforcements ciblés sur les violations détectées."""
    if not original_prompt or not violations:
        return original_prompt
    blocks = []
    for v in violations:
        text = VIOLATION_REINFORCEMENT.get(v)
        if text:
            blocks.append(f"[CRITICAL — RETRY AFTER VIOLATION '{v}']\n{text}")
    if not blocks:
        return original_prompt
    header = (
        "PREVIOUS ATTEMPT FAILED automated quality check. The output had violations listed below. "
        "You MUST avoid repeating these mistakes in this new attempt.\n\n"
        + "\n\n".join(blocks)
        + "\n\n[ORIGINAL TASK INSTRUCTION FOLLOWS]\n\n"
    )
    return header + original_prompt


# Violations qu'on tente de corriger via retry. Les autres (lighting_break, etc. en contexte ai_lighting)
# sont déjà filtrées plus tôt dans ai_validator.
ACTIONABLE_VIOLATIONS = {
    "invented_furniture",
    "subject_on_water",
    "subject_on_furniture_top",
    "subject_wrong_side_barrier",
    "scene_regenerated",
    "inconsistent_scale",
    "architecture_changed",
}


def enhance_one(input_path: Path, strategy: dict, output_dir: Path) -> dict:
    """Applique la séquence de steps en chaînant les outputs.

    Si strategy['steps'] est défini → chaîne (max 2 steps).
    Sinon (rétrocompat ancien format) → 1 seule action.

    Validation post-IA + auto-correction :
    - Après les steps, on valide l'output via ai_validator.
    - Si violations actionables → 1 retry de la dernière step IA avec prompt durci ciblant la violation.
    - Si toujours violations après retry → fallback : on remplace l'output par l'ORIGINAL non retouché
      (préférable à une photo pétée qui passe en publication).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / input_path.name

    steps = strategy.get("steps")
    if not steps:
        # Rétrocompat : une seule action au top-level
        steps = [{"action": strategy["action"], "prompt": strategy.get("prompt"),
                  "crop_box_pct": strategy.get("crop_box_pct"), "reason": strategy.get("reason", "")}]

    total_cost_usd = 0.0
    total_duration_ms = 0
    methods: list[str] = []
    framing_changed = False
    framing_warning = None
    current_input = input_path
    intermediate_paths: list[Path] = []
    last_ai_step_input: Path | None = None  # input qui a alimenté la dernière étape IA (pour retry)
    last_ai_step_index: int | None = None

    try:
        for i, step in enumerate(steps):
            is_last = (i == len(steps) - 1)
            step_out = output_path if is_last else (output_dir / f"_step{i}_{input_path.name}")
            if step["action"].startswith("ai_"):
                last_ai_step_input = current_input
                last_ai_step_index = i
            res = _apply_step(current_input, step_out, step)
            total_cost_usd += res.get("cost_usd", 0)
            total_duration_ms += res.get("duration_ms", 0)
            methods.append(res.get("method", step["action"]))
            if res.get("framing_changed"):
                framing_changed = True
                if res.get("framing_warning"):
                    framing_warning = res["framing_warning"]
            current_input = step_out
            if not is_last:
                intermediate_paths.append(step_out)

        # ━━━ Validation post-IA (avant la LUT, pour détecter les violations sur l'output IA brut) ━━━
        # On valide UNIQUEMENT si une étape IA a été appliquée (pas pour les photos pure local_warm_boost / smart_crop).
        had_ai_step = any(s["action"].startswith("ai_") for s in steps)
        ai_validation = None
        retry_attempted = False
        fallback_to_original = False
        if had_ai_step:
            # Récupère l'action principale du chaînage pour adapter la validation
            ai_steps = [s for s in steps if s["action"].startswith("ai_")]
            primary_ai_action = ai_steps[-1]["action"] if ai_steps else None
            try:
                ai_validation = ai_validator.validate_ai_output(
                    input_path, output_path, action_context=primary_ai_action,
                )
                total_cost_usd += ai_validation.get("cost_usd", 0)
                total_duration_ms += ai_validation.get("duration_ms", 0)
            except Exception as e:
                ai_validation = {"ok": True, "violations": [], "summary": f"validation skip: {e}"}

            # ━━━ Auto-correction : retry avec prompt durci sur les violations actionables ━━━
            actionable_violations = [
                v for v in (ai_validation.get("violations") or [])
                if v in ACTIONABLE_VIOLATIONS
            ]
            if actionable_violations and last_ai_step_input and last_ai_step_index is not None:
                retry_attempted = True
                last_step = steps[last_ai_step_index]
                reinforced = _reinforced_prompt(last_step.get("prompt", ""), actionable_violations)
                try:
                    res2 = enhance_ai(last_ai_step_input, output_path, reinforced)
                    total_cost_usd += res2.get("cost_usd", 0)
                    total_duration_ms += res2.get("duration_ms", 0)
                    methods.append(f"retry_after_{','.join(actionable_violations[:2])}")
                    # Re-validation
                    ai_validation2 = ai_validator.validate_ai_output(
                        input_path, output_path, action_context=primary_ai_action,
                    )
                    total_cost_usd += ai_validation2.get("cost_usd", 0)
                    total_duration_ms += ai_validation2.get("duration_ms", 0)

                    still_bad = [
                        v for v in (ai_validation2.get("violations") or [])
                        if v in ACTIONABLE_VIOLATIONS
                    ]
                    # Note l'historique : on garde ai_validation2 mais on signale qu'il y a eu retry
                    ai_validation2["retry_attempted"] = True
                    ai_validation2["violations_before_retry"] = ai_validation.get("violations", [])
                    ai_validation = ai_validation2

                    if still_bad:
                        # ━ FALLBACK : on remplace l'output par l'ORIGINAL non retouché ━
                        # Mieux vaut une photo non retouchée mais propre, qu'une photo IA pétée.
                        try:
                            shutil.copy(input_path, output_path)
                            fallback_to_original = True
                            methods.append("fallback_original")
                            ai_validation["fallback_to_original"] = True
                        except Exception:
                            pass
                except Exception as e:
                    # Retry IA crashe → on garde l'output original IA (pas de fallback automatique)
                    ai_validation["retry_error"] = str(e)[:200]

        # Cleanup des intermediaires (APRÈS les retries éventuels qui pouvaient s'en servir)
        for p in intermediate_paths:
            try:
                p.unlink()
            except Exception:
                pass

        # ━━━ Post-traitement obligatoire : LUT brand Dayuse ━━━
        # Toutes les photos finales passent par la LUT pour cohérence inter-photos/inter-hôtels.
        # Sauf si la dernière step était DÉJÀ local_warm_boost (= la LUT a déjà été appliquée).
        last_action = steps[-1]["action"] if steps else None
        brand_lut_applied = False
        if last_action != "local_warm_boost":
            try:
                brand_lut.apply_brand_lut(output_path, output_path)
                methods.append("brand_lut")
                brand_lut_applied = True
            except Exception:
                # Si la LUT échoue (rare), on garde l'output sans LUT
                pass

        return {
            "input_path": str(input_path),
            "output_path": str(output_path),
            "action": strategy["action"] if not fallback_to_original else "fallback_original",
            "reason": strategy.get("reason", "") if not fallback_to_original else (
                "Validation post-IA échouée 2× → fallback sur l'originale (mieux qu'une photo IA pétée)."
            ),
            "steps": [{"action": s["action"], "reason": s.get("reason", "")} for s in steps],
            "method": " → ".join(methods),
            "cost_usd": round(total_cost_usd, 6),
            "duration_ms": total_duration_ms,
            "framing_changed": framing_changed,
            "framing_warning": framing_warning,
            "brand_lut_applied": brand_lut_applied or last_action == "local_warm_boost",
            "ai_validation": ai_validation,
            "retry_attempted": retry_attempted,
            "fallback_to_original": fallback_to_original,
        }
    except Exception as e:
        # Cleanup partiel
        for p in intermediate_paths:
            try:
                p.unlink()
            except Exception:
                pass
        return {
            "input_path": str(input_path),
            "output_path": None,
            "action": strategy["action"],
            "reason": strategy.get("reason", ""),
            "error": str(e)[:500],
            "cost_usd": round(total_cost_usd, 6),
            "duration_ms": total_duration_ms,
        }
