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

# ━ Upscale Lanczos final ━
# Nano Banana sort à ~1264x843 fixe (testé empiriquement 12/05/2026, indépendant
# de la résolution source). Les UIs Dayuse desktop affichent ces photos en
# 1500-1800px → upscale navigateur médiocre + pixelisation perceptible en Retina.
# On applique un Lanczos final ×2 (1264→2528) pour servir des photos finales
# nativement HD. Lanczos = interpolation classique, n'ajoute pas de détail réel
# mais évite la pixelisation et garde des arêtes nettes.
# Pour désactiver : FINAL_UPSCALE_FACTOR=1.0 dans .env. Pour x3 (8K) : 3.0.
FINAL_UPSCALE_FACTOR = float(os.getenv("FINAL_UPSCALE_FACTOR", "2.0"))

# --- Prompts (basés sur les exemples Martin) ---

PROMPT_ENSOLEILLEMENT = (
    "Transform this scene into a bright sunny daytime scene with clear natural sunlight. "
    "Keep the original composition, framing, and all objects unchanged. "
    "Replace the current lighting with strong daytime sun, realistic natural shadows, "
    "bright warm daylight, and a clean sunlit atmosphere. "
    "The image should feel fully illuminated by daylight, with crisp highlights, "
    "balanced contrast, and natural warm tones. "
    "Create a realistic, inviting, premium look with a clear sunny daytime ambiance.\n\n"
    "💡 ARTIFICIAL LIGHTS — TURN THEM OFF / DIM TO INVISIBLE :\n"
    "In sunlit daytime, artificial fixtures are NOT lit (or visually negligible vs the sun). "
    "If the input shows lit lamps, wall sconces, ceiling spots / downlights, LED strips, "
    "pendant lights, accent uplights, neon signage, candles or any glowing fixture :\n"
    "- KEEP the FIXTURE itself visible (it's part of the architecture — don't invent / don't remove).\n"
    "- TURN OFF its emission : no glow, no light spill on nearby walls/ceiling, no specular hotspot on the bulb/LED.\n"
    "- Replace the cast pool of warm light on adjacent surfaces by the natural daylight ambient tone.\n"
    "- The bulb / tube / LED panel appears as a dark or neutral object (not emissive).\n"
    "Reason : a photo with visible lit lamps + bright daylight looks unnatural ('lights left on at noon'). "
    "Real sunlit photos have all artificial lights off or imperceptible.\n\n"
    "🚨 ABSOLUTE ARCHITECTURAL PRESERVATION (CRITICAL — non-negotiable) :\n"
    "You may ONLY change the QUALITY of light (intensity, color temperature, direction, "
    "softness). You MUST NOT add, remove, transform, or invent any architectural element :\n"
    "- DO NOT transform alcoves, niches, walls, displays, columns, ceilings, panels, screens, "
    "or any opaque/closed surface INTO windows, openings, skylights, glass facades, or "
    "sources of natural light.\n"
    "- DO NOT open new windows that don't exist in the input.\n"
    "- DO NOT add views of city / sky / nature outside that were not visible originally.\n"
    "- DO NOT remove or replace existing decor (artwork, neon signage, color panels, "
    "wallpaper, murals) — even if it looks 'less aspirational' than sunlit walls.\n"
    "- If the input has a colored neon-lit alcove → it remains a colored neon-lit alcove "
    "in the output (the FIXTURE stays, but its EMISSION is dimmed/off — see ARTIFICIAL LIGHTS rule above).\n"
    "- The light SOURCES visible in the input (windows, lamps, skylights) stay at their "
    "original positions, sizes and shapes — but for LAMPS/SCONCES/SPOTS, turn their emission OFF.\n"
    "- Walls, ceilings, floors keep their materials and patterns identical. Tiles, paint, "
    "wood, carpet remain unchanged.\n\n"
    "If you cannot brighten the scene without inventing new windows or removing existing "
    "decor → return the image with ONLY a global warm color/exposure shift on the existing "
    "pixels (no structural change). A photo that is just 'a bit brighter' is acceptable. "
    "A photo with fabricated architecture is REJECTED.\n\n"
    "NEGATIVE PROMPT : new windows, new openings, invented skylights, fabricated city view, "
    "removed artwork, removed neon, replaced wall panels, walls turned into glass facades, "
    "alcoves turned into windows, transformed displays, new architectural elements, "
    "visibly glowing lamps in daylight, lit sconces under sunlight, emissive ceiling spots, "
    "warm light pools on walls under bright daylight, lamps left on at noon."
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

# === Ajout pool float (standalone — sans humain, pour photos déjà peuplées ou non) ===
def build_pool_float_only_prompt(float_desc: str, is_aerial: bool = False) -> str:
    """Prompt pour ajouter UNIQUEMENT une bouée dans une photo piscine, sans toucher au reste.

    Utilisé quand la photo n'a pas besoin d'add_character mais qu'on veut quand même
    booster son côté playful (Martin 11/05/2026 : "ça peut être ajouté en plus des humains,
    pas un critère unique").

    Args:
        float_desc : description du float ("inflatable flamingo float, pink", etc.)
        is_aerial : True si la photo est en vue aérienne / drone (modifie les contraintes
            de perspective — Martin 15/05/2026, bug Moxy Miami où cygne 3D frontale
            a été ajouté sur une photo top-down → look incohérent).
    """
    # Bloc perspective spécifique vue aérienne (Martin 15/05/2026)
    aerial_block = """
📐 AERIAL VIEW LOCK — CRITICAL (this photo is shot from ABOVE, top-down / drone perspective) :
- The float MUST be rendered with the SAME top-down perspective as the rest of the scene.
- Seen from ABOVE as a FLAT shape : ellipse for a donut, elongated flat shape for flamingo/swan, rectangle for a lilo.
- We see only the TOP surface of the float — its profile / side / volume must NOT be visible.
- ❌ FORBIDDEN : float rendered in 3D perspective (frontale catalog product shot, side view, 3/4 angle).
- ❌ FORBIDDEN : float that "stands up" out of the water vertically.
- ✅ The float lies COMPLETELY FLAT on the water surface, partially submerged where appropriate.
- Same shading direction as other floats already in the pool (if any visible).
- Shadow on the water is a soft ellipse directly below the float (sun overhead in aerial shots), NOT a long shadow stretching sideways.
""" if is_aerial else ""

    return f"""🛑 ABSOLUTE RULE — ADD ONE SINGLE POOL FLOAT, NOTHING ELSE:

You are ONLY allowed to add ONE pool float in the existing pool water of this image — specifically: {float_desc}.

🚨 THE FLOAT IS THE *ONLY* NEW PIXEL (Martin 19/05/2026, retry bug invented_furniture) :
Every pixel of the output OTHER than the float MUST be PIXEL-IDENTICAL to the input.
In particular, there must be:
- ❌ NOTHING UNDER the float (no raft, no platform, no daybed, no rigid support — the float
  sits DIRECTLY on the existing water surface)
- ❌ NOTHING BESIDE the float (no second float, no auxiliary object, no decoration added)
- ❌ NOTHING ATTACHED to the float (no rope, no platform behind, no extra inflatable
  surrounding it)
- ❌ NO ANCHOR / PLATFORM / SUPPORT visible anywhere in the water that wasn't there before
- ❌ NO HAND, no body part, no shadow of a person near the float
The float floats FREELY in the pool water. The water remains the water. Nothing
else changes. If you can't add the float WITHOUT inventing some kind of support
or accompanying object → DO NOT add it. Return image UNCHANGED.

You MUST NEVER add ANY of the following:
- Any human, person, character, body part, hand, leg, shadow of a person
- Any furniture, lounger, daybed, towel, plant, decoration, drink, sign, logo
- Any new architecture, wall, column, ceiling, balustrade, railing
- Any modification to the existing water shape, decking, plants, walls, ceiling, lighting
- Any second float — exactly ONE float, no more
- Any change to the framing, composition, perspective, lighting, color grading
{aerial_block}
🎯 PLACEMENT RULES for the float:
- Place it IN the existing pool water, in a zone that is currently EMPTY (no swimmers, no decoration in that spot already).
- Pick a natural-looking position : near the center of the water surface, or gently drifting near the edge.

📏 SCALE LOCK — CRITICAL (Martin 15/05/2026 + 19/05/2026, bug bouées géantes persistant) :
- The float must NEVER cover more than ~10% of the visible water surface. NOT 25%, NOT 20%, NOT 15% — strict 10% max.
- SCALE ANCHOR : the float must be SMALLER than the smallest lounger/daybed visible around the pool. If a lounger appears N pixels long in the input, the float should be ≤ 0.8 × N (NOT N, NOT 2N).
- VISUAL TEST : if you mentally place an ADULT HUMAN lying down next to your float, the human should be LONGER than the float, not shorter. The float is roughly the size of a child or a beach ball, NEVER the size of an adult+arms.
- For a typical 5m×3m pool, the float should appear barely 1m in diameter — like a small playful accent in a corner, NOT a centerpiece.
- ❌ FORBIDDEN : a giant float occupying a third of the pool. That looks fake and ruins the photo. A small natural float in a corner is INFINITELY better than a giant one centered.
- Mental visual test BEFORE finalizing : compare the float to the visible loungers AND to the smallest visible step/ladder. Float ≥ size of a lounger = WRONG, downscale immediately to half that.

🌊 Realistic INTEGRATION with the water:
- Subtle wake / ripple around it
- Slight reflection of the float's underside on the water
- Float partially sitting on water, NOT floating dry above it

☀️ Realistic LIGHTING : the float must receive the same sunlight direction as the rest of the scene; cast a soft natural shadow on the water consistent with the existing shadow direction.

🎨 STYLE : color/style remain PHOTOREALISTIC — no over-saturated CGI candy palette. Slight wear/dust is fine.

🚫 IF YOU CANNOT add the float naturally according to ALL the rules above → DO NOT add it. Return the image UNCHANGED. A photo without a float is always acceptable. A bad fake float ruins the photo.

🚫 FRAMING LOCK : DO NOT zoom in/out, DO NOT crop, DO NOT change the camera angle, focal length, or any pixel of the image OTHER than the small region where the float sits.

🚫 STRUCTURAL PRESERVATION : every other pixel of the image MUST remain pixel-identical to the input. Same architecture, same walls, same plants, same furniture, same humans (if any present in original), same lighting, same color grading, same shadows except the new one cast by the float.

NEGATIVE PROMPT:
- new humans, new people, hands, legs, body parts
- new furniture, new objects beyond the single float
- 🚨 new raft, platform, daybed, support UNDER or BESIDE the float (most common failure mode)
- 🚨 second auxiliary object accompanying the float (= invented furniture)
- duplicated floats, multiple floats, more than one float
- 🚨 oversized float covering > 10% of water surface (banned even if "centerpiece" look is tempting)
- 🚨 float larger than a single visible lounger/daybed (always downscale)
- 🚨 3D perspective float on a top-down aerial photo
- changes to framing, composition, perspective, water shape, decking
- cartoon / CGI look, oversaturated colors, plastic shine, glowing edges
- floating dry above water without surface contact
"""


# === Suppression clutter (parasites : objets, mais aussi équipements techniques visibles) ===
# Note 12/05/2026 : on garde le mega-prompt comme TEMPLATE GÉNÉRIQUE (fallback si pas
# d'analyse Gemini), MAIS le builder build_remove_clutter_prompt() ci-dessous le préfixe
# avec la liste EXPLICITE des éléments clutter_to_remove identifiés par Gemini Vision
# pour CETTE photo précise. Sans ça, Gemini Image ne sait pas quoi retirer concrètement
# (cf. bouée de sauvetage rouge identifiée mais pas retirée).

def build_remove_clutter_prompt(clutter_list: list[str] | None = None) -> str:
    """Construit un prompt clutter ciblé sur les éléments précis listés par Gemini Vision.

    Si la liste est vide → fallback sur le prompt générique uniquement.
    Si la liste a des entrées → on les met EN TÊTE du prompt avec "REMOVE EXACTLY THESE".
    """
    targeted_block = ""
    if clutter_list:
        items = "\n".join(f"  {i+1}. {desc}" for i, desc in enumerate(clutter_list))
        targeted_block = f"""🎯 EXPLICIT TARGETED CLEANUP — REMOVE EXACTLY THESE ELEMENTS (identified by a prior vision pass on this exact photo) :

{items}

These are the PRIMARY removal targets for this image. Find them, remove them, and reconstruct the area underneath/behind them (water surface, wall texture, plant continuation, tile pattern, sky, etc.) seamlessly. After removing these, also look for any additional clutter from the generic list below — but the explicit items above MUST be removed first.

If an item in the list above is ambiguous, prefer LEAVING it rather than removing the wrong thing — false positives are worse than missed clutter.

"""
    return targeted_block + _PROMPT_REMOVE_CLUTTER_TEMPLATE


_PROMPT_REMOVE_CLUTTER_TEMPLATE = """🛑 ADDITION-FREE RULE (#1, MOST IMPORTANT) :
This task is REMOVAL ONLY. You are NEVER allowed to ADD anything to the image — no people, no furniture, no plants, no birds, no clouds, no construction equipment (cranes, scaffolding, trucks, vehicles), no signs, no text, no shadows, no decoration. NOTHING NEW.
You may ONLY remove existing visible clutter elements and replace the area with what was naturally behind/under them (sky, wall, floor, fabric).

If you find yourself wanting to "improve" the scene by adding anything → STOP. The improvement is exactly what we're avoiding. Just remove and reconstruct what was there.

Remove non-aspirational clutter and technical eyesores from this image while keeping the scene EXACTLY identical otherwise.

REMOVE (if visible) — anything that breaks a premium editorial feel:

Loose objects:
- Electrical cables, wires, exposed pipes, sockets, plugs on walls or floor
- Forgotten items (cups, water bottles, used glasses, crumpled towels, trash, plastic bags, beach toys, plastic toys, sand buckets/spades, beach balls, kid floats abandoned on furniture)
- Personal belongings left behind (piled sandals/flip-flops, scattered clothing, open beach bags, sunscreen bottles)
- Unsightly signage, posters, "out of order" notices, plastic A-boards, price tags
- Construction-site hazard tape, plastic safety cones, temporary site barriers, hose lying on the ground, fire-extinguisher boxes on a wall
  ⛔ DO NOT TOUCH permanent safety barriers : glass/metal/wire pool fences, spa safety fences, balcony or rooftop railings, terrace balustrades — these are LEGALLY REQUIRED and removing them ruins the photo (it makes the pool look unsafe = legal red flag). Permanent pool/balcony/rooftop fences and railings ALWAYS STAY. Same for pool steps, pool ladders, handrails of stairs.

Technical / structural eyesores (accept the small risk of bavure):
- Visible surveillance cameras / CCTV (mounted on poles, walls, ceilings)
- Fire-escape staircases visible on neighbouring buildings, fire-escape doors
- Antennas, satellite dishes, telecom poles
- Outdoor air-conditioner units, AC compressors, vents, ventilation grilles, gutter pipes, downspouts (only the obvious eyesore ones — keep architectural details)
- Ugly handrails painted in non-brand colors (plain galvanized steel, etc.) — replace with discreet matching railing if removable risk too high
- Drainage covers, manholes when in plain sight in a key area

Third-party brand logos / sponsorship markings (these break editorial neutrality):
- Brand logos visible on parasols, umbrellas, cushions, towels, signage, drink coasters
- Sponsorship branding on cabanas, chair backs, beach toys
- Restaurant / hotel brand markings other than the venue's own subtle signage
Replace the logo area with a neutral matching color/texture (the parasol's main color, the cushion fabric pattern, etc.). KEEP the parasol/cushion/towel itself — only remove the logo printed on it.

KEEP EXACTLY IDENTICAL:
- All hospitality furniture (loungers, daybeds, parasols, tables, chairs, sofas)
- All structures, lighting fixtures, plants, water, sky, architecture proper
- **Permanent safety fences / railings** : pool fences (glass, metal, wire mesh, wooden), spa enclosure fences, balcony railings, rooftop balustrades, terrace handrails, pool ladders, pool steps, pool grab-rails. Even if they look "industrial" or "ugly" — they MUST stay. Their absence makes the photo unusable (safety liability).
- **Decorative inflatable pool floats** floating in the water (flamingo, unicorn, donut, pineapple, watermelon, swan, avocado, pastel ring, etc.). These are the EXACT type of "instagrammable playful" element Dayuse strategy actively ADDS to other photos. Removing them defeats the brand intent. Keep them. Only exception : a clearly broken / deflated / dirty float lying on the deck (not in water) can be removed.
- Any food/drinks SERVED on a dining table (cocktail, plate of food → keep; abandoned dirty glass on a lounger → remove)
- All people present in the scene

Reconstruct the underlying surface (sand, tile, wood, fabric, wall, sky) seamlessly where the clutter was. If a structural eyesore is too embedded to remove cleanly, leave it rather than create a glitch.

Photorealistic editorial lifestyle photography. The result must look like a professional cleanup crew passed and the technical building services had been hidden — same scene, just polished.

NEGATIVE PROMPT (HARD avoid):
- ANY new element added to the scene (cranes, scaffolding, trucks, vehicles, construction, birds, people, plants, decorations, signs, text overlays)
- "improvements" that go beyond cleaning (do NOT add a sky, do NOT add clouds, do NOT add greenery)
- removed furniture, altered architecture, missing decor, ghost outlines, blurred patches, CGI artifacts, structural deformation
- **removed pool fence / safety railing / glass pool barrier / balcony railing / rooftop balustrade / pool ladder / pool steps / handrail** (these are mandatory safety elements — never erase them, even partially)
- removed served food or cocktails on a dining table, removed people
- duplicated parts of the scene (a wall section pasted twice, water duplicated)
- color shifts in regions that were not edited"""


# Alias rétro-compat : si du code appelle encore PROMPT_REMOVE_CLUTTER en variable simple
# (sans la liste targetted), il aura le template générique. Mais TOUS les call sites
# devraient passer par build_remove_clutter_prompt(clutter_to_remove) désormais.
PROMPT_REMOVE_CLUTTER = _PROMPT_REMOVE_CLUTTER_TEMPLATE


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SCENARIOS DÉTERMINISTES (Martin 12/05/2026)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Avant : le prompt persona donnait 4-6 "PLACEMENT OPTIONS" à Gemini Image
# qui choisissait — d'où la dérive observée (transat flottant inventé,
# couple debout dans piscine sans fond, etc.).
# Après : on choisit UN scenario unique en Python via pick_human_scenario(),
# en se basant sur les safe_zones Gemini Vision. Le prompt envoyé à Nano
# Banana décrit UNE situation précise sans alternative. Plus de latitude
# créative → résultats reproductibles, conformes brand.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _classify_safe_zone(zone_text: str) -> str:
    """Mappe une description textuelle de safe_zone Gemini vers un type de scenario.

    Returns: l'un de {"in_water", "pool_edge", "lounger", "cabana_daybed",
                       "dining_table", "rooftop_deck", "outdoor_deck",
                       "indoor_seating",
                       "cardio_machine", "weight_bench", "weights_area", "gym_mat",
                       "unknown"}

    (Martin 13/05/2026 v3) : ajout reconnaissance FR + équipements gym spécifiques
    (tapis de course, banc muscu, haltères) pour éviter le bug "yoga sur tapis de
    course" — auparavant tout terminait en zone_type='gym_mat' (= yoga par défaut).
    Si AUCUN match précis, on retourne 'unknown' → le scenario AUTO sera utilisé.
    """
    z = (zone_text or "").lower()
    # ── EAU / PISCINE (priorité haute) ──
    if any(k in z for k in ["in the pool water", "in the water", "in the pool", "pool water",
                              "swimming", "submerged", "wading in",
                              # FR
                              "dans la piscine", "dans l'eau", "en train de nager", "en nageant"]):
        return "in_water"
    if any(k in z for k in ["pool edge", "pool rim", "rim of the pool", "edge of the pool",
                              "sitting at the edge", "edge of pool",
                              # FR
                              "rebord de la piscine", "bord de la piscine", "rebord de piscine",
                              "bord de piscine", "lèvre de la piscine"]):
        return "pool_edge"
    # ── MOBILIER OUTDOOR PISCINE ──
    if any(k in z for k in ["lounger", "sun lounger", "sunbed", "sun bed", "deck chair",
                              "transat", "chaise longue"]):
        return "lounger"
    if any(k in z for k in ["cabana", "daybed", "day bed", "pool bed", "lit de jour"]):
        return "cabana_daybed"
    # ── F&B ──
    if any(k in z for k in ["dining table", "restaurant table", "around the table",
                              "at the table", "bar counter",
                              # FR
                              "table à manger", "à la table", "comptoir du bar"]):
        return "dining_table"
    # ── ROOFTOP ──
    if any(k in z for k in ["rooftop", "roof terrace", "skydeck", "rooftop deck"]):
        return "rooftop_deck"
    # ── GYM — disambiguation FINE (Martin 13/05/2026 — bug yoga sur tapis course) ──
    # On vérifie d'abord les équipements SPÉCIFIQUES (tapis course, vélo, banc muscu)
    # AVANT le générique "gym mat" qui couvre uniquement yoga.
    if any(k in z for k in ["treadmill", "running mat", "cardio machine", "elliptical",
                              "stationary bike", "rowing machine",
                              # FR
                              "tapis de course", "tapis course", "vélo elliptique",
                              "vélo stationnaire", "rameur", "vélo d'appartement"]):
        return "cardio_machine"
    if any(k in z for k in ["weight bench", "bench press", "weight bench",
                              # FR
                              "banc de musculation", "banc musculation", "banc de muscu"]):
        return "weight_bench"
    if any(k in z for k in ["dumbbell rack", "dumbbells", "free weights area", "weights rack",
                              # FR
                              "haltères", "rack à haltères", "zone haltères"]):
        return "weights_area"
    if any(k in z for k in ["yoga mat", "yoga", "stretching mat", "stretching area",
                              # FR
                              "tapis de yoga", "tapis yoga", "espace stretching"]):
        return "gym_mat"
    # ── INTÉRIEUR / SEATING ──
    if any(k in z for k in ["sofa", "armchair", "lounge chair", "bench", "indoor seat",
                              "wicker chair", "rattan chair",
                              # FR
                              "canapé", "fauteuil", "siège intérieur", "fauteuil en osier",
                              "fauteuil en rotin"]):
        return "indoor_seating"
    # ── OUTDOOR DECK générique (dernier filet avant unknown) ──
    if any(k in z for k in ["deck", "patio", "terrace", "ground", "floor",
                              # FR
                              "sol", "dalle", "carrelage", "zone carrelée",
                              "terrasse", "plancher"]):
        return "outdoor_deck"
    return "unknown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IDENTITÉS RÉCURRENTES — California influencer beach aesthetic (Martin
# 12/05/2026, niveau 7-8/10 sur l'échelle sexy : aspirational confident,
# jamais aguicheur, jamais lingerie/sous-vêt.).
# Inspiration : prompts JSON UGC model (Reformation / Solid&Striped /
# Mediterranean influencer summer aesthetic).
# Ces strings sont inlinées dans chaque scenario pour rester self-contained
# côté Gemini Image (qui aime les prompts denses sans variables externes).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Look California sun-kissed influencer (femme adulte 22-28)
_CA_WOMAN = (
    "She has a slim toned hourglass figure with soft feminine curves, deep golden "
    "Californian/Mediterranean tan, radiant sun-kissed glowing skin (natural radiance, "
    "no contouring), medium-long sun-bleached beachy blonde hair in loose tousled waves "
    "with casual middle part, heart-shaped face with defined cheekbones, warm hazel or "
    "ocean blue eyes, full plump lips with glossy nude balm. Soft natural \"beach glam\" "
    "makeup : sheer bronzer, peachy blush, mascara only, glossy nude lips. Layered fine "
    "gold chain necklaces, small gold hoop earrings. Confident relaxed expression with a "
    "soft natural smile."
)

# Look California sun-kissed (homme adulte 25-32)
_CA_MAN = (
    "He has a lean athletic build with toned shoulders and light defined abs, deep golden "
    "Mediterranean tan, casual tousled medium-brown hair, soft natural stubble, warm brown "
    "eyes, confident relaxed expression with a soft natural smile. Minimal jewelry : "
    "single thin gold chain around the neck."
)

# Look family vacation sun-kissed (parents + 1 enfant) — version adoucie, pas influencer
_FAMILY_LOOK = (
    "Both parents are lean and toned with a healthy Mediterranean golden tan, soft natural "
    "smiles, late 20s. The mother has medium-long sun-bleached wavy hair, the father has "
    "casual tousled brown hair with light stubble. Child age 6-7 with natural happy energy, "
    "wind-tousled hair, sun-kissed cheeks. No heavy jewelry on parents, no makeup on the "
    "child. Real family vacation vibe — never staged."
)

# Catalogue des scenarios. Chaque entrée = (persona, zone_type) → bloc texte précis.
# Le bloc DOIT décrire UNE seule pose, position, attribut. Pas de "ou", pas de "(1)/(2)/(3)".
# Termes anglais car Gemini Image y répond mieux en pratique.
_SCENARIO_CATALOG: dict[tuple[str, str], str] = {

    # ━━ COUPLES (California influencer aesthetic 7-8/10) ━━━━━━━━━━━━━━━━━
    ("couples", "in_water"): (
        f"Place exactly TWO subjects in the existing pool water — a young adult couple "
        f"(one woman 24-26, one man 26-28). "
        f"WOMAN : {_CA_WOMAN} She wears a chic sleek black bandeau bikini (or terracotta "
        f"if pool tones already cool) — strapless, modern silhouette. "
        f"MAN : {_CA_MAN} He wears classic tailored swim shorts in matching neutral tone "
        f"(cream / navy / olive). "
        f"POSE : Both are STANDING in chest-deep water near the visible center of the pool. "
        f"Water level on both : sternum / upper-chest (only upper torso, shoulders, neck and "
        f"head above water — belly button, hips, thighs MUST be fully submerged). They face "
        f"each other in soft three-quarter profile to camera, sharing a candid laughing moment "
        f"mid-conversation. Their hands are clasped between them at chest height in the water. "
        f"Hair slightly wet at temples for her, water droplets on his shoulders. Soft "
        f"concentric water ripples spreading around both bodies. They do NOT look at the camera."
    ),
    ("couples", "pool_edge"): (
        f"Place exactly TWO subjects sitting DIRECTLY on the existing bare pool deck (concrete / "
        f"tile / wood — exact same material as input) right at the pool edge — "
        f"a young adult couple (one woman 24-26, one man 26-28). "
        f"WOMAN : {_CA_WOMAN} She wears a chic high-waisted bikini set in soft terracotta or "
        f"cream (full coverage bottoms, modern bandeau or triangle top). "
        f"MAN : {_CA_MAN} He wears tailored cream swim shorts, no shirt. "
        f"POSE : They sit side by side with feet and calves submerged in the pool water "
        f"(water at mid-calf). Their HANDS rest flat behind them ON THE BARE DECK for support "
        f"(no cushion, no towel, no mat under them — body weight rests directly on the concrete/tile). "
        f"Both lean slightly toward each other, sharing a candid laughing moment. He holds a "
        f"tall iced drink with citrus slice in his outer hand. A pair of sleek tortoise-shell "
        f"sunglasses rests on her head. Soft water reflections shimmer on their lower legs. "
        f"Neither looks at the camera ; they look at each other.\n"
        f"🚫 DO NOT add any cushion, towel, bench, mat, or extra surface under them. DO NOT "
        f"extend the deck or invent a step. DO NOT modify the pool shape."
    ),
    ("couples", "lounger"): (
        f"Place exactly TWO subjects on two adjacent existing sun loungers visible in the "
        f"photo — a young adult couple (one woman 24-26, one man 26-28). "
        f"WOMAN : {_CA_WOMAN} She wears a chic olive or cream high-cut one-piece swimsuit "
        f"(modern silhouette, NOT racy — elegant), oversized straw-brimmed hat resting on her "
        f"lap, sleek dark sunglasses on. "
        f"MAN : {_CA_MAN} He wears navy tailored swim shorts, no shirt, sleek aviator "
        f"sunglasses. "
        f"POSE : The woman reclines comfortably on the lounger closest to camera, propped on "
        f"a flat cushion, one knee slightly bent, reading a slim hardback book held in both "
        f"hands with a relaxed half-smile. The man reclines on the adjacent lounger, propped "
        f"on one elbow facing slightly away, looking out at the scene. They do NOT touch ; "
        f"calm confident relaxed energy. Neither looks at the camera."
    ),
    ("couples", "cabana_daybed"): (
        f"Place exactly TWO subjects together on the existing cabana daybed / pool sofa "
        f"visible in the photo — a young adult couple (one woman 24-26, one man 26-28). "
        f"WOMAN : {_CA_WOMAN} She wears a chic terracotta high-waist bikini with a light "
        f"cream open sarong loosely tied at her hips, sleek sunglasses. "
        f"MAN : {_CA_MAN} He wears tailored swim shorts, no shirt, aviator sunglasses. "
        f"POSE : They sit close together — the woman cross-legged leaning her shoulder "
        f"against him, both gazing out at the pool. He holds a tall iced cocktail with "
        f"citrus in one hand resting on his knee. A crochet tote bag in natural straw "
        f"color rests on the daybed beside them. Soft mid-day shadow under the cabana canopy. "
        f"Neither looks at the camera ; they share a quiet candid moment."
    ),
    ("couples", "dining_table"): (
        f"Place exactly TWO subjects around the existing dining table visible in the photo — "
        f"a young adult couple (one woman 24-26, one man 26-28) sitting across from each "
        f"other. "
        f"WOMAN : {_CA_WOMAN} She wears a chic cream linen midi dress with thin straps. "
        f"MAN : {_CA_MAN} He wears a relaxed open linen shirt in oat / sand tone, sleeves "
        f"casually rolled. "
        f"POSE : The woman is mid-laugh, hand gesturing softly. The man leans slightly "
        f"forward, attentive, soft smile. One existing wine glass and one water glass on the "
        f"table only. They are clearly mid-conversation, NOT looking at the camera."
    ),
    ("couples", "rooftop_deck"): (
        f"Place exactly TWO subjects standing on the rooftop deck near (on the safe interior "
        f"side of) the existing railing — a young adult couple (one woman 24-26, one man "
        f"26-28). "
        f"WOMAN : {_CA_WOMAN} She wears a flowy cream silk slip dress reaching mid-thigh, "
        f"slim heeled sandals. "
        f"MAN : {_CA_MAN} He wears an open cream linen shirt with tailored sand chino shorts, "
        f"clean white sneakers. "
        f"POSE : They stand close, her shoulder against his arm, both gazing out at the "
        f"horizon / city skyline (NOT at camera). He holds a sleek cocktail glass with "
        f"clear ice and citrus in his outer hand. Natural late-afternoon golden warm light "
        f"hits their profiles, slight golden hour glow."
    ),
    ("couples", "outdoor_deck"): (
        f"Place exactly TWO subjects standing casually on the existing outdoor deck — a "
        f"young adult couple (one woman 24-26, one man 26-28). "
        f"WOMAN : {_CA_WOMAN} She wears a flowy cream or terracotta linen short dress, "
        f"crochet tote bag in straw color slung over one shoulder. "
        f"MAN : {_CA_MAN} He wears an open linen shirt in oat tone, tailored sand chino "
        f"shorts, clean sneakers. "
        f"POSE : They face each other in soft profile to camera, mid-conversation. She "
        f"holds a takeaway coffee cup in one hand with a soft laugh. Natural golden warm "
        f"light. Neither looks at the camera."
    ),
    ("couples", "indoor_seating"): (
        f"Place exactly TWO subjects on the existing sofa or lounge chair visible in the "
        f"photo — a young adult couple (one woman 24-26, one man 26-28). "
        f"WOMAN : {_CA_WOMAN} She wears casual smart attire : silk cream camisole and slim "
        f"high-waist linen trousers. "
        f"MAN : {_CA_MAN} He wears a relaxed crew neck tee in oat color and slim tailored "
        f"trousers. "
        f"POSE : The woman sits cross-legged at one end of the sofa, scrolling on her phone "
        f"with a soft half-smile. The man sits at the other end facing her, propped on one "
        f"elbow, glancing toward her with a relaxed smile. Neither looks at the camera."
    ),

    # ━━ SOLOS (1 femme adulte, California influencer 7-8/10) ━━━━━━━━━━━━━
    ("solos", "in_water"): (
        f"Place exactly ONE subject in the existing pool water — a young adult woman 24-26. "
        f"LOOK : {_CA_WOMAN} She wears a sleek black or terracotta one-piece swimsuit "
        f"(modern silhouette, deep scoop neckline, NOT racy). "
        f"POSE : She is swimming gentle breaststroke in the center of the visible water "
        f"surface, her head above water (chin level), arms making soft swim motion with "
        f"slight wake behind her. Wet hair slicked back, droplets glistening on her shoulders. "
        f"Soft mid-day golden sun on her tanned shoulders. She does NOT look at the camera ; "
        f"her gaze is directed slightly ahead along the water surface."
    ),
    ("solos", "pool_edge"): (
        f"Place exactly ONE subject sitting DIRECTLY on the existing bare pool deck (concrete / "
        f"tile / wood — exact same material as input) right at the pool edge — a young adult "
        f"woman 24-26. LOOK : {_CA_WOMAN} She wears a chic cream or terracotta high-waist bikini "
        f"set (modern bandeau top + full-coverage bottoms). Sleek tortoise-shell sunglasses "
        f"pushed up on her head. "
        f"POSE : She sits on the BARE pool deck with legs dangling in the water (water at "
        f"mid-calf). One hand rests flat BEHIND HER ON THE BARE DECK for support (no cushion, no "
        f"towel underneath), the other hand holds a cold drink with citrus. Soft confident "
        f"half-smile, gaze toward the water (NOT at camera). Soft water reflection shimmering "
        f"on her lower legs. A natural straw crochet tote bag rests beside her on the deck.\n"
        f"🚫 DO NOT add any cushion, towel, bench, mat, or extra surface under her. DO NOT "
        f"extend the deck or invent a step. DO NOT modify the pool shape."
    ),
    ("solos", "lounger"): (
        f"Place exactly ONE subject on the existing sun lounger visible in the photo — a "
        f"young adult woman 24-26. LOOK : {_CA_WOMAN} She wears a chic olive or cream "
        f"high-cut one-piece swimsuit (elegant modern silhouette, NOT racy), wide-brimmed "
        f"straw sun hat on her lap, sleek dark sunglasses. "
        f"POSE : She reclines comfortably on the lounger, propped slightly up on a flat "
        f"cushion, one knee bent. Slim hardback book held open in one hand, the other hand "
        f"resting on her thigh. Soft confident half-smile, gaze on the book (NOT at camera). "
        f"Natural mid-afternoon golden sun on her tanned body, soft glowing skin."
    ),
    ("solos", "cabana_daybed"): (
        f"Place exactly ONE subject on the existing cabana daybed visible in the photo — a "
        f"young adult woman 24-26. LOOK : {_CA_WOMAN} She wears a chic terracotta bikini with "
        f"a light cream open sarong loosely tied at her hips, sleek sunglasses. "
        f"POSE : She sits cross-legged with her back against the cushions, holding a tall "
        f"iced drink in one hand. The other hand rests gracefully on her knee. Her face is "
        f"turned slightly toward the pool with a soft confident smile (NOT at camera). A "
        f"natural straw crochet tote bag rests at her side."
    ),
    ("solos", "rooftop_deck"): (
        f"Place exactly ONE subject standing on the rooftop deck (on the safe interior side "
        f"of the existing railing) — a young adult woman 24-26. LOOK : {_CA_WOMAN} She wears "
        f"a flowy cream silk slip dress reaching mid-thigh, slim heeled sandals. "
        f"POSE : She holds a sleek cocktail glass with clear ice and citrus in one hand, the "
        f"other hand resting lightly on the railing. She gazes out at the city skyline / "
        f"horizon (profile to camera, NOT at camera). Confident serene expression. Natural "
        f"late-afternoon golden hour warm light glowing on her tanned side and hair."
    ),
    ("solos", "outdoor_deck"): (
        f"Place exactly ONE subject standing casually on the existing outdoor deck — a "
        f"young adult woman 24-26. LOOK : {_CA_WOMAN} She wears a flowy cream or terracotta "
        f"short linen dress, natural straw crochet tote bag slung over one shoulder. "
        f"POSE : She holds a takeaway coffee cup in one hand, gazing out at the scene with "
        f"a soft confident smile (profile to camera, NOT at camera). Natural golden hour "
        f"warm light."
    ),
    ("solos", "indoor_seating"): (
        f"Place exactly ONE subject on the existing sofa or lounge chair visible in the "
        f"photo — a young adult woman 24-26. LOOK : {_CA_WOMAN} (slightly more muted makeup "
        f"for the indoor setting — still glowing tan and lips). She wears casual smart attire "
        f": silk cream camisole and slim high-waist linen trousers. "
        f"POSE : She sits cross-legged at one end of the sofa, an open laptop on her lap, "
        f"focused half-smile gazing at the screen. A natural-toned ceramic coffee cup rests "
        f"on the nearby existing table (only if one is clearly visible in the input). She "
        f"does NOT look at the camera."
    ),
    ("solos", "gym_mat"): (
        f"Place exactly ONE subject on the existing yoga mat / gym floor visible in the "
        f"photo — a young adult woman 24-26 in a graceful warrior-II yoga pose (NOT downward "
        f"dog — too revealing). She has the same {_CA_WOMAN.replace(' Confident relaxed', ' Focused calm').replace('soft natural smile', 'serene neutral expression')} "
        f"She wears matching premium athleisure (high-waist black or sage leggings and a "
        f"fitted scoop-neck sports bra). Natural studio light on her toned tanned body, soft "
        f"glowing skin. She does NOT look at the camera ; gaze focused along her front arm."
    ),
    # ━━ GYM ÉQUIPEMENTS SPÉCIFIQUES (Martin 13/05/2026 — fix bug yoga sur tapis course) ━━
    ("solos", "cardio_machine"): (
        f"Place exactly ONE subject ON the existing treadmill / running machine visible in the "
        f"photo — a young adult woman 24-26 IN MOTION of running at moderate pace, both feet "
        f"in mid-stride on the running belt, hands holding the front rail lightly, looking "
        f"FORWARD (not at camera). She has the same {_CA_WOMAN.replace(' Confident relaxed', ' Focused energetic').replace('soft natural smile', 'concentrated expression')} "
        f"She wears matching premium athleisure (high-waist black leggings, fitted sports bra "
        f"or athletic tank top, clean white running sneakers — running shoes are MANDATORY on "
        f"the treadmill, NEVER barefoot). Earbuds in ears optional. Natural studio gym light. "
        f"NEVER place her doing yoga / stretching / standing still on the machine — she must "
        f"be ACTIVELY using the treadmill (running stride, contact with the belt)."
    ),
    ("solos", "weight_bench"): (
        f"Place exactly ONE subject ON the existing weight bench visible in the photo — a "
        f"young adult woman 24-26 SITTING UPRIGHT on the bench, holding light dumbbells (one "
        f"in each hand) at shoulder height in a controlled shoulder-press position, gaze "
        f"forward and focused (not at camera). She has the same {_CA_WOMAN.replace(' Confident relaxed', ' Focused energetic').replace('soft natural smile', 'concentrated expression')} "
        f"She wears premium athleisure (high-waist black or sage leggings, fitted sports bra "
        f"or athletic tank top, clean training sneakers). Natural studio gym light, slight "
        f"sheen on her toned shoulders. NEVER place her doing yoga or stretching on the bench."
    ),
    ("solos", "weights_area"): (
        f"Place exactly ONE subject standing in the existing free-weights area visible in the "
        f"photo — a young adult woman 24-26 reaching for / picking up a dumbbell from the rack "
        f"with one hand, body slightly turned in profile, focused expression (not at camera). "
        f"She has the same {_CA_WOMAN.replace(' Confident relaxed', ' Focused energetic').replace('soft natural smile', 'concentrated expression')} "
        f"She wears premium athleisure (high-waist black leggings, fitted sports bra or tank "
        f"top, clean training sneakers). Natural studio gym light. NEVER place her doing yoga, "
        f"standing on the dumbbells, or lying on the floor — she is actively selecting weights."
    ),

    # ━━ FAMILIES (couple + 1-2 enfants, sun-kissed family vacation) ━━━━━━━
    # Tone adouci vs influencer : pas de gold chains layered, pas de makeup
    # heavy. Vrai look "famille en vacances Méditerranée".
    ("families", "in_water"): (
        f"Place exactly THREE subjects in the existing pool water — a young family. "
        f"{_FAMILY_LOOK} Mother wears a sleek terracotta or olive one-piece swimsuit, "
        f"father wears tailored navy swim shorts (no shirt), child wears a bright colorful "
        f"kids' swimsuit. "
        f"POSE : Both parents stand chest-deep (water at sternum on adults). The mother "
        f"holds the child in front of her at the water surface, helping the child gently "
        f"splash and laugh with a wide candid smile. The father stands close to them, soft "
        f"natural smile, one hand lightly resting on the mother's shoulder. None look at "
        f"the camera ; their gaze is on the child / between each other. Natural mid-day "
        f"warm sun."
    ),
    ("families", "pool_edge"): (
        f"Place exactly THREE subjects at the pool edge, ALL sitting/kneeling DIRECTLY on the "
        f"existing bare pool deck (concrete / tile / wood — exact same material as input) — a "
        f"young family. {_FAMILY_LOOK} Mother wears a chic cream high-waist bikini (modern, "
        f"modest), father wears tailored swim shorts in oat tone, child wears bright kids' "
        f"swimwear. "
        f"POSE : Mother sits on the BARE pool deck with her feet in the water, holding the "
        f"child's hand who sits beside her with a wide laughing smile. Father kneels next to "
        f"them on the BARE DECK, smiling at the child. None look at the camera. A natural "
        f"straw beach bag rests on the deck.\n"
        f"🚫 DO NOT add any cushion, towel, bench, mat, or extra surface under them. DO NOT "
        f"extend the deck or invent a step. DO NOT modify the pool shape."
    ),
    ("families", "lounger"): (
        f"Place exactly THREE subjects on existing sun loungers visible in the photo — a "
        f"young family. {_FAMILY_LOOK} All wear casual modern swimwear. "
        f"POSE : Mother reclines on one lounger, sleek sunglasses on, soft natural smile "
        f"toward the child age 6 who is sitting up at her feet showing her a colorful "
        f"inflatable beach toy. Father reclines on the adjacent lounger, propped on one "
        f"elbow, soft smile watching them. Natural mid-afternoon warm sun on tanned skin. "
        f"None look at the camera."
    ),
    ("families", "cabana_daybed"): (
        f"Place exactly THREE subjects together on the existing cabana daybed — a young "
        f"family. {_FAMILY_LOOK} Mother in chic terracotta bikini with cream sarong over "
        f"hips, father in tailored swim shorts and a light open cream linen shirt, child "
        f"in bright kids' swimwear. "
        f"POSE : Father at one end, mother at the other end, the child age 6 sitting "
        f"between them with a wide smile, showing the parents a small toy or shell. All "
        f"mid-laugh, real candid family moment. None looks at the camera."
    ),
    ("families", "dining_table"): (
        f"Place exactly THREE subjects around the existing dining table — a young family. "
        f"{_FAMILY_LOOK} Mother in a flowy cream linen short dress, father in a relaxed "
        f"open linen shirt in oat tone, child in casual sun-kissed summer attire. "
        f"POSE : Mother at one side passing a small dish to the child age 7 sitting across "
        f"from her, soft natural laugh. Father next to the child, mid-conversation with a "
        f"warm smile. Existing wine/water glasses on the table only. They share a candid "
        f"laughing meal moment. None looks at the camera."
    ),
    ("families", "outdoor_deck"): (
        f"Place exactly THREE subjects on the existing outdoor deck — a young family. "
        f"{_FAMILY_LOOK} Mother in a flowy cream short linen dress, father in linen shirt "
        f"and tailored sand chino shorts, child in casual summer clothes. "
        f"POSE : All casually standing close, the child age 6 between the parents, all "
        f"smiling at something just out of frame (off-camera). Natural golden warm light. "
        f"None looks at the camera."
    ),

    # ━━ SMALL_GROUPS (3 amis trendy California influencer) ━━━━━━━━━━━━━━
    ("small_groups", "in_water"): (
        f"Place exactly THREE subjects in the existing pool water — three young adult friends "
        f"(two women 24-26, one man 26-28). Both women have the look : {_CA_WOMAN} The man "
        f"has the look : {_CA_MAN} "
        f"Outfits : woman 1 in a sleek black bandeau bikini, woman 2 in a chic terracotta "
        f"high-waist bikini, the man in tailored cream swim shorts. "
        f"POSE : All three stand chest-deep (water at sternum) near the center of the pool, "
        f"forming a loose triangle, mid-laugh in conversation. Hair slightly wet at temples "
        f"for both women. Soft concentric water ripples around their bodies. None look at "
        f"the camera ; they look at each other / off-frame."
    ),
    ("small_groups", "pool_edge"): (
        f"Place exactly THREE subjects sitting in a row DIRECTLY on the existing bare pool deck "
        f"(concrete / tile / wood — exact same material as input) at the pool edge — three "
        f"young adult friends (two women 24-26, one man 26-28). Both women look : "
        f"{_CA_WOMAN} Man : {_CA_MAN} "
        f"Outfits : woman 1 in cream high-waist bikini set, woman 2 in olive bandeau bikini, "
        f"man in tailored navy swim shorts (no shirt). Sleek sunglasses on all three. "
        f"POSE : They sit side by side ON THE BARE DECK, feet and calves in the water, hands "
        f"resting flat behind them on the BARE concrete/tile for support (no cushion, no towel "
        f"under them). The friend in the center holds an iced cocktail with citrus, telling a "
        f"story while the others laugh with confident soft smiles. Natural straw crochet tote "
        f"bag visible on the deck. None look at the camera.\n"
        f"🚫 DO NOT add any cushion, towel, bench, mat, or extra surface under them. DO NOT "
        f"extend the deck or invent a step. DO NOT modify the pool shape."
    ),
    ("small_groups", "lounger"): (
        f"Place THREE subjects on EXISTING sun loungers visible in the photo — three young "
        f"adult friends (two women 24-26, one man 26-28). Women look : {_CA_WOMAN} Man : "
        f"{_CA_MAN} "
        f"⚠️ ADAPTIVE PLACEMENT — count the EMPTY loungers actually visible in the input :\n"
        f"  • If 3+ adjacent loungers visible : place all 3 subjects there, reclined.\n"
        f"  • If only 2 loungers visible : place 2 subjects reclining, the 3rd standing "
        f"casually beside them holding a cocktail.\n"
        f"  • If only 1 lounger visible : place 1 subject reclining, 2 standing beside or "
        f"sitting on the deck nearby. DO NOT invent additional loungers.\n"
        f"  • If 0 lounger visible (only deck/water) : DO NOT use this scenario — return "
        f"image unchanged.\n"
        f"Outfits : woman 1 in cream high-cut one-piece, woman 2 in terracotta bandeau "
        f"bikini, man in tailored navy swim shorts. Sleek sunglasses on all, wide-brimmed "
        f"straw hat on the center lounger if present. "
        f"POSE : Reclined subjects propped on one elbow, standing subjects in relaxed contrapposto "
        f"holding a cocktail with citrus. Natural mid-afternoon golden sun on their tanned bodies. "
        f"None look at the camera."
    ),
    ("small_groups", "cabana_daybed"): (
        f"Place exactly THREE subjects on the existing cabana daybed / large pool sofa — "
        f"three young adult friends (two women 24-26, one man 26-28). Women look : "
        f"{_CA_WOMAN} Man : {_CA_MAN} "
        f"Outfits : modern chic swimwear with light open cream linen shirts loosely worn "
        f"over for the women, tailored swim shorts and open linen shirt for the man. Sleek "
        f"sunglasses on all. "
        f"POSE : They sit close, mid-laugh in candid conversation. Existing cocktail glasses "
        f"in their hands only if a tray / glasses are clearly visible in the input. A natural "
        f"straw crochet tote bag rests at one corner of the daybed. None looks at the camera."
    ),
    ("small_groups", "dining_table"): (
        f"Place exactly THREE subjects around the existing dining table — three young adult "
        f"friends (two women 24-26, one man 26-28). Women look : {_CA_WOMAN} Man : {_CA_MAN} "
        f"Outfits : flowy cream / terracotta linen midi dresses for women, open oat linen "
        f"shirt with tailored shorts for man. "
        f"POSE : Mid-meal candid moment, one woman passing a small bread basket to the man, "
        f"both women mid-laugh, man with a soft attentive smile. Natural straw tote bag slung "
        f"on the chair back. Existing wine glasses / water carafe on the table only. None "
        f"looks at the camera."
    ),
    ("small_groups", "rooftop_deck"): (
        f"Place exactly THREE subjects standing on the rooftop deck (on the safe interior "
        f"side of the existing railing) — three young adult friends (two women 24-26, one "
        f"man 26-28). Women look : {_CA_WOMAN} Man : {_CA_MAN} "
        f"Outfits : flowy cream silk slip dresses for women with slim heeled sandals, open "
        f"cream linen shirt and tailored sand chino shorts for the man. "
        f"POSE : They form a loose group facing each other in soft profile, sleek cocktail "
        f"glasses with clear ice in hand, mid-laugh. Natural late-afternoon golden hour warm "
        f"light glowing on their tanned profiles. None looks at the camera."
    ),
    ("small_groups", "outdoor_deck"): (
        f"Place exactly THREE subjects standing in a loose group on the existing outdoor "
        f"deck — three young adult friends (two women 24-26, one man 26-28). Women look : "
        f"{_CA_WOMAN} Man : {_CA_MAN} "
        f"Outfits : flowy cream / terracotta short linen dresses for women, oat linen shirt "
        f"and tailored sand shorts for the man. Sleek sunglasses on all. "
        f"POSE : They share a candid laugh, slightly turned toward each other. Natural "
        f"golden warm light. None looks at the camera."
    ),

    # ━━ GROUPS (4 amis énergie festive California) ━━━━━━━━━━━━━━━━━━━━━━
    ("groups", "in_water"): (
        f"Place FOUR subjects in the existing pool water — a group of young adult friends "
        f"(two women 24-26, two men 26-28). Women look : {_CA_WOMAN} Men look : {_CA_MAN} "
        f"Outfits : women in chic modern bikinis (one cream high-waist, one terracotta "
        f"bandeau), men in tailored navy / oat swim shorts. "
        f"POSE : All four stand chest-deep (water at sternum) near the center of the pool, "
        f"forming a loose circle. Two of them mid-laugh, the others smiling in conversation. "
        f"Soft water ripples around their bodies. None look at the camera ; festive confident "
        f"daytime vibe."
    ),
    ("groups", "pool_edge"): (
        f"Place FOUR subjects sitting in a row DIRECTLY on the existing bare pool deck "
        f"(concrete / tile / wood — exact same material as input) at the pool edge with feet in "
        f"the water — a group of young adult friends (two women 24-26, two men 26-28). Women : "
        f"{_CA_WOMAN} Men : {_CA_MAN} "
        f"Outfits : modern chic swimwear (high-waist bikinis for women, tailored swim shorts "
        f"for men). Sleek sunglasses on all. "
        f"POSE : They sit side by side ON THE BARE DECK, hands resting flat behind them on the "
        f"BARE concrete/tile for support (no cushion, no towel under them). Mid-conversation, "
        f"two of them mid-laugh, one holding a cold cocktail glass with citrus. Natural straw "
        f"crochet tote bag at the end of the row. None looks at the camera.\n"
        f"🚫 DO NOT add any cushion, towel, bench, mat, or extra surface under them. DO NOT "
        f"extend the deck or invent a step. DO NOT modify the pool shape."
    ),
    ("groups", "lounger"): (
        f"Place FOUR subjects on four adjacent existing sun loungers — a group of young adult "
        f"friends (two women 24-26, two men 26-28). Women : {_CA_WOMAN} Men : {_CA_MAN} "
        f"Outfits : chic modern swimwear, wide-brimmed straw hats and sleek sunglasses. "
        f"POSE : They share a candid relaxed moment — one sitting up to talk to the others, "
        f"the others reclining propped on one elbow. Natural mid-afternoon golden sun on "
        f"their tanned bodies. None looks at the camera."
    ),
    ("groups", "rooftop_deck"): (
        f"Place FOUR subjects standing in a loose semicircle on the rooftop deck (on the "
        f"safe interior side of the existing railing) — a group of young adult friends "
        f"(two women 24-26, two men 26-28). Women : {_CA_WOMAN} Men : {_CA_MAN} "
        f"Outfits : flowy cream / terracotta silk slip dresses for women, open cream linen "
        f"shirts with tailored sand chino shorts for men. "
        f"POSE : Mid-toast with sleek cocktail glasses (clear ice + citrus) in hand, all "
        f"smiling, two of them mid-laugh. Natural late-afternoon golden hour warm light on "
        f"profiles. None looks at the camera."
    ),
    ("groups", "outdoor_deck"): (
        f"Place FOUR subjects standing in a loose semicircle on the existing outdoor deck "
        f"— a group of young adult friends (two women 24-26, two men 26-28). Women : "
        f"{_CA_WOMAN} Men : {_CA_MAN} "
        f"Outfits : flowy linen short dresses for women, oat linen shirts and tailored "
        f"sand shorts for men. Sleek sunglasses, natural straw crochet tote bag visible. "
        f"POSE : Mid-laugh in candid group conversation, slightly turned toward each other. "
        f"Natural golden warm light. None looks at the camera."
    ),
    ("groups", "dining_table"): (
        "Place FOUR subjects around the existing dining table — a group of trendy mixed-race "
        "friends late 20s, mid-meal candid moment, one of them mid-laugh raising a glass. "
        "Casual smart attire. None looks at the camera."
    ),
}


def _coerce_scenario_count(scenario_block: str, target_n: int) -> str:
    """Réécrit les mentions de quantité dans le scenario pour matcher target_n.

    Bug récurrent (Martin 13/05/2026) : le scenario hardcoded dit "Place exactly
    THREE subjects" mais target_n=1 (capé pour cause de barrière par exemple).
    Gemini Image suit le scenario plus détaillé → 3 humains au lieu de 1.

    Fix : on patch le scenario AVANT l'envoi pour aligner les chiffres.
    Conserve les autres mentions numériques (ex: "24-26") intactes.
    """
    if not scenario_block or target_n is None:
        return scenario_block
    import re as _re
    word_n = {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR", 5: "FIVE"}.get(target_n, str(target_n))
    # Remplace "Place exactly TWO/THREE/FOUR/FIVE subjects" → "Place exactly {N} subject(s)"
    patched = _re.sub(
        r"Place exactly (ONE|TWO|THREE|FOUR|FIVE)\s+(subject|subjects)\b",
        f"Place exactly {word_n} subject(s)",
        scenario_block,
    )
    # Remplace aussi "Place FOUR subjects" sans "exactly"
    patched = _re.sub(
        r"\bPlace\s+(ONE|TWO|THREE|FOUR|FIVE)\s+(subject|subjects)\b",
        f"Place {word_n} subject(s)",
        patched,
    )
    return patched


def _build_auto_description_scenario(
    persona: str,
    zone_text: str,
    target_n: int,
) -> str:
    """Construit un scenario générique adapté à la safe_zone Gemini réelle.

    Utilisé quand aucun scenario hardcoded ne match précisément la safe_zone
    (= évite le bug "yoga sur tapis de course"). Plutôt que d'imposer une pose
    spécifique qui peut être incohérente avec l'équipement réel, on délègue la
    décision de pose à Gemini Image en lui donnant :
      - l'identité du sujet (look California influencer)
      - la zone EXACTE telle que décrite par Gemini Vision
      - une pose ADAPTÉE à la zone, dérivée des mots-clés de la zone_text

    Returns : bloc texte prêt à insérer dans le prompt.
    """
    # Look + outfit selon persona
    if persona == "solos":
        identity = f"a young adult woman 24-26. {_CA_WOMAN}"
        outfit_hint = (
            "Outfit adapted to the zone : swimwear if pool/beach context, premium "
            "athleisure if gym, flowy silk slip dress if rooftop/lounge, smart "
            "casual if dining."
        )
    elif persona == "couples":
        identity = f"a young adult couple (one woman 24-26, one man 26-28). WOMAN : {_CA_WOMAN} MAN : {_CA_MAN}"
        outfit_hint = (
            "Outfits adapted to the zone : swimwear for pool/beach, premium athleisure "
            "for gym, slip dress + linen shirt for rooftop, smart casual for dining."
        )
    elif persona == "families":
        identity = f"a young family with one child age 6-7. {_FAMILY_LOOK}"
        outfit_hint = (
            "Outfits adapted to the zone : swimwear for pool/beach, casual play "
            "clothes for outdoor, smart casual for dining."
        )
    elif persona in ("small_groups", "groups"):
        n_women = 2 if persona == "small_groups" else 2
        n_men = 1 if persona == "small_groups" else 2
        identity = (
            f"a group of young adult friends ({n_women} women 24-26, {n_men} men "
            f"26-28). WOMEN : {_CA_WOMAN} MEN : {_CA_MAN}"
        )
        outfit_hint = (
            "Outfits adapted to the zone : swimwear for pool/beach, athleisure for "
            "gym, slip dresses + linen shirts for rooftop/lounge, smart casual for "
            "dining."
        )
    else:
        identity = f"a young adult subject. {_CA_WOMAN}"
        outfit_hint = "Outfit adapted to the zone context."

    # Pose hint déduite des mots-clés dans zone_text (FR + EN)
    zl = (zone_text or "").lower()
    if any(k in zl for k in ["course", "treadmill", "running"]):
        pose_hint = (
            "POSE : The subject is ACTIVELY using the machine — mid-stride running "
            "with both feet on the belt (running shoes mandatory), hands lightly "
            "holding the front rail. NOT yoga, NOT stretching, NOT standing still."
        )
    elif any(k in zl for k in ["banc", "bench", "musculation"]):
        pose_hint = (
            "POSE : The subject is SITTING UPRIGHT on the bench, holding light "
            "dumbbells in a controlled shoulder-press position, focused expression."
        )
    elif any(k in zl for k in ["haltère", "dumbbell", "weight"]):
        pose_hint = (
            "POSE : The subject is reaching for / picking up a dumbbell from the rack, "
            "body slightly turned in profile, focused expression."
        )
    elif any(k in zl for k in ["yoga", "stretching"]):
        pose_hint = (
            "POSE : The subject is in a graceful warrior-II yoga pose on the mat, "
            "gaze focused along front arm."
        )
    elif any(k in zl for k in ["debout", "standing", "admir"]):
        pose_hint = (
            "POSE : The subject stands relaxed, soft confident posture, gazing out "
            "at the scene / horizon (NOT at camera)."
        )
    elif any(k in zl for k in ["assise", "assis", "sit", "seated"]):
        pose_hint = (
            "POSE : The subject is sitting in the position described by the zone, "
            "relaxed and natural."
        )
    elif any(k in zl for k in ["allong", "lying", "reclin", "lecture", "lit"]):
        pose_hint = (
            "POSE : The subject is reclining/lying on the surface described, in a "
            "relaxed natural pose."
        )
    elif any(k in zl for k in ["nag", "swim"]):
        pose_hint = (
            "POSE : The subject is swimming gently, head above water, chest-deep."
        )
    else:
        pose_hint = (
            "POSE : Choose a pose that is PHYSICALLY COHERENT with the zone described "
            "above — sitting on a seat, standing on solid ground, lying on a flat "
            "surface, etc. NEVER pick an incoherent pose (e.g. yoga on a treadmill, "
            "lying on an upright machine)."
        )

    return (
        f"Place exactly {target_n} subject(s) — {identity} "
        f"LOCATION : EXACTLY in the zone described as «{zone_text}» — same spot, "
        f"same orientation. Do NOT relocate to a different area. "
        f"{outfit_hint} "
        f"{pose_hint} "
        f"Subject(s) gaze : NEVER at the camera (look forward, at each other, at the "
        f"horizon, or at the activity). Natural ambient light matching the existing "
        f"scene direction."
    )


def pick_human_scenario(
    persona: str,
    category: str,
    safe_zones: list[str] | None,
    capacity: int | None,
    target_n: int | None = None,
) -> dict | None:
    """Choisit UN scenario unique pour cette photo. Retourne None si pas de scenario valide.

    Approche v3 (Martin 13/05/2026) :
      1. Map la safe_zone[0] vers un zone_type via _classify_safe_zone.
      2. Cherche un scenario hardcoded matchant (persona, zone_type).
      3. Si match → vérifie cohérence SÉMANTIQUE entre zone_text et scenario_text.
         (ex: scenario "yoga warrior-II" vs zone "tapis de course" → REJET).
      4. Si pas de match OU rejet → fallback vers _build_auto_description_scenario
         qui construit un scenario à partir des mots-clés de la zone Gemini.
    """
    if not safe_zones:
        return None
    cat_lower = (category or "").lower()
    if cat_lower in ("piscine_vue_aerienne", "facade", "chambre", "staff"):
        return None

    zone_text = safe_zones[0] if safe_zones else ""
    zone_type = _classify_safe_zone(zone_text)

    if zone_type == "unknown":
        if cat_lower in ("piscine", "rooftop"):
            zone_type = "in_water" if "piscine" in cat_lower else "rooftop_deck"
        elif cat_lower in ("cabana",):
            zone_type = "cabana_daybed"
        elif cat_lower in ("transat",):
            zone_type = "lounger"
        elif cat_lower in ("f_and_b",):
            zone_type = "dining_table"
        elif cat_lower in ("gym",):
            # ⚠️ pas de fallback aveugle gym→gym_mat — on délègue au scenario AUTO
            zone_type = "AUTO_GYM"
        elif cat_lower in ("interieur_commun",):
            zone_type = "indoor_seating"
        else:
            zone_type = "outdoor_deck"

    # Cherche le scenario exact (persona, zone_type)
    block = _SCENARIO_CATALOG.get((persona, zone_type))
    if block is None:
        for fallback_persona in ("couples", "solos", "small_groups"):
            block = _SCENARIO_CATALOG.get((fallback_persona, zone_type))
            if block:
                break

    # ━ COHERENCE CHECK SÉMANTIQUE (Martin 13/05/2026 — fix yoga sur tapis course) ━
    # On regarde UNIQUEMENT les ~250 premiers chars du scenario (= pose principale),
    # pas les phrases NEGATIVE en fin qui mentionnent "NEVER yoga" etc.
    rejected_reason = None
    if block:
        pose_intro = block[:250].lower()  # zone où la pose principale est décrite
        zone_lower = zone_text.lower()
        incoherences = [
            (("yoga pose", "warrior-ii", "downward dog"),
             ("tapis de course", "treadmill", "running", "course", "banc de musc", "vélo", "haltère", "rower", "bench press")),
            (("running stride", "mid-stride running", "on the treadmill"),
             ("yoga mat", "tapis de yoga", "stretching mat")),
            (("swimming", "submerged", "chest-deep"),
             ("rooftop deck", "outdoor deck", "patio", "interior seat", "lounge chair", "canapé")),
            (("around the dining table", "at the existing dining table"),
             ("pool water", "in the pool", "lounger", "transat")),
        ]
        for scenario_kws, zone_kws in incoherences:
            if any(s in pose_intro for s in scenario_kws) and any(z in zone_lower for z in zone_kws):
                rejected_reason = f"scenario décrit {scenario_kws} mais zone Gemini dit {zone_kws}"
                block = None
                break

    # Fallback vers AUTO-DESCRIPTION si rien ne match ou incohérence
    if block is None:
        eff_n = target_n if target_n else (
            compute_target_humans(persona, capacity) if capacity else
            (2 if persona in ("couples", "small_groups", "families", "groups") else 1)
        )
        eff_n = max(1, min(eff_n, 5))
        block = _build_auto_description_scenario(persona, zone_text, eff_n)
        return {
            "scenario_id": f"{persona}__AUTO",
            "persona": persona,
            "zone_type": zone_type,
            "primary_safe_zone": zone_text,
            "prompt_block": block,
            "auto_description_used": True,
            "rejected_hardcoded_reason": rejected_reason,
        }

    return {
        "scenario_id": f"{persona}__{zone_type}",
        "persona": persona,
        "zone_type": zone_type,
        "primary_safe_zone": zone_text,
        "prompt_block": block,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Legacy : PERSONA_TEMPLATES (gardé pour rétro-compat des autres callers, mais
# n'est plus utilisé par build_persona_prompt depuis 12/05/2026).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PERSONA_TEMPLATES = {
    "couples": (
        "ONE couple (mixed-race adult man + woman, late 20s/30s, NATURAL chemistry), real travel candid feel (NOT staged photo shoot). "
        "Outfit : SWIMWEAR for both on pool/beach scenes (bikini/one-piece for her, swim shorts for him), or relaxed leisure attire for indoor/dining. "
        "\n\n"
        "PLACEMENT (pick what fits the scene) :\n"
        "  (1) IN THE WATER swimming together, splashing playfully, one of them helping the other into the water, holding hands while wading\n"
        "  (2) AT POOL EDGE — both sitting feet-in-water side by side, talking, sharing a drink, one leaning on the other\n"
        "  (3) HALF-EMERGING — she's in the water, he's at the edge offering her a hand, OR vice versa. Wet skin, real moment\n"
        "  (4) ON EXISTING ADJACENT LOUNGERS (only if clearly visible empty) — relaxed, one reading, one on phone, occasionally glancing at each other\n"
        "  (5) ON ONE EXISTING DAYBED/CABANA SOFA together — close, casual\n"
        "  (6) IF nothing fits naturally → DO NOT ADD. Return image unchanged.\n"
        "\n"
        "INTERACTION : real chemistry, mid-conversation, sharing a candid moment. They can look at each other, OR one looking at the other, OR both looking at the water/horizon naturally — but NEVER directly at the camera.\n"
        "\n"
        "🔥 NEVER invent furniture. If only one good spot exists, ADD JUST HIM OR HER (one person), NOT a couple."
    ),
    "solos": (
        "ONE adult woman (late 20s/early 30s, mixed-race or any ethnicity that fits the scene), naturally beautiful (NOT artificial / over-filtered). "
        "She is LIVING THE MOMENT — actually enjoying her stay, not posing for a photo shoot. Think candid travel photography, NOT model fashion editorial. "
        "Outfit : chic SWIMWEAR (sleek one-piece swimsuit, stylish bikini, monokini, cut-out swimsuit) — NEVER a long dress, NEVER a robe, NEVER street clothes. Sunglasses pushed up in her hair or on her face, a summer hat (straw / canvas / bucket) optional. Toned, healthy, natural body. Real micro-expressions, slight asymmetry — feels REAL, not posed."
        "\n\n"
        "PLACEMENT (pick the option that fits THIS scene best) :\n"
        "  (1) IN THE WATER swimming gently breaststroke or freestyle, head above water, real swim motion (one arm extended, slight splash) — NOT planking, NOT lying flat\n"
        "  (2) HALF-EMERGING FROM POOL at the edge — wet skin, hair wet, leaning naturally on the rim with one or both arms, can be looking AT the water, AT her arm/hand, AT a friend off-scene, OR straight ahead with a soft natural expression. NOT a posed gaze\n"
        "  (3) AT POOL EDGE sitting with feet/legs in water, can be reading on her phone, putting on/removing sunglasses, applying sunscreen, sipping a drink, scrolling on her phone, casually looking down at the water\n"
        "  (4) STANDING in the pool waist-deep, can be moving / wading / gathering hair behind ears\n"
        "  (5) ON AN EXISTING EMPTY LOUNGER (only if clearly visible empty) — reading, on her phone, eyes closed sunbathing, drinking. NOT staring at the horizon for the photo\n"
        "  (6) IF NOTHING FITS NATURALLY → DO NOT ADD. Return image unchanged.\n"
        "\n"
        "GAZE & EXPRESSION (CRITICAL — vary these naturally) :\n"
        "  - VARIETY of gaze : looking at her phone / at her drink / at the water / at her hand / closing eyes peacefully / mid-laugh / chatting / reading / lost in thought looking down / NOT every photo with the same 'looking off in the distance' magazine pose\n"
        "  - NEVER directly at the camera (no selfie pose)\n"
        "  - Natural micro-expressions : slight smile / focused / serene / casual\n"
        "  - The viewer should think 'this is a guest enjoying her day', NOT 'this is a model posing for a brochure'\n"
        "\n"
        "🚫 DO NOT use a 'planking on the back' pose. DO NOT use a 'staring at the horizon hand-on-hip' pose. DO NOT make her look like she's modeling.\n"
        "\n"
        "🔥 ABSOLUTE RULE — DO NOT INVENT any furniture, raft, float, daybed, lounger, ladder, pool steps, handrail, or anything not visible in the input. If no place to put her naturally → do not add anyone.\n"
        "\n"
        "Mid-action candid moment, warm natural light, premium-accessible editorial travel-magazine feel — like a real guest snapped by a friend, not a fashion shoot."
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
    "cabana":   "lounging on the existing daybed/cabana sofa, sunglasses on, relaxed posture (use existing furniture only)",
    "transat":  "reclining on the existing sun lounger(s), correct contact with the chair, natural weight (use existing furniture only)",
    "piscine":  "DEFAULT placement = MODEL pose IN or AT the water. Priority order: (1) emerging from the pool at the edge (wet hair slicked back, elbows on pool rim, droplets running down — Vogue Vacation editorial), (2) swimming breaststroke head above water in golden light, (3) sitting at pool edge with calves in water in editorial model pose, (4) standing waist-deep gliding hands through water. Stylish swimwear (bikini/one-piece). Editorial wet-look beauty, fashion-editorial composition. Use an existing lounger ONLY if clearly empty in foreground. ⛔ DO NOT use 'planking on the back' pose (lying flat horizontally on water — looks artificial). NEVER invent a raft, lounger, daybed, towel, or any object.",
    "piscine_vue_aerienne": "DO NOT add subjects (aerial view — added humans would be tiny). If the brief insists, return image unchanged.",
    "rooftop":  "standing at the rooftop with view in background holding a drink, OR seated on existing rooftop lounger, OR if there's a rooftop pool: in/at the pool (cf. piscine rules)",
    "f_and_b":  "around the existing dining table, mid-meal moment (passing food, pouring drink), one or two glasses on the table",
    "beach":    "on the existing sun lounger or beach chair, swimwear, relaxed beach moment, OR walking on the sand/at water edge",
    "gym":      "ONE adult mid-action using the EXISTING equipment visible in the photo : doing yoga/pilates pose on a yoga mat (downward dog, plank, lunge, savasana), OR light stretching against a wall/floor, OR holding a small dumbbell/kettlebell at low weight, OR moderate pace on a stationary bike/treadmill if visible. Athleisure outfit (leggings, sports bra, fitted t-shirt). Focused but relaxed mood, never grimacing, never lifting heavy weights. Use ONLY existing equipment — do not invent machines, weights, or mats.",
    "exterieur": "naturally placed in the existing outdoor space, casual moment",
    "interieur_commun": "naturally placed in the existing interior space (seated on chairs/sofas, gathered near tables, walking through), casual conversation, fitting the venue type — leisure/business-casual attire, no formal black-tie",
}


# ━━ Arbre de décision Persona × Capacity ━━
# Aligné sur la doc onglet Personas & humains (Vision validé Martin).
# capacity = seats + water_zones identifiés par Gemini sur la photo.
# Règle d'or : ne JAMAIS dépasser la capacity (pas d'invention de places).
# Si capacity = 0 → on ne place pas d'humain (pipeline skip propre).
PERSONA_CAPACITY_TARGET = {
    # persona  →  fonction qui prend capacity et retourne nombre cible d'humains
    "solos":        lambda cap: 1,
    "couples":      lambda cap: 1 if cap == 1 else 2,
    "small_groups": lambda cap: 1 if cap == 1 else (2 if cap in (2, 3) else 3),
    "families":     lambda cap: 1 if cap == 1 else (2 if cap in (2, 3) else (4 if cap >= 7 else 3)),
    "groups":       lambda cap: 1 if cap == 1 else (2 if cap == 2 else (3 if cap == 3 else (4 if cap <= 6 else 5))),
}


def compute_target_humans(persona: str, capacity: int) -> int:
    """Détermine combien d'humains placer selon le persona et la capacity de la photo.

    Approche NON-CONTRAIGNANTE : si capacity=0 → 0 humain. Sinon arbre selon persona.
    """
    if capacity <= 0:
        return 0
    fn = PERSONA_CAPACITY_TARGET.get(persona)
    if not fn:
        # Fallback sécuritaire : 1 ou 2 selon capacity
        return 1 if capacity == 1 else 2
    return fn(capacity)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pool float props (bouées rigolotes)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Martin (11/05/2026) : on aime bien voir des bouées fun (flamant rose, ananas,
# donut…) dans les photos de piscine — ça donne un côté playful crédible —
# MAIS PAS systématique sinon ça devient cliché. On le déclenche :
#   • uniquement sur catégorie 'piscine' / 'rooftop' (avec piscine)
#   • probabilité ~35% (deterministe par photo via hash filename → reproductible)
#   • vibes "Family-Friendly" / "Party" / "Trendy" : +20% (= ~55%)
#   • vibes "Luxe" / "Serene" : -20% (= ~15%, garde le côté minimaliste)
# Si déclenché : on autorise UN float dans le prompt, avec règles strictes
# (taille plausible, subject ON or NEAR the float, jamais multiple floats).
import hashlib

# ━━ POOL_FLOATS_OPTIONS : variété conservée + scale lock par item ━━━━━━━━━━━
# Martin (19/05/2026) : on garde le côté trendy/Instagram (flamingo, pineapple,
# swan, unicorn…) parce que ça fait l'identité visuelle Dayuse. Bug taille
# adressé via 3 leviers conjoints :
#   1. Description par item annoté "small / single-seater / compact / ~1-1.5m"
#      → force Nano Banana à rendre la version personnelle, PAS la version giant
#      party flamingo de 3m. La taille déclarée par item est notre anchor le
#      plus fiable (l'IA respecte mieux une longueur en mètres qu'un pourcentage).
#   2. Prompt build_pool_float_only_prompt avec scale lock 10% surface eau + "size
#      of a child or beach ball, NEVER adult+arms".
#   3. Validator pool_float_realistic seuil 12% FAIL.
POOL_FLOATS_OPTIONS = [
    # Classiques iconiques — version PERSONNELLE (pas party prop)
    "a SMALL pink inflatable flamingo float — single-seater, compact ~1.4m long (NOT the giant 3m party flamingo), photogenic top-pose with neck folded",
    "a SMALL inflatable pineapple float — bright yellow body ~1.2m tall with realistic green leaves (not the oversized 2m+ party version), single-seater",
    "a colorful donut pool float — pink frosting with rainbow sprinkles, glossy finish, compact ~1m diameter",
    "a SMALL white inflatable swan float — single-seater, compact ~1.3m long, elegant tucked-wing pose (NOT the giant party swan)",
    "a watermelon slice inflatable float — pink flesh with dark seeds and green rind, compact ~1m wide single-seater",
    "a translucent pastel-colored inflatable ring — clean minimalist aesthetic, soft mint or peach tone, ~1m diameter disc",
    # Instagrammable / influenceur-friendly — version PERSONNELLE
    "a SMALL inflatable unicorn float — pastel rainbow mane, gold horn, soft white body, single-seater compact ~1.3m (NOT the giant 2.5m unicorn)",
    "an avocado pool float — green outer ring with a centered brown stone (you sit IN it), compact single-seater ~1.2m diameter",
    "a SMALL inflatable ice cream cone float — pastel scoop on a waffle cone pattern, cherry on top, single-seater ~1.3m long",
    "a SMALL golden swan float — same as classic swan but in metallic gold finish (luxe instagram aesthetic), compact ~1.3m long",
    "a SMALL inflatable peacock float — turquoise and emerald body with realistic tail feather pattern, single-seater ~1.3m",
    "an inflatable shell float — iridescent pearl-pink scallop, mermaidcore aesthetic, compact ~1m diameter",
    "an inflatable lemon slice float — bright yellow with white pulp pattern, summer-fresh look, ~1m diameter disc",
    "a classic round inflatable inner tube — pastel coral color, simple ~1m disc with no protruding parts",
]

POOL_FLOAT_BASE_PROBABILITY = 0.35


def pick_pool_float_hint(
    category: str | None,
    vibe: str | None,
    photo_filename: str | None,
    analysis: dict | None = None,
) -> str | None:
    """Retourne la description du float à autoriser, ou None pour skip.

    Déterministe par filename → un même run replay donne le même résultat.
    Pas systématique : la randomisation déterministe est CRITIQUE pour que ça
    reste "occasionnel et naturel" comme demandé par Martin.

    SKIP RULE (Martin 15/05/2026, bug Moxy Miami vue aérienne : cygne doré ajouté
    alors que 15 bouées colorées déjà présentes → look incohérent) : si la photo
    contient DÉJÀ des bouées/flotteurs, on n'en ajoute pas — pour éviter (a) de
    saturer la piscine, (b) d'ajouter une bouée stylistiquement incohérente avec
    les existantes (perspective, palette).
    """
    if not category:
        return None
    cat_lower = category.lower()
    # Seuls les scènes piscine sont éligibles (rooftop ok ssi le mot pool est dedans)
    if "piscine" not in cat_lower and "pool" not in cat_lower:
        return None

    # ━━ SKIP si bouées EXISTANTES détectées dans la photo source ━━━━━━━━━━━━━
    # Niveau A : on scanne factual.subjects / clutter_to_remove / issues pour des
    # mots-clés bouée. Si match → on n'ajoute pas une 2e bouée.
    # Note : analyze.py ne retourne pas (encore) un champ structuré
    # existing_pool_floats_count → fallback heuristique sur les chaînes de texte.
    if analysis:
        factual = analysis.get("factual") or {}
        subjects_text = " ".join(factual.get("subjects") or []).lower()
        clutter_text = " ".join(analysis.get("clutter_to_remove") or []).lower()
        issues_text = " ".join(analysis.get("issues") or []).lower()
        # Mots-clés bouée gonflable, en FR + EN (Gemini Vision peut alterner)
        EXISTING_FLOAT_KEYWORDS = (
            "bouée", "bouee", "buoy", "float", "flotteur", "matelas gonflable",
            "gonflable", "inflatable", "flamingo", "flamant", "donut", "swan", "cygne",
            "lilo", "pool noodle", "frite piscine", "raft", "ring", "anneau gonflable",
            "ananas gonflable", "watermelon float", "pastèque gonflable", "licorne gonflable",
        )
        combined = f"{subjects_text} {clutter_text} {issues_text}"
        if any(kw in combined for kw in EXISTING_FLOAT_KEYWORDS):
            return None  # Bouée(s) déjà présente(s) → on n'en ajoute pas
    # Les vues aériennes piscine RESTENT éligibles pour les bouées.
    # Référence : hero homepage Dayuse avec bouée flamingo en vue aérienne. La bouée est
    # parfaitement visible en aerial (contrairement à un humain qui serait trop petit).
    is_aerial = "aerienne" in cat_lower or "aerial" in cat_lower

    # Probabilité ajustée par vibe
    prob = POOL_FLOAT_BASE_PROBABILITY
    if vibe in ("Family-Friendly", "Party", "Trendy"):
        prob += 0.20
    elif vibe in ("Luxe", "Serene"):
        prob -= 0.20
    # ━━ Boost vue aérienne (Martin 12/05/2026) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Sur piscine_vue_aerienne, l'ajout d'humain est interdit (figure trop petite) ⇒
    # la bouée est le SEUL élément playful possible pour casser le cadrage plat.
    # Cap à 0.70 → on garantit ~7/10 photos aerial pool avec bouée tout en gardant
    # une part de variété (3/10 sans bouée pour les scènes type spa minimaliste).
    if is_aerial:
        prob = max(prob, 0.70)
    prob = max(0.0, min(1.0, prob))

    # Random déterministe par filename → reproductible sur replay
    seed_str = photo_filename or ""
    h = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:8], 16)
    bucket = (h % 1000) / 1000.0  # ∈ [0, 1)
    if bucket >= prob:
        return None

    # Choix du float (déterministe aussi)
    idx = h % len(POOL_FLOATS_OPTIONS)
    return POOL_FLOATS_OPTIONS[idx]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Détection structurelle "rooftop avec barrière de sécurité au premier plan"
# (Martin 13/05/2026 — Andaz West Hollywood bug : Gemini Image plaçait des
# humains DERRIÈRE la barrière en verre en INVENTANT des transats dans le
# vide). Si Gemini Vision a flaggé une unsafe_zone contenant des mots-clés
# barrière au premier plan → on injecte un bloc EXTRA-STRICT en tête de
# prompt + on force max_humans à 1 (réduit la tentation d'éparpiller hors zone).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_BARRIER_KEYWORDS = (
    "barrière", "barriere", "garde-corps", "garde corps", "guardrail",
    "guard rail", "guard-rail", "balustrade", "railing", "glass barrier",
    "glass panel", "safety rail", "safety fence", "parapet", "verre au premier plan",
)


def _detect_barrier_risk(unsafe_zones: list[str] | None, category: str | None) -> bool:
    """Retourne True si la scène a une barrière de sécurité explicitement listée
    en unsafe_zone — typique des rooftops, balcons, terrasses en hauteur.

    Quand True, le prompt ajout perso injecte un bloc EXTRA strict pour empêcher
    Gemini Image de placer des humains du mauvais côté de la barrière (et inventer
    du mobilier dans le vide).
    """
    if not unsafe_zones:
        return False
    cat = (category or "").lower()
    # On ne déclenche que sur scènes à risque (rooftop, balcony, terrasse, piscine
    # en hauteur). Pour une piscine au sol, "railing" autour de la piscine est OK
    # et ne doit pas déclencher ce flag.
    if cat not in ("rooftop", "piscine", "exterieur_commun", "vue_panoramique"):
        return False
    for z in unsafe_zones:
        if not z:
            continue
        z_low = z.lower()
        if any(k in z_low for k in _BARRIER_KEYWORDS):
            return True
    return False


def build_persona_prompt(persona: str, category: str, vibe: str | None = None,
                         safe_zones: list[str] | None = None,
                         unsafe_zones: list[str] | None = None,
                         max_humans: int | None = None,
                         capacity: int | None = None,
                         pool_float_hint: str | None = None,
                         scenario_block_override: str | None = None,
                         chosen_anchor: str | None = None) -> str:
    """Construit le prompt ajout personnage.

    ━━━ HISTORIQUE & VERSIONS ━━━
    • V1 (longue, ~5000 tokens) — version actuellement utilisée PAR DÉFAUT.
        Cumul de règles construit au fil des bugs avr.→mai 2026. Beaucoup de
        doublons et contradictions mais marche dans la majorité des cas.
    • V2 (compacte, ~2000 tokens) — tentative de refonte 13/05/2026.
        Hypothèse : moins de dilution = signal critique plus pur. En pratique :
        bord piscine inventé, échelles humaines incohérentes, sport non-sense
        sur tapis de course → V2 sacrifie trop de garde-fous. Rollback Martin.
        Conservée en opt-in derrière flag env pour tests futurs.

    🔁 BASCULER VERS V2 (compacte) — pour tests :
        export USE_COMPACT_PROMPT_V2=1
        # puis relance Flask

    🔁 BASCULER VERS V1 (longue) — défaut, comportement actuel :
        # rien à faire (= unset USE_COMPACT_PROMPT_V2)
    """
    # ━━ Par défaut : V1 longue (Martin 13/05/2026 — V2 compacte trop permissive) ━━
    # On délègue au snapshot V1 dans _legacy_prompts.py qui contient la version
    # complète avec toutes les règles strictes (anatomy, scale lock, water depth, etc).
    if os.getenv("USE_COMPACT_PROMPT_V2") != "1":
        from _legacy_prompts import build_persona_prompt_v1_long
        return build_persona_prompt_v1_long(
            persona=persona, category=category, vibe=vibe,
            safe_zones=safe_zones, unsafe_zones=unsafe_zones,
            max_humans=max_humans, capacity=capacity,
            pool_float_hint=pool_float_hint,
            scenario_block_override=scenario_block_override,
            chosen_anchor=chosen_anchor,
            _pick_human_scenario=pick_human_scenario,
            _PERSONA_TEMPLATES=PERSONA_TEMPLATES,
            _CATEGORY_ACTION_HINT=CATEGORY_ACTION_HINT,
            _compute_target_humans=compute_target_humans,
            _detect_barrier_risk=_detect_barrier_risk,
            _coerce_scenario_count=_coerce_scenario_count,
        )
    # ━━ Sinon : V2 compacte (opt-in via env var) ━━

    # ━━ Détection rooftop+barrière (impacte persona ET target_n) ━━
    # (Martin 13/05/2026, Andaz V2 bug) : si la photo a une barrière en premier plan,
    # on cap target_n à 1 ET on bascule persona vers "solos" pour que le scenario_block
    # généré décrive UNE personne (et pas un couple). Sinon le prompt envoyé contient
    # à la fois "EXACTLY 1" et "Place TWO subjects" → contradiction → Gemini suit le
    # plus détaillé (le scenario) → 2 personnes placées dans une zone à risque.
    barrier_risk = _detect_barrier_risk(unsafe_zones, category)
    effective_persona = persona
    if barrier_risk and persona in ("couples", "small_groups", "families", "groups"):
        effective_persona = "solos"

    # ━━ Scenario déterministe (= description sujet + pose précise) ━━
    scenario = pick_human_scenario(effective_persona, category, safe_zones, capacity)
    scenario_block = scenario["prompt_block"] if scenario else None

    # ━ target_n : nombre d'humains à placer (capé 1 si barrière) ━
    if capacity is not None and capacity > 0:
        target_n = compute_target_humans(effective_persona, capacity)
    elif max_humans is not None:
        target_n = max_humans
    else:
        target_n = 2 if effective_persona in ("couples", "small_groups", "families", "groups") else 1
    target_n = max(1, min(target_n, 5))
    if barrier_risk and target_n > 1:
        target_n = 1  # rooftop+barrière → 1 humain bien placé > 2 humains à risque

    vibe_mood = {
        "Family-Friendly": "warm family vacation energy, playful but tasteful",
        "Party":           "festive daytime vibe, friends having fun, never crowded",
        "Serene":          "quiet contemplative moment, peaceful luxury",
        "Luxe":            "effortless luxury, refined casual elegance",
        "Trendy":          "urban-leisure vibe, lifestyle editorial mood",
    }.get(vibe or "", "warm relaxed daytime moment, premium-accessible feel")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # BLOC A — WHERE : zones autorisées / interdites + règle "no invention"
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if safe_zones:
        safe_list = "\n".join(f"  ZONE {i+1}: {z}" for i, z in enumerate(safe_zones))
        unsafe_list = "\n".join(f"  ✘ {z}" for z in (unsafe_zones or [])) or "  (no specific forbidden zones)"
        where_block = f"""
🎯 WHERE — ALLOWED placement zones (Gemini Vision identified these as the ONLY natural spots, in priority order) :
{safe_list}

🚫 FORBIDDEN placement zones (DO NOT place any subject here, ever) :
{unsafe_list}

ABSOLUTE LAW : place the subject(s) in ONE of the ALLOWED zones above, EXACTLY as described. If the scene does not allow it without inventing or modifying anything, return the image UNCHANGED.
"""
    else:
        where_block = """
🛑 NO SAFE PLACEMENT ZONE in this photo. Return the image UNCHANGED. Do NOT add any subject.
"""

    # Bloc rooftop+barrière (conditionnel) — placé en HAUT pour primauté
    barrier_lock_block = ""
    if barrier_risk:
        barrier_lock_block = """🚨 SAFETY BARRIER LOCK (rooftop / elevated scene) :
A safety barrier (glass / metal / parapet) separates the SAFE INTERIOR (existing furniture, pool, deck) from the VOID OUTSIDE (sky, city view, drop). Place all subject(s) STRICTLY on the INTERIOR side, using EXISTING furniture only. Do NOT invent any lounger / daybed / platform / deck extension beyond the barrier — there is NOTHING there. If you cannot honor this, return the image UNCHANGED.

"""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # BLOC B — WHO : description du sujet (scenario block) + count
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    who_block = f"""
🎬 WHO — Subject(s) to add — EXACTLY {target_n}, no more, no less :
{scenario_block if scenario_block else "(no scenario selected — return image unchanged)"}

Count check before output : if you placed more than {target_n}, remove the extra(s). If you cannot fit {target_n} on existing furniture / in water without inventing, place FEWER (down to 1, or zero — return unchanged is always acceptable).
"""

    # Pool float (conditionnel, ~35% des piscines) — bloc compact
    pool_float_block = ""
    if pool_float_hint:
        pool_float_block = f"""
🍩 POOL FLOAT (additional element, MUST appear) : add one {pool_float_hint} floating in the existing pool water — engaged with a subject (lounging on it / holding it / next to it). Realistic scale, water displacement, no CGI candy palette. The float CANNOT replace any existing furniture. If realistically impossible, omit it (acceptable).
"""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # BLOC C — HOW : scene preservation + physics + style + negatives
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    how_block = f"""
🛡️ PRESERVE SCENE — everything outside the subject must be PIXEL-IDENTICAL to input :
- DO NOT invent or add furniture, decor, plants, props, drinks, signs, towels, ladders, steps, platforms, decking extensions, walls, windows. The ONLY allowed addition is the subject(s) themselves + their worn/held items (swimwear, hat, sunglasses, drink in hand).
- DO NOT remove, shrink, move, resize, or "beautify" any existing element — including TVs / screens (even black), railings, balustrades, AC units, drainage covers, fire escapes, cables, antennas. Ugly stays. The pool keeps its EXACT shape and size.
- DO NOT zoom, crop, or change camera angle / framing / focal length.
- DO NOT change lighting time-of-day or color grading.

⚖️ PHYSICS — subjects must be physically plausible :
- Sit / recline / stand ONLY on EXISTING visible surfaces (lounger, daybed, chair, sofa, solid deck, pool edge, OR submerged in water).
- In water : water level reaches CHEST / sternum / shoulders on standing adults (NEVER knees / thighs / hips — pool looks bottomless). If chest-deep impossible, sit at the pool EDGE with feet dangling instead.
- Multiple subjects in same pool = same water level (geometric coherence).
- Consistent scale between subjects ; correct perspective vs existing furniture (adult ≈ 2× lounger height).
- Match existing lighting direction & color temperature on faces / clothing / shadows.

🎨 STYLE — California-influencer travel aesthetic :
Premium lifestyle photo, warm saturated tones, golden hour ambient, Kodak Portra 800 grain feel, candid travel-magazine moment (never staged catalog). {vibe_mood}.

👤 FACE QUALITY (Nano Banana common failure) :
Photorealistic faces — clear eyes / nose / mouth, natural skin texture, no smudge / no melted features. For small/medium-distance subjects, prefer 3/4 angle, sunglasses, or hat brim shadow to mask details.

⛔ NEGATIVE — these break the photo, avoid absolutely :
- Inventing furniture / decor / platforms / steps / extra deck → BIGGEST failure mode
- Subjects in physically impossible positions : walking on water, floating dry, standing on a daybed top, leaning over rooftop edge, on the wrong side of a railing / barrier / glass panel
- Distorted faces, blurry / faceless / melted / mannequin-like, eyeless, plastic CGI skin
- Lingerie / sheer / micro-bikini / nipple visible / staged sultry pose / heavy contoured makeup. Swimwear stays tasteful (Reformation / Solid&Striped aesthetic).
- Smartphones in subject's hand, flashy jewelry, business attire on a pool scene
- Cartoon / oversaturated CGI palette, harsh HDR, blown highlights, studio strobe flat lighting
"""

    # ━━ Assemble final ━━
    return f"""{barrier_lock_block}{where_block}
{who_block}
{pool_float_block}
{how_block}"""


# --- Stratégie : router selon l'analyse Gemini ---

def _pick_main_action(
    analysis: dict | None,
    category: str | None = None,
    personas_allowed: list[str] | None = None,
    vibe: str | None = None,
    add_character: bool = False,
    persona_override: str | None = None,
    photo_filename: str | None = None,
    image_path: Path | None = None,
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
    has_dark_issue = any(k in issues_str for k in DARK_KEYWORDS)

    # ━━ Détection "nuit" ROBUSTE (regex avec contexte) ━━━━━━━━━━━━━━━━━━━━━━━━
    # Bug observé Martin (11/05/2026) sur booking_022 : issue "présence d'un parking
    # avec voitures en arrière-plan qui NUIT à l'ambiance évasion" → le mot "nuit"
    # (verbe nuire conjugué) matchait NIGHT_KEYWORDS générique → has_night_clue=True
    # → ai_lighting déclenché à tort sur une photo de JOUR + ai_remove_clutter
    # JAMAIS exécuté (parking voitures pas retiré).
    # Fix : on exige un CONTEXTE qui rend "nuit" sans ambiguïté (substantif), pas
    # un simple match de substring. Le verbe nuire conjugué ("qui nuit à...") n'est
    # plus capté à tort. Si Gemini retourne explicitement time_of_day=nuit/crépuscule
    # côté factual, on l'utilise direct (canal le plus fiable).
    night_issue_patterns = (
        " de nuit", " la nuit", "scène nocturne", "ambiance nocturne",
        "ambiance de nuit", "photo de nuit", "shot de nuit", "prise de nuit",
        "en pleine nuit", "vue de nuit", "image de nuit", "cliché de nuit",
        "nocturne", "couché de soleil", "couche de soleil",
        "crépuscule", "crepuscule", "twilight",
    )
    has_night_keyword_in_issues = any(p in issues_str for p in night_issue_patterns)
    has_night_clue = time_of_day in ("nuit", "aube_crepuscule") or has_night_keyword_in_issues

    # ---- Règles métier intransgressibles (priorité décroissante) ----

    # 1. F&B plats : pas d'AJOUT perso ni de génération (risque d'inventer un plat inexistant)
    #    MAIS ai_lighting (nuit→jour) et ai_remove_clutter (retirer câbles/écrans) RESTENT autorisés —
    #    ils ne touchent pas aux plats servis.
    is_food_only = (
        cat == "f_and_b"
        and "cocktail" not in " ".join(factual.get("subjects") or []).lower()
    )

    # 2. Règle stricte NUIT : on transforme en jour via IA (pas de photo de nuit en sortie)
    #    Y compris pour F&B (transformer la lumière n'invente pas un plat — ça change juste l'éclairage).
    if has_night_clue:
        return {
            "action": "ai_lighting",
            "prompt": PROMPT_ENSOLEILLEMENT,
            "reason": f"photo de nuit/crépuscule → forcée en jour ensoleillé (règle brand stricte)",
        }

    # 1.bis F&B sans nuit : si pas de clutter ni autre besoin → fallback warm boost local sans IA générative
    if is_food_only:
        # On laisse les autres règles tourner (clutter, lighting non-night) puis fallback warm boost
        # On ne return pas direct, on laisse passer au cas où il y a du clutter à retirer
        pass

    # 3. Ajout personnage forcé en amont (pipeline calcule l'alternance)
    #    Skip explicite sur F&B plats (risque inventer plat) et piscine_vue_aerienne (figure trop petite)
    if add_character and personas_allowed and not is_food_only and cat != "piscine_vue_aerienne":
        persona = persona_override or (personas_allowed[0] if personas_allowed else "couples")
        safe_zones_block = analysis.get("safe_zones_for_humans") or {}
        safe_zones = safe_zones_block.get("safe_areas") or []
        unsafe_zones = safe_zones_block.get("unsafe_areas") or []
        max_h_raw = safe_zones_block.get("max_recommended")
        try:
            max_h = int(max_h_raw) if max_h_raw is not None else None
        except (ValueError, TypeError):
            max_h = None

        # ━ Récupère la capacity de la photo (nouveau champ Gemini) ━
        # Sert à l'arbre persona × capacity pour adapter le nombre d'humains à placer.
        capacity_block = analysis.get("placement_capacity") or {}
        capacity_total = capacity_block.get("total")
        try:
            capacity_total = int(capacity_total) if capacity_total is not None else None
        except (ValueError, TypeError):
            capacity_total = None

        # ━ Skip strict UNIQUEMENT si Gemini a explicitement dit max_h == 0 ━
        # max_h == 0 = Gemini certain qu'il n'y a aucune place (ex: vue purement
        # architecturale, gros plan objet). On respecte.
        if max_h is not None and max_h == 0:
            return {
                "action": "local_warm_boost",
                "prompt": None,
                "reason": "ajout perso skip (Gemini : max_h=0, photo non habitée par design)",
            }

        # ━ Fallback safe_zones si Gemini retourne vide MAIS la photo est candidate ━
        # Avant : skip strict si safe_zones=[] → trop conservateur, on ratait des slots 1
        # pour des photos évidentes (piscine avec transats vides). Maintenant : on injecte
        # des safe_zones GÉNÉRIQUES MAIS SÛRES par catégorie (toujours en référence à du
        # mobilier visible courant pour cette catégorie). Gemini Image reste contraint
        # par le prompt persona qui dit "NE PAS INVENTER de décor".
        used_fallback = False
        if not safe_zones:
            fallback = _fallback_safe_zones_by_category(cat)
            if fallback:
                safe_zones = fallback
                used_fallback = True

        # Si même après fallback c'est vide (catégorie pas listée) → skip propre
        if not safe_zones:
            return {
                "action": "local_warm_boost",
                "prompt": None,
                "reason": f"ajout perso skip (catégorie '{cat}' sans fallback safe_zone disponible)",
            }

        # ━ Pool float occasionnel (déterministe par filename, voir pick_pool_float_hint) ━
        # Skip auto si la photo a déjà des bouées (cf. analysis passé en arg).
        pool_float = pick_pool_float_hint(cat, vibe, photo_filename, analysis=analysis)
        fallback_tag = " [fallback safe_zones]" if used_fallback else ""

        # ━━ V5 Vision-Generated Scenario (Martin 13/05/2026) ━━━━━━━━━━━━━━━━━━━
        # Au lieu d'utiliser le catalogue Python hardcoded (qui est aveugle à la
        # photo réelle), on demande à Gemini Vision de RÉDIGER le scenario
        # spécifiquement adapté à CETTE photo. Coût : +$0.001/photo.
        #
        # Activé par défaut. Pour rollback : export USE_LEGACY_SCENARIO_CATALOG=1
        # Si scenario_writer échoue (image absente, API down, JSON malformé) →
        # fallback automatique vers l'ancien catalogue (pas de break pipeline).
        scenario_block_override = None
        scenario_writer_meta = None
        chosen_anchor = None  # Mono-zone (Martin 15/05/2026) — anchor unique choisi par Vision
        if os.getenv("USE_LEGACY_SCENARIO_CATALOG") != "1" and image_path is not None:
            try:
                from scenario_writer import write_scenario
                sw_result = write_scenario(
                    image_path=image_path,
                    category=cat,
                    vibe=vibe,
                    persona=persona,
                )
                if sw_result.get("feasibility") == "ok" and sw_result.get("scenario_block"):
                    scenario_block_override = sw_result["scenario_block"]
                    chosen_anchor = sw_result.get("primary_anchor") or None
                    # Optionnel : si Vision dit max_subjects < ce que Python calcule,
                    # on respecte Vision (= sa lecture de la photo, plus fiable)
                    sw_max = sw_result.get("max_subjects_realistic")
                    if sw_max and sw_max < (max_h or 99):
                        max_h = sw_max
                scenario_writer_meta = {
                    "used": scenario_block_override is not None,
                    "feasibility": sw_result.get("feasibility"),
                    "skip_reason": sw_result.get("skip_reason"),
                    "max_subjects_realistic": sw_result.get("max_subjects_realistic"),
                    "primary_anchor": sw_result.get("primary_anchor"),
                    "estimated_subject_scale_pct": sw_result.get("estimated_subject_scale_pct"),
                    "uses_pool_float_joker": sw_result.get("uses_pool_float_joker", False),
                    "pitfalls": sw_result.get("pitfalls_specific_to_this_photo"),
                    "cost_usd": (sw_result.get("_meta") or {}).get("cost_usd", 0),
                    "duration_ms": (sw_result.get("_meta") or {}).get("duration_ms", 0),
                }
            except Exception as e:
                print(f"[scenario_writer] failed for {photo_filename}: {e} — fallback catalogue")
                scenario_writer_meta = {"used": False, "error": str(e)[:200]}

        return {
            "action": "ai_add_character",
            "prompt": build_persona_prompt(
                persona, cat, vibe,
                safe_zones=safe_zones, unsafe_zones=unsafe_zones,
                max_humans=max_h, capacity=capacity_total,
                pool_float_hint=pool_float,
                scenario_block_override=scenario_block_override,
                chosen_anchor=chosen_anchor,
            ),
            "reason": f"ajout personnage IA ({persona}, target={compute_target_humans(persona, capacity_total or 2)}) sur {cat or 'scène vide'}{fallback_tag}" + (f" + bouée 🍩 {pool_float[:30]}…" if pool_float else "") + (" [Vision scenario, mono-anchor]" if scenario_block_override else ""),
            "scenario_writer": scenario_writer_meta,
            "persona_used": persona,
            "capacity_used": capacity_total,
            "pool_float_used": pool_float,
            "safe_zones_fallback_used": used_fallback,
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
        "sac", "bag", "extincteur", "hose", "tuyau",
        # NB : "barrière" retiré — trop générique, déclenchait à tort sur pool fences.
        # Les vraies barrières temporaires (chantier, hazard tape) sont identifiées via
        # "détritus" ou figurent dans clutter_to_remove explicite avec un wording précis.
        # Eyesores techniques/structurels
        "caméra", "camera", "surveillance", "cctv",
        "escalier de secours", "fire escape", "issue de secours",
        "antenne", "antenna", "parabole", "satellite",
        "climatiseur", "ac unit", "air conditioner", "ventilation",
        "vmc", "extracteur", "grille", "gaine",
        # Logos / marques tierces
        "logo", "logos", "branding", "marque", "brand", "label", "sponsor",
    )
    has_clutter_in_issues = any(k in issues_str for k in clutter_keywords_in_issues)
    if clutter_list or has_clutter_in_issues:
        clutter_desc = ", ".join(clutter_list[:3]) if clutter_list else "éléments parasites mentionnés en issues"
        # ━ 12/05/2026 : on passe la liste EXPLICITE à Gemini Image via le builder.
        # Avant : prompt générique → Gemini ne savait pas quoi cibler (ex : bouée de
        # sauvetage rouge identifiée par Vision mais pas retirée par Image).
        # Maintenant : la liste textuelle est en TÊTE du prompt avec "REMOVE EXACTLY THESE".
        clutter_targets = clutter_list if clutter_list else [issues_str[:200]] if has_clutter_in_issues else None
        return {
            "action": "ai_remove_clutter",
            "prompt": build_remove_clutter_prompt(clutter_targets),
            "reason": f"nettoyage clutter : {clutter_desc}",
            "clutter_targets": clutter_targets,
        }

    # 5b. Cadrage off détecté → on NE FAIT PAS de ai_recompose (qui invente du décor pour
    # remplir les bords du recadrage). Si Gemini avait jugé un crop pertinent, il aurait
    # rempli `recommended_crop` qui est déjà géré en local Pillow par _maybe_crop_step.
    # Si juste mention "horizon penché" / "asymétrique" sans coordonnées : on accepte la photo
    # telle quelle (pas de redressement automatique sécurisé sans recadrage IA risqué).

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
    """DEPRECATED 12/05/2026 — Martin : "tej les anciens crops Gemini, on les corrige
    dans les formats de sortie multi-format".

    Le crop local Pillow basé sur recommended_crop Gemini était une optimisation de cadrage
    redondante avec la step 5 multi-format qui re-crop tout pour chaque format cible.
    En plus, ce crop modifiait le cadrage AVANT les steps IA → risque de dérive amplifiée
    par les retouches. On le désactive : on garde la photo enhanced au cadrage natif et
    le multi-format crop fait le travail de cadrage final par format de sortie.

    Retourne toujours None. Code laissé en commentaire pour rétro-compat / debug rapide.
    """
    return None
    # ━━ Ancien comportement (gardé pour référence) ━━━━━━━━━━━━━━━━━━━━━━━━━
    if not analysis:
        return None
    rec = analysis.get("recommended_crop") or {}
    if not rec.get("should_crop"):
        return None

    # ━ Anti-coupure humain ━
    # Cas typique : photo de yoga/sport/lifestyle où Gemini suggère un crop "pour recentrer
    # sur l'amenity" qui finit par couper la tête de la personne.
    # Si humain présent ET bien visible → on skip le crop.
    factual = analysis.get("factual") or {}
    presence = (factual.get("human_presence_type") or "").lower()
    human_count = factual.get("human_count") or 0
    if human_count > 0 and presence in ("full_visible", "fully visible", "complete"):
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
    """Vrai si Gemini a identifié du clutter (objets, eyesores techniques, ou logos tiers)."""
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
        # Eyesores techniques
        "caméra", "camera", "surveillance", "cctv",
        "escalier de secours", "fire escape", "issue de secours",
        "antenne", "antenna", "parabole",
        "climatiseur", "ac unit", "air conditioner", "ventilation",
        "vmc", "extracteur", "grille", "gaine",
        # Logos / marques tierces
        "logo", "logos", "branding", "marque", "brand", "label", "sponsor",
    )
    return any(k in issues_str for k in keywords)


# ━━ Fallback safe_zones par catégorie ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Quand Gemini Vision retourne safe_zones_for_humans.safe_areas = [] sur une
# photo qui est manifestement candidate (transats vides + piscine bien cadrée),
# on doit pouvoir quand même placer un humain SANS que l'IA invente du décor.
# La stratégie : fournir des safe_zones GÉNÉRIQUES qui réfèrent toujours à du
# mobilier ou éléments QUASI-CERTAINS d'être visibles dans la catégorie. Le
# prompt persona reste contraint par "DO NOT INVENT decor" — donc même si la
# safe_zone ne matche pas pile l'image, l'IA n'inventera pas (elle skippera).
#
# IMPORTANT : ces strings doivent être SUFFISAMMENT GÉNÉRIQUES pour rester
# valables sur 80%+ des photos de chaque catégorie, mais SPÉCIFIQUES sur le
# type de surface utilisé (mobilier existant uniquement).
_FALLBACK_SAFE_ZONES = {
    "piscine": [
        "allongée sur un transat libre visible au bord de la piscine (premier plan latéral)",
        "debout sur le carrelage/dallage existant du bord de piscine, face caméra, à environ 1m de l'eau",
        "assise sur le rebord de la piscine (uniquement si rebord plat et large visible), jambes dans l'eau",
    ],
    "cabana": [
        "allongée sur le lit/sofa de la cabana visible, position lecture détendue",
        "assise sur le bord du daybed de la cabana, face caméra ou de 3/4",
    ],
    "transat": [
        "allongée sur un transat libre identifiable au premier plan, serviette discrète sous le corps",
    ],
    "rooftop": [
        "debout sur la terrasse, côté intérieur du garde-corps/balustrade, face caméra à environ 1.5m du bord",
        "assise sur un siège/banc/sofa existant visible sur la terrasse",
    ],
    "beach": [
        "allongée sur une serviette posée sur le sable, à l'ombre/proche d'un parasol visible",
        "debout sur le sable, face caméra à l'avant du cadre",
    ],
    "spa": [
        "assise sur un banc ou rebord existant de la salle de soin, peignoir blanc",
    ],
    "f_and_b": [
        "assise à une table dressée existante, face caméra ou de 3/4, posture conviviale",
    ],
    "interieur_commun": [
        "assise sur un fauteuil/sofa/canapé existant visible au premier plan, posture lecture/détente",
    ],
    "exterieur": [
        "debout sur le sol carrelé/dallé/wood deck visible, face caméra à l'avant du cadre",
    ],
    "gym": [
        "utilisant un appareil de musculation/yoga visible, posture concentrée",
    ],
}


def _fallback_safe_zones_by_category(cat: str | None) -> list[str]:
    """Retourne 1-3 safe_zones génériques compatibles avec la catégorie quand
    Gemini Vision n'en a pas identifié. Liste vide si la catégorie n'est pas
    couverte (photo type 'detail'/'facade'/'piscine_vue_aerienne' où on ne
    peut pas placer d'humain à 100% sans risque).
    """
    if not cat:
        return []
    return _FALLBACK_SAFE_ZONES.get(cat.lower(), [])


def pick_strategy(
    analysis: dict | None,
    category: str | None = None,
    personas_allowed: list[str] | None = None,
    vibe: str | None = None,
    add_character: bool = False,
    persona_override: str | None = None,
    photo_filename: str | None = None,
    image_path: Path | None = None,
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
    main_step = _pick_main_action(analysis, category, personas_allowed, vibe, add_character, persona_override, photo_filename, image_path=image_path)

    # ━ Si on a déjà un step IA (clutter / lighting / add_character), on évite le crop additionnel ━
    # Le crop modifie le cadrage, l'IA ensuite peut amplifier la dérive (régénération sur image cropée).
    # On préfère 1 seul step IA propre que crop+IA chaîné.
    if crop_step and main_step["action"].startswith("ai_"):
        crop_step = None

    # Détection : photo nuit/sombre qui doit ÉGALEMENT recevoir un personnage IA
    # (alternance forcée → mais _pick_main_action a retourné ai_lighting et ignoré add_character)
    factual = (analysis or {}).get("factual") or {}
    hints = (analysis or {}).get("technical_hints") or {}
    is_night_or_dark = (
        (factual.get("time_of_day") or "").lower() in ("nuit", "aube_crepuscule")
        or (hints.get("ambiance") or "").lower().startswith("sombre")
    )
    needs_lighting_then_character = (
        add_character
        and personas_allowed
        and main_step["action"] == "ai_lighting"
        and is_night_or_dark
    )

    steps: list[dict] = []
    if crop_step:
        steps.append(crop_step)

    if needs_lighting_then_character:
        # Chaînage : ai_lighting (nuit→jour) puis ai_add_character (sur la version jour)
        cat = (factual.get("category") or "").lower()
        persona = persona_override or (personas_allowed[0] if personas_allowed else "couples")
        safe_zones_block = (analysis or {}).get("safe_zones_for_humans") or {}
        safe_zones = safe_zones_block.get("safe_areas") or []
        unsafe_zones = safe_zones_block.get("unsafe_areas") or []
        max_h_raw = safe_zones_block.get("max_recommended")
        try:
            max_h = int(max_h_raw) if max_h_raw is not None else None
        except (ValueError, TypeError):
            max_h = None
        steps.append({
            "action": "ai_lighting",
            "prompt": PROMPT_ENSOLEILLEMENT,
            "reason": "transformation nuit→jour avant ajout perso (étape 1/2)",
        })
        # V5 : utilise scenario_writer également pour le chaînage lighting→character
        chained_scenario_override = None
        chained_chosen_anchor = None  # mono-zone (Martin 15/05/2026)
        chained_sw_meta = None
        if os.getenv("USE_LEGACY_SCENARIO_CATALOG") != "1" and image_path is not None:
            try:
                from scenario_writer import write_scenario
                sw_result = write_scenario(image_path=image_path, category=cat, vibe=vibe, persona=persona)
                if sw_result.get("feasibility") == "ok" and sw_result.get("scenario_block"):
                    chained_scenario_override = sw_result["scenario_block"]
                    chained_chosen_anchor = sw_result.get("primary_anchor") or None
                    sw_max = sw_result.get("max_subjects_realistic")
                    if sw_max and (max_h is None or sw_max < max_h):
                        max_h = sw_max
                chained_sw_meta = {
                    "used": chained_scenario_override is not None,
                    "feasibility": sw_result.get("feasibility"),
                    "primary_anchor": sw_result.get("primary_anchor"),
                    "estimated_subject_scale_pct": sw_result.get("estimated_subject_scale_pct"),
                    "uses_pool_float_joker": sw_result.get("uses_pool_float_joker", False),
                    "cost_usd": (sw_result.get("_meta") or {}).get("cost_usd", 0),
                }
            except Exception as e:
                print(f"[scenario_writer chain] failed for {photo_filename}: {e}")
                chained_sw_meta = {"used": False, "error": str(e)[:200]}

        steps.append({
            "action": "ai_add_character",
            "prompt": build_persona_prompt(persona, cat, vibe, safe_zones=safe_zones,
                                           unsafe_zones=unsafe_zones, max_humans=max_h,
                                           scenario_block_override=chained_scenario_override,
                                           chosen_anchor=chained_chosen_anchor),
            "reason": f"ajout personnage IA ({persona}) sur scène ensoleillée (étape 2/2)" + (" [Vision scenario, mono-anchor]" if chained_scenario_override else ""),
            "scenario_writer": chained_sw_meta,
        })
    else:
        # Cas spécial : ajout perso ET clutter détecté → on chaîne clutter avant add_character
        if main_step["action"] == "ai_add_character" and _has_clutter(analysis):
            clutter_list = (analysis or {}).get("clutter_to_remove") or []
            clutter_desc = ", ".join(clutter_list[:3]) if clutter_list else "objets parasites détectés"
            steps.append({
                "action": "ai_remove_clutter",
                # Idem : on cible explicitement les éléments listés par Vision
                "prompt": build_remove_clutter_prompt(clutter_list if clutter_list else None),
                "reason": f"pré-nettoyage clutter avant ajout perso : {clutter_desc}",
                "clutter_targets": clutter_list if clutter_list else None,
            })

        # Si on a déjà cropé ET que l'action principale est juste warm_boost (rien d'urgent), on saute le warm
        if not (crop_step and main_step["action"] == "local_warm_boost"):
            steps.append(main_step)

    # Sécurité : si la liste est vide (cas dégénéré), au moins warm_boost
    if not steps:
        steps.append(main_step)

    # ━━ Pool float standalone (Martin 11/05/2026) ━━
    # Si la photo est piscine ET qu'aucune action ai_add_character n'est dans les steps
    # (sinon le float est DÉJÀ injecté dans le prompt persona via pool_float_hint),
    # on tire le hint séparément. Si déclenché → on ajoute un step ai_add_pool_float
    # AVANT le warm_boost final (pour qu'il soit retraité par le LUT brand).
    # Le seed est déterministe par filename → comportement reproductible en replay.
    factual_ = (analysis or {}).get("factual") or {}
    primary_cat = (factual_.get("category") or "").lower()
    already_has_character = any(s.get("action") == "ai_add_character" for s in steps)
    if not already_has_character:
        float_hint = pick_pool_float_hint(primary_cat, vibe, photo_filename, analysis=analysis)
        if float_hint:
            # Détection vue aérienne pour appliquer les contraintes perspective top-down
            # (Martin 15/05/2026, bug Moxy Miami : cygne 3D ajouté sur photo top-down)
            is_aerial_view = "aerienne" in primary_cat or "aerial" in primary_cat
            # Insertion juste avant un éventuel local_warm_boost final (pour que le warm_boost
            # apparaisse comme une dernière étape de finition). Si pas de warm_boost, on
            # ajoute en fin.
            float_step = {
                "action": "ai_add_pool_float",
                "prompt": build_pool_float_only_prompt(float_hint, is_aerial=is_aerial_view),
                "reason": f"ajout bouée 🍩 ({float_hint[:40]}…) — playful touch sur piscine" + (" [aerial perspective]" if is_aerial_view else ""),
                "pool_float_used": float_hint,
                "is_aerial_view": is_aerial_view,
            }
            # Si dernière étape est local_warm_boost → insère avant ; sinon ajoute en fin
            if steps and steps[-1]["action"] == "local_warm_boost":
                steps.insert(-1, float_step)
            else:
                steps.append(float_step)

    # L'action "principale" pour le badge front = la plus marquante (priorité IA > smart_crop > warm)
    priority = {"ai_add_character": 7, "ai_remove_people": 6, "ai_remove_clutter": 5,
                "ai_add_pool_float": 4.5, "ai_lighting": 4, "ai_recompose": 3,
                "local_smart_crop": 2, "local_warm_boost": 1}
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
    # Expose persona si un step ai_add_character a été utilisé
    persona_step = next((s for s in steps if s.get("persona_used")), None)
    if persona_step:
        out["persona_used"] = persona_step["persona_used"]
    # Expose pool_float si un step l'a utilisé (soit dans add_character, soit standalone)
    float_step = next((s for s in steps if s.get("pool_float_used")), None)
    if float_step:
        out["pool_float_used"] = float_step["pool_float_used"]
    # ━━ LUT profile adaptatif (12/05/2026, Martin retour "trop jaune") ━━
    # On laisse brand_lut.pick_profile décider selon ambiance+palette de Gemini Vision.
    # Photo lumineux-chaud aligned-warm → "soft" (pas re-pousser le warmth)
    # Photo lumineux-froid / off-brand   → "strong"
    # Sinon                              → "medium"
    out["lut_profile"] = brand_lut.pick_profile(analysis)
    return out


# Heuristique pour repérer les photos candidates ajout personnage quand Gemini ne le retourne pas explicitement.
# Catégorie compatible : amenities + intérieur commun (sauf chambre, interdite par règle métier).
AI_ADD_OK_CATEGORIES = {
    "cabana", "transat", "piscine", "rooftop", "beach", "exterieur",
    "interieur_commun", "gym",
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


_BUSINESS_RULE_EXCLUDED_CATS = {
    # Catégories où enhance.py va SKIPPER l'ajout perso quoi qu'il arrive (règles métier
    # hard-codées plus bas dans pick_strategy). On exclut DE TÊTE pour que l'alternance
    # `prev_will_have_human` (calculée en app.py AVANT pick_strategy) ne compte pas
    # faussement ces photos comme "humain ajouté".
    "piscine_vue_aerienne",  # figure trop petite vue aérienne
    "f_and_b",                # risque d'inventer un plat
    "chambre",                # règle Day Pass : pas de chambre
    "staff",                  # photo dédiée staff, on n'ajoute pas un client par-dessus
    "detail",                 # gros plan d'un objet, pas de place pour humain
    "facade",                 # vue purement architecturale du bâtiment
    "autre",                  # cat fourre-tout, par défaut pas de candidat
}


def is_add_character_candidate(analysis: dict) -> bool:
    """Vrai si la photo est candidate à un ajout personnage IA.

    Source de vérité (par ordre) :
      1. Guard CATÉGORIES MÉTIER EXCLUES (priorité absolue) : si la cat est dans
         _BUSINESS_RULE_EXCLUDED_CATS, on retourne False MÊME SI Gemini dit candidate=True.
         Sans ce guard, l'alternance app.py compte faussement un "humain ajouté" sur ces
         slots qui seront skippés par enhance.py → casse l'alternance → cascade de slots
         sans humain (bug observé Martin 11/05/2026 sur pack avec piscine_vue_aerienne + f_and_b).
      2. Guard SHOT_TYPE problématique : aerial → toujours False (figure trop petite,
         Gemini Image génère un humain mal proportionné), wide sans human_can_be_prominent
         → False (cas où l'amenity domine et l'humain serait trop petit ou perdu dans
         le cadre). Bug observé Martin 12/05/2026 sur booking_001 (exterieur shot_type=aerial,
         humains ajoutés au bord de piscine vus de très loin = échelle complètement faussée).
      3. Champ explicite Gemini ai_add_character_candidate.is_candidate
      4. Heuristique : catégorie ∈ AI_OK + pas de présence humaine narrative
    """
    if not analysis:
        return False

    factual = analysis.get("factual") or {}
    cat = (factual.get("category") or "").lower()

    # ━ Guard #1 : catégories métier exclues — court-circuit indépendant de Gemini ━
    if cat in _BUSINESS_RULE_EXCLUDED_CATS:
        return False

    # ━ Guard #2 : shot_type problématique (échelle humain irréaliste) ━
    # (Martin 18/05/2026) — Rollback du fix "consulter safe_zones" : il rendait
    # éligibles des photos wide où Nano Banana plaçait un humain qui RECOUVRAIT
    # la piscine en partie (transformation décor pour faire de la place). Le
    # signal Gemini `human_can_be_prominent=False` reste pertinent pour bloquer
    # ces cas avant qu'ils n'arrivent.
    shot_block = analysis.get("shot_type") or {}
    shot_t = (shot_block.get("type") or "").lower()
    human_can_be_prominent = bool(shot_block.get("human_can_be_prominent"))
    if shot_t == "aerial":
        # Vue drone / aérienne : l'humain serait minuscule (< 30px) ou complètement
        # faussé par Gemini Image. La photo se passe d'humain.
        return False
    if shot_t == "wide" and not human_can_be_prominent:
        # Wide où Gemini lui-même a dit que l'humain ne pourra PAS être proéminent
        # (= pas de premier plan avec mobilier accueillant). Le résultat sera mal
        # proportionné ou perdu dans le cadre.
        return False

    ai_block = analysis.get("ai_add_character_candidate")
    if isinstance(ai_block, dict) and isinstance(ai_block.get("is_candidate"), bool):
        return ai_block["is_candidate"]

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

def upscale_lanczos_inplace(path: Path, factor: float = FINAL_UPSCALE_FACTOR) -> dict | None:
    """Upscale Lanczos en place. Retourne metadata ou None si factor ≤ 1.0.

    Pourquoi inplace : on remplace le fichier final pour que le pipeline aval
    (multi_format_cropper, PDF export, ZIP) travaille directement sur la HD.

    Lanczos = interpolation classique :
    - N'ajoute PAS de détail réel (vs Real-ESRGAN qui hallucinerait des textures)
    - Préserve les arêtes nettes (mieux que Bicubic / Bilinear)
    - Coût zéro, ~0.2-0.5s par photo
    """
    if factor <= 1.0:
        return None
    try:
        with Image.open(path) as im:
            w, h = im.size
            new_w, new_h = int(w * factor), int(h * factor)
            upscaled = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
            # Préserve le format source : JPEG quality 92 (sweet spot poids/qualité)
            fmt = (im.format or "JPEG").upper()
            save_kwargs = {"quality": 92, "optimize": True} if fmt in ("JPEG", "JPG") else {}
            # Garde le format d'origine (JPEG/PNG/WEBP). PIL infère depuis l'extension.
            upscaled.save(path, **save_kwargs)
        return {
            "method": f"lanczos_x{factor:g}",
            "source_size": [w, h],
            "final_size": [new_w, new_h],
            "duration_ms": 0,  # négligeable, pas mesuré
            "cost_usd": 0.0,
        }
    except Exception as e:
        # Si l'upscale plante on garde la photo originale plutôt que de tout casser
        print(f"  [upscale] échec sur {path.name} : {e}", file=__import__('sys').stderr)
        return {"method": "lanczos_failed", "error": str(e)[:120]}


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

    # ━ Garde-fous robustes contre les crops Gemini douteux ━
    # 1. Si crop trop agressif (< 70% conservé) : skip, on n'est pas sûr de la fiabilité Gemini
    # 2. Si crop trop léger (> 92% conservé) : skip aussi, le gain visuel est négligeable et
    #    chaque crop ajoute du risque (changement aspect ratio, etc.)
    area_ratio = ((x_max - x_min) * (y_max - y_min)) / (w * h)
    if area_ratio < 0.70:
        img.save(output_path, quality=92)
        return {
            "duration_ms": int((time.time() - t0) * 1000),
            "cost_usd": 0,
            "method": f"pillow_crop_skipped (box trop agressive {area_ratio:.0%} < 70%)",
            "framing_changed": False,
        }
    if area_ratio > 0.92:
        img.save(output_path, quality=92)
        return {
            "duration_ms": int((time.time() - t0) * 1000),
            "cost_usd": 0,
            "method": f"pillow_crop_skipped (gain négligeable {area_ratio:.0%} > 92%)",
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


def enhance_local_warm(input_path: Path, output_path: Path, profile: str | None = None) -> dict:
    """Applique la LUT brand Dayuse (= ton cible cohérent inter-photos / inter-hôtels).

    Paramètres dans config/brand_lut.json. C'est ce qu'on applique aux photos déjà
    conformes brand : pas besoin d'IA, juste l'harmonisation tonale.

    Args:
        profile : "soft"|"medium"|"strong" — chosen by `brand_lut.pick_profile(analysis)`
                  upstream. Si None, fallback sur les params de config/brand_lut.json.
    """
    t0 = time.time()
    res = brand_lut.apply_brand_lut(input_path, output_path, profile=profile)
    return {
        "duration_ms": int((time.time() - t0) * 1000),
        "cost_usd": 0,
        "method": "brand_lut" + (f"[{profile}]" if profile else ""),
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
    """Retouche via Nano Banana 2. Préserve la composition.

    Si Nano Banana retourne du texte au lieu d'une image (cas connu Gemini Image preview),
    on retry 1 fois avec un préfixe forçant la génération d'image. Si encore raté → exception.
    """
    t0 = time.time()
    client = _get_genai_client()

    with open(input_path, "rb") as f:
        image_bytes = f.read()

    # MIME type
    suffix = input_path.suffix.lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
        suffix.lstrip("."), "image/jpeg"
    )

    def _call_with_prompt(p):
        return client.models.generate_content(
            model=model,
            contents=[p, types.Part.from_bytes(data=image_bytes, mime_type=mime)],
        )

    def _extract_image(resp):
        img_data = None
        txt = None
        for part in resp.candidates[0].content.parts:
            if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
                img_data = part.inline_data.data
                break
            if hasattr(part, "text") and part.text:
                txt = part.text
        return img_data, txt

    def _extract_tokens(resp):
        """Récupère input_tokens + output_tokens depuis usage_metadata si dispo."""
        u = getattr(resp, "usage_metadata", None)
        if not u:
            return 0, 0
        return (getattr(u, "prompt_token_count", 0) or 0,
                getattr(u, "candidates_token_count", 0) or 0)

    # 1ère tentative
    response = _call_with_prompt(prompt)
    image_data, text_response = _extract_image(response)
    input_tokens, output_tokens = _extract_tokens(response)

    # Retry si Nano Banana a renvoyé du texte au lieu d'une image (~5-10% des cas, modèle preview)
    if not image_data:
        forced_prompt = (
            "🚨 STRICT IMAGE GENERATION REQUIRED — DO NOT respond with text or descriptions. "
            "Apply the following editing instruction and RETURN THE EDITED IMAGE as your only output. "
            "Any text response will be considered a failure.\n\n"
            f"{prompt}"
        )
        try:
            response2 = _call_with_prompt(forced_prompt)
            image_data, text_response2 = _extract_image(response2)
            it2, ot2 = _extract_tokens(response2)
            input_tokens += it2
            output_tokens += ot2
            if not image_data:
                # Toujours raté → exception explicite avec texte des 2 tentatives pour debug
                raise RuntimeError(
                    f"Nano Banana n'a pas retourné d'image (2 tentatives). "
                    f"Texte 1 : {(text_response or 'aucun')[:120]} | "
                    f"Texte 2 : {(text_response2 or 'aucun')[:120]}"
                )
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Nano Banana retry failed: {e}")

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
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


# --- Orchestrateur ---

def _apply_step(input_path: Path, output_path: Path, step: dict, lut_profile: str | None = None) -> dict:
    """Applique UNE step. Retourne le dict de résultat de la fonction sous-jacente.

    lut_profile : si action == local_warm_boost, on transmet le profil LUT choisi
                  par pick_strategy. Pour les autres actions, ignoré (la LUT post-
                  traitement est appliquée séparément par enhance_one).
    """
    action = step["action"]
    if action.startswith("ai_"):
        if not step.get("prompt"):
            raise RuntimeError(f"action {action} sans prompt")
        return enhance_ai(input_path, output_path, step["prompt"])
    if action == "local_smart_crop":
        return enhance_local_crop(input_path, output_path, step.get("crop_box_pct") or {})
    if action == "local_warm_boost":
        return enhance_local_warm(input_path, output_path, profile=lut_profile)
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
    "shallow_water_illusion": (
        "WATER DEPTH VIOLATION : in your previous output, a standing subject in the pool has water only at the knees / thighs / "
        "hips, which makes the pool look bottomless or like a kiddie pool. THIS IS UNACCEPTABLE. "
        "For ANY standing subject in pool water, the water MUST hide AT LEAST the belly button — preferably the chest/sternum. "
        "Re-position the subject(s) so they are CHEST-DEEP (water at sternum / upper-chest, only upper torso + head visible). "
        "If that's geometrically impossible because of placement → put them sitting on the pool EDGE with feet dangling, "
        "OR swimming horizontally with head + upper back above water, OR don't add them at all. "
        "Verify before output : water hides at minimum the belly button on every standing subject."
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
    "subject_oversized": (
        "🚨 CRITICAL VIOLATION — SUBJECT(S) TOO LARGE : in your previous output, the subject(s) "
        "occupied too much of the frame (head > 15% of frame height on a wide shot, or full "
        "body > 30% of frame width). Real travel photography places subjects as ONE element "
        "AMONG MANY in the scene, NOT as the focal point. "
        "Re-do : place the subject(s) FURTHER BACK / DEEPER in the scene (mid-ground or "
        "background), at a size where their full body occupies AT MOST 20% of the frame width "
        "and their head AT MOST 8% of the frame height. Reference : the subject should be "
        "comparable in scale to existing furniture (a standing person ≈ 2× lounger height — "
        "NOT 4× or 5×). If you cannot fit them that small while keeping them recognizable, "
        "OMIT them entirely (return image with no subject)."
    ),
    "architecture_changed": (
        "DO NOT ALTER THE ARCHITECTURE: walls, structures, decor, plants, water shape, sky, and overall composition must remain identical to the input. Only requested transformations apply."
    ),
    "pool_surface_reduced": (
        "🚨 CRITICAL VIOLATION — POOL SURFACE MODIFIED : in your previous output, the SWIMMING POOL "
        "water surface was shrunk / reshaped / partially covered compared to the input. "
        "THIS IS UNACCEPTABLE — the pool is the primary commercial asset of a hotel photo. "
        "ABSOLUTE RULE for this retry : the pool water surface MUST stay 100% IDENTICAL to the input — "
        "same shape (rectangular stays rectangular, oval stays oval, lagoon shape stays lagoon shape), "
        "same dimensions (every edge in the EXACT same place), same proportion of the frame. "
        "If a subject or furniture conflicts with the pool space, MOVE THE SUBJECT to a different "
        "anchor point AWAY from the water — DO NOT shrink the pool to make room. "
        "Verify before output : trace the contour of the water in the input, trace it in your output, "
        "they must be SUPERIMPOSABLE. Zero pixel of former water may become deck/floor/furniture."
    ),
    "subject_count_wrong": (
        "🚨 SUBJECT COUNT MISMATCH : in your previous output, the number of humans added does NOT "
        "match the target requested in the original task. Re-read the scenario : the target N must "
        "be exactly respected. If you added MORE than N → remove the extras. If you added FEWER "
        "than N → it's acceptable ONLY if you cannot fit them on EXISTING furniture/water without "
        "inventing decor. Count the visible added humans before finalizing — match the target exactly."
    ),
    "pool_float_oversized": (
        "🚨 POOL FLOAT OVERSIZED / WRONG PERSPECTIVE : in your previous output, the inflatable "
        "pool float added is TOO LARGE or in WRONG PERSPECTIVE relative to the scene. "
        "ABSOLUTE RULES for this retry : "
        "(a) The float must cover MAXIMUM 12% of the visible water surface — NOT 25%, NOT 20%, strict 12% max. "
        "(b) Scale anchor : the float must be approximately the SAME size as ONE existing lounger/daybed visible "
        "in the photo. If the lounger is N pixels long, the float is MAX N pixels long (not 2N, not 3N). "
        "(c) Perspective : if the photo is shot from ABOVE (aerial / top-down / drone), the float MUST be rendered "
        "in TOP-DOWN view (flat ellipse / flat elongated shape — never in 3D side perspective). "
        "(d) Reference : a real flamingo float is ~1.5m long, a real donut ~1m diameter, both fit a 5m×3m pool "
        "as a small playful accent, NEVER as a centerpiece covering half the water. "
        "A small natural float in a corner is INFINITELY better than a giant misplaced one. "
        "If you cannot honor these rules → DO NOT add the float (return image unchanged)."
    ),
    "architecture_invented": (
        "🚨 CRITICAL VIOLATION — FABRICATED ARCHITECTURE : in your previous output, you "
        "INVENTED a structural element that does not exist in the input. Typical cases : "
        "you transformed an opaque alcove / display / wall panel INTO a window opening onto "
        "a sunlit exterior view ; you added a new skylight ; you replaced a wall with a glass "
        "facade ; you fabricated a city skyline behind a former opaque surface. "
        "ABSOLUTE RULE : you may ONLY change LIGHTING QUALITY (intensity, warmth, direction) "
        "on the existing pixels. NEVER add new windows, new openings, new views, new "
        "architectural elements. If the original has an opaque alcove with neon lights, the "
        "output MUST keep that opaque alcove — only the light hitting it may change. "
        "Re-do the transformation : keep ALL walls, displays, panels, alcoves, ceilings, "
        "columns EXACTLY as they are in the input. Only adjust the global light tone."
    ),
    "lighting_break": (
        "MATCH EXISTING LIGHTING: shadows on added subjects must follow the same direction and softness as the existing shadows in the photo. No mismatched key light, no different time of day on the subject vs the scene."
    ),
    "decor_elements_lost": (
        "🚨 CRITICAL VIOLATION — DECORATIVE ELEMENTS REMOVED : in your previous output, you "
        "DELETED decorative elements that were present in the input (planters with plants, "
        "vases, decorative objects, cushions, art pieces, lamps). These elements are PART "
        "of the venue identity and MUST be preserved EXACTLY as in the input. "
        "Re-do : keep ALL plants, planters, vases, decorations, cushions, objects on tables, "
        "art pieces, lamps, and any decorative item visible in the input photo. Only change "
        "the lighting QUALITY (color temperature, intensity, direction) on the existing pixels — "
        "NEVER remove a planter even if it 'doesn't fit a sunlit aesthetic'. The plants stay. "
        "The vases stay. The art stays. EVERYTHING stays."
    ),
}


def _swap_scenario(prompt: str, from_zones: list[str], to_zone: str) -> tuple[str, bool, str | None]:
    """Remplace le bloc scenario du prompt par un scenario `to_zone` plus safe.

    Cherche d'abord lequel des `from_zones` est présent dans le prompt (pour tous les
    personas connus), puis swap par le scenario `to_zone` du même persona.

    Returns: (new_prompt, was_swapped, swapped_from_zone)
    """
    for persona in ("couples", "solos", "families", "small_groups", "groups"):
        for from_zone in from_zones:
            src = _SCENARIO_CATALOG.get((persona, from_zone))
            dst = _SCENARIO_CATALOG.get((persona, to_zone))
            if src and dst and src in prompt:
                return prompt.replace(src, dst), True, f"{persona}/{from_zone}"
    return prompt, False, None


# Violations pour lesquelles on bascule vers un scenario "safe haven" au retry.
# Logique : si la 1ère tentative a inventé du mobilier OU remodelé la scène,
# c'est que le scenario demandé était trop spécifique vs le décor réel. Au lieu
# de répéter, on bascule vers POOL_EDGE (assis au bord, pieds dans l'eau) qui
# ne nécessite AUCUN mobilier inventable — la photo doit juste avoir une piscine.
# Fallback secondaire OUTDOOR_DECK (debout sur le sol) si pas de piscine.
_SCENARIO_SWAP_TARGETS = {
    "invented_furniture": ("pool_edge", "outdoor_deck"),
    "architecture_changed": ("pool_edge", "outdoor_deck"),
    "scene_regenerated": ("pool_edge", "outdoor_deck"),
    "shallow_water_illusion": ("pool_edge", None),  # already covered
    "subject_on_water": ("pool_edge", "outdoor_deck"),
    # subject_wrong_side_barrier : swap vers pool_edge (sujet ASSIS sur le rebord
    # piscine, pieds dans l'eau, du bon côté) — c'est la position la plus safe
    # qui ne nécessite aucun mobilier inventé et garde le sujet visible côté safe.
    "subject_wrong_side_barrier": ("pool_edge", "outdoor_deck"),
    # pool_surface_reduced (Martin 15/05/2026, Gates Hotel SB) : Nano Banana a rétréci
    # la piscine pour faire de la place au sujet. Swap vers outdoor_deck (sujet sur le
    # sol/dalle, AWAY from water) pour éliminer le conflit géographique avec la piscine.
    "pool_surface_reduced": ("outdoor_deck", "pool_edge"),
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Préfixe de RETRY "preserve pool" (Martin 15/05/2026 — bug Gates Hotel South Beach)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Quand un retry est déclenché après invented_furniture / architecture_changed /
# pool_surface_reduced (toutes les violations qui indiquent que Nano Banana s'est mis
# à recomposer la scène pour caser le sujet), on prepend ce bloc EN TÊTE du prompt
# retry. Objectif : faire passer le message "préserve la piscine ET le décor" AVANT
# même que Nano Banana lise le scenario.
#
# Différent du `_reinforced_prompt` qui empile des [CRITICAL — RETRY AFTER VIOLATION]
# header par violation : ce préfixe est COURT, prioritaire, et axé spécifiquement sur
# la préservation du décor structurel — pas sur la correction d'une violation.
_POOL_PRESERVE_PREFIX = (
    "🚨 RETRY CONTEXT — DECOR PRESERVATION OVERRIDE (read this FIRST) :\n"
    "Your previous attempt FAILED because you modified the scene's STRUCTURE while placing the subject. "
    "On THIS retry, the following rules SUPERSEDE every other instruction below :\n"
    "  1. THE POOL WATER SURFACE MUST STAY 100% IDENTICAL — same shape, same dimensions, "
    "every edge in the EXACT same place. Zero pixel of former water may become deck/floor.\n"
    "  2. NO NEW FURNITURE may appear — only existing visible loungers/daybeds/chairs may be used.\n"
    "  3. NO EXISTING DECOR may disappear — every plant, planter, vase, lamp, art piece stays.\n"
    "  4. THE FRAME MUST STAY IDENTICAL — no crop, no zoom, no recentering on the subject.\n"
    "If the requested subject placement would CONFLICT with these rules (e.g., the anchor would force "
    "the pool to shrink), then MOVE the subject to a different anchor point AWAY from the water/decor — "
    "DO NOT modify the scene to make space. If no compatible anchor exists, return the input UNCHANGED "
    "rather than damage the scene.\n\n"
    "[ORIGINAL TASK INSTRUCTION FOLLOWS — apply it UNDER the constraints above]\n\n"
)

# Violations qui déclenchent l'ajout du préfixe pool/decor preserve en tête de prompt retry.
# Logique : ces violations indiquent que Nano Banana a CONFONDU "placer un sujet" avec
# "recomposer la scène pour optimiser le sujet". Le préfixe corrige cette confusion en amont.
_VIOLATIONS_TRIGGERING_POOL_PREFIX = {
    "invented_furniture",
    "architecture_changed",
    "pool_surface_reduced",
    "decor_elements_lost",
    "scene_regenerated",
}

# Liste des zone_types qu'on peut swap (= scenarios "complexes" qui risquent l'invention)
_SWAPPABLE_FROM_ZONES = ["in_water", "lounger", "cabana_daybed", "rooftop_deck",
                          "indoor_seating", "gym_mat", "dining_table"]


# ━ Renforcement contextuel pour ai_add_pool_float + invented_furniture ━━━━━━
# Martin 19/05/2026 — la reinforcement générique "invented_furniture" parle de
# SUJETS placés sur du mobilier inventé, mais sur ai_add_pool_float il n'y a pas
# de sujet : la bouée est seule. Nano Banana invente alors un "support" sous la
# bouée (raft/plateforme/daybed flottant) que le validator détecte comme
# invented_furniture. Le retry actuel n'adresse pas ce cas spécifique.
# Ce bloc remplace le bloc invented_furniture quand l'action est ai_add_pool_float.
_POOL_FLOAT_NO_SUPPORT_REINFORCEMENT = (
    "🚨 CRITICAL VIOLATION ON POOL FLOAT — INVENTED SUPPORT UNDER FLOAT : "
    "in your previous output, you placed the inflatable float ON TOP of a "
    "FABRICATED raft / platform / daybed / floating structure that did NOT exist "
    "in the original photo. This is the most common failure mode and it is "
    "ABSOLUTELY FORBIDDEN.\n\n"
    "STRICT RULES for this retry :\n"
    "(a) The float must sit DIRECTLY on the EXISTING water surface — no support "
    "    of ANY kind underneath, including no transparent platform, no raft, "
    "    no rigid frame, no daybed, no platform of any sort.\n"
    "(b) The float must have NOTHING attached to it : no rope, no platform behind, "
    "    no auxiliary inflatable, no second object floating nearby.\n"
    "(c) The water under the float remains WATER — same color, same ripples as "
    "    in the input. The only visible difference under the float is a soft "
    "    natural shadow from the float itself.\n"
    "(d) If you cannot honor (a) AND (b) AND (c) → DO NOT add the float at all. "
    "    Return the image UNCHANGED. A bare pool is INFINITELY better than a "
    "    pool with a fake raft under a fake float.\n\n"
    "Pre-flight mental check before producing the output :\n"
    "1. Is the float on the water? YES required, not on a platform.\n"
    "2. Is there any new rigid object next to the float? NO required.\n"
    "3. Does the area under/around the float still look like the original pool water? YES required.\n"
    "If any check fails → return the input image unchanged."
)


def _reinforced_prompt(
    original_prompt: str,
    violations: list[str],
    primary_action: str | None = None,
) -> tuple[str, list[str]]:
    """Construit un prompt 'durci' en concaténant les renforcements ciblés + swap scenario.

    Stratégie de retry (Martin 12/05/2026) : un simple header de renforcement ne suffit pas
    quand Gemini Image a inventé du mobilier ou remodelé la scène — l'instruction du scenario
    original (« cabana daybed », « 3 adjacent loungers », etc.) reste plus forte que le
    renforcement et le bug se reproduit.

    Solution : on SWAP entièrement le bloc scenario vers un scenario "safe haven" :
      - `pool_edge` : assis au bord de la piscine, pieds dans l'eau. Ne nécessite AUCUN
        mobilier inventable — il faut juste une piscine et un bord visible.
      - `outdoor_deck` : debout sur le sol existant. Fallback si pas de piscine.

    Args:
        primary_action : l'action IA principale qui a produit la violation
            (ex "ai_add_character", "ai_add_pool_float"). Permet d'adapter le
            renforcement au contexte — ex sur ai_add_pool_float + invented_furniture,
            on utilise un bloc dédié interdisant tout support sous la bouée (Martin
            19/05/2026, photo Sagamore beach aerial).

    Returns: (prompt_durci, applied_strategies)
    """
    if not original_prompt or not violations:
        return original_prompt, []

    applied: list[str] = []
    prompt = original_prompt

    # ━ Stratégie 1 : swap scenario vers safe haven ━
    # Skip si action = ai_add_pool_float (pas de scenario character à swap, et le
    # swap pourrait casser le prompt float-only).
    if primary_action != "ai_add_pool_float":
        for v in violations:
            targets = _SCENARIO_SWAP_TARGETS.get(v)
            if not targets:
                continue
            primary_target, secondary_target = targets

            # Tente swap vers le primary (pool_edge)
            new_prompt, swapped, swapped_from = _swap_scenario(prompt, _SWAPPABLE_FROM_ZONES, primary_target)
            if swapped:
                prompt = new_prompt
                applied.append(f"scenario_swap_{swapped_from.split('/')[1]}_to_{primary_target}")
                break

            # Si pas de match (scenario actuel n'est pas swappable, ex: pool_edge déjà), tente secondary
            if secondary_target:
                new_prompt, swapped, swapped_from = _swap_scenario(prompt, _SWAPPABLE_FROM_ZONES, secondary_target)
                if swapped:
                    prompt = new_prompt
                    applied.append(f"scenario_swap_{swapped_from.split('/')[1]}_to_{secondary_target}")
                    break

    # ━ Stratégie 2 : header de renforcement classique (avec override contextuel) ━
    blocks = []
    for v in violations:
        # Override contextuel pour le combo (ai_add_pool_float, invented_furniture)
        # → on remplace la reinforcement générique par le bloc float-only.
        if primary_action == "ai_add_pool_float" and v == "invented_furniture":
            blocks.append(
                f"[CRITICAL — RETRY AFTER VIOLATION 'invented_furniture' on pool float]\n"
                f"{_POOL_FLOAT_NO_SUPPORT_REINFORCEMENT}"
            )
            applied.append("pool_float_no_support_override")
            continue
        text = VIOLATION_REINFORCEMENT.get(v)
        if text:
            blocks.append(f"[CRITICAL — RETRY AFTER VIOLATION '{v}']\n{text}")
    if blocks:
        header = (
            "PREVIOUS ATTEMPT FAILED automated quality check. The output had violations listed below. "
            "You MUST avoid repeating these mistakes in this new attempt.\n\n"
            + "\n\n".join(blocks)
            + "\n\n[ORIGINAL TASK INSTRUCTION FOLLOWS]\n\n"
        )
        prompt = header + prompt
        applied.append("reinforcement_header")

    # ━ Stratégie 3 : préfixe "preserve pool/decor" en TÊTE absolue ━
    # Si une des violations indique une recomposition de la scène (pool rétrécie,
    # mobilier inventé, décor perdu, scène régénérée), on prepend un bloc COURT qui
    # supersede toutes les autres instructions. Ce bloc passe en PREMIER, AVANT le
    # header de renforcement et AVANT le scenario swappé.
    if any(v in _VIOLATIONS_TRIGGERING_POOL_PREFIX for v in violations):
        prompt = _POOL_PRESERVE_PREFIX + prompt
        applied.append("pool_preserve_prefix")

    return prompt, applied


# Violations qu'on tente de corriger via retry. Les autres (lighting_break, etc. en contexte ai_lighting)
# sont déjà filtrées plus tôt dans ai_validator.
# ⚠️ INVARIANT : toute violation présente dans VIOLATION_REINFORCEMENT (= prompt durci défini)
# DOIT figurer ici, sinon le pipeline détecte mais ne corrige pas. Exception : `lighting_break`
# qui est whitelisté pour ai_lighting (effet voulu nuit→jour modifie inévitablement les ombres).
ACTIONABLE_VIOLATIONS = {
    "invented_furniture",
    "subject_on_water",
    "shallow_water_illusion",       # Bug Martin 12/05/2026 yotel-miami_pool_01 :
                                     # validator détectait "Piscine sans fond" mais retry pas
                                     # déclenché → output IA bugué publié tel quel.
    "subject_on_furniture_top",
    "subject_wrong_side_barrier",
    "subject_oversized",            # Martin 13/05/2026 : Moxy rooftop trio géant
                                     # → retry avec contrainte distance/scale supplémentaire.
    "scene_regenerated",
    "inconsistent_scale",
    "architecture_changed",
    "architecture_invented",         # Critique : fenêtres inventées sur ai_lighting
                                     # (cf. règle métier validator) — retry obligatoire.
    "decor_elements_lost",          # Martin 13/05/2026 : Moxy rooftop trio — plantes/bacs
                                     # disparus après ai_lighting. Pas whitelisté pour lighting
                                     # car la disparition d'éléments décoratifs n'est JAMAIS
                                     # justifiée par un changement de luminosité.
    "pool_surface_reduced",         # Martin 15/05/2026 : Gates Hotel SB — Nano Banana a
                                     # rétréci la piscine sur retry après invented_furniture.
                                     # NON whitelisté pour ai_lighting : un changement de lumière
                                     # ne justifie JAMAIS de modifier la surface d'eau.
    "subject_count_wrong",          # Martin 15/05/2026 : validateur critical fields option B —
                                     # détecte mismatch target_n vs actual humans added.
    "pool_float_oversized",         # Martin 15/05/2026 : bouée trop grosse OU mauvaise perspective
                                     # (3D frontale sur photo top-down). Détecté par validator
                                     # critical fields. Retry avec contraintes scale renforcées.
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

    # Profil LUT adaptatif choisi par pick_strategy selon ambiance Gemini
    # (soft/medium/strong). Propagé aux 2 call sites brand_lut.apply_brand_lut.
    lut_profile = strategy.get("lut_profile") or "medium"

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
            res = _apply_step(current_input, step_out, step, lut_profile=lut_profile)
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
            # Récupère TOUTES les actions IA du chaînage pour unionner les whitelists du validateur.
            # Cas critique : chaînage ai_lighting → ai_add_character → le validateur doit accepter
            # à la fois les violations légitimes de ai_lighting (scene_regenerated, architecture_changed)
            # ET celles de ai_add_character. Sinon faux positif → retry → fallback original.
            ai_steps = [s for s in steps if s["action"].startswith("ai_")]
            ai_actions_chain = [s["action"] for s in ai_steps]

            # ━━ Injection virtuelle "ai_add_pool_float" si un step a utilisé pool_float_hint ━━
            # Bug observé Martin (12/05/2026) sur booking_026 : ai_add_character avec
            # pool_float_hint injecté dans le prompt → bouée ajoutée dans la photo →
            # validator post-IA flag "invented_pool_float" car la whitelist de
            # ai_add_character ne l'inclut pas. Faux positif → la photo paraît
            # "Bouée inventée (hors règle pipeline)" alors qu'on a EXPLICITEMENT
            # voulu cette bouée. Fix : si un step a pool_float_used (bouée VOULUE),
            # on injecte "ai_add_pool_float" comme action virtuelle dans la chaîne
            # → la whitelist union autorise invented_pool_float.
            if any(s.get("pool_float_used") for s in steps):
                if "ai_add_pool_float" not in ai_actions_chain:
                    ai_actions_chain = list(ai_actions_chain) + ["ai_add_pool_float"]

            primary_ai_action = ai_actions_chain[-1] if ai_actions_chain else None
            try:
                ai_validation = ai_validator.validate_ai_output(
                    input_path, output_path,
                    action_context=primary_ai_action,
                    actions_chain=ai_actions_chain,
                )
                total_cost_usd += ai_validation.get("cost_usd", 0)
                total_duration_ms += ai_validation.get("duration_ms", 0)
            except Exception as e:
                ai_validation = {"ok": True, "violations": [], "summary": f"validation skip: {e}"}

            # ━━━ Validateur structuré "critical fields" en parallèle (Martin 15/05/2026, Option B) ━━━
            # Focalisé sur 6 champs critiques (pool shape/surface, water boundary, count, barrier,
            # invented support). Fusionne ses verdicts avec le validateur narratif.
            import re as _re_local
            critical_validation = None
            has_add_char_step = any(s["action"] == "ai_add_character" for s in steps)
            if has_add_char_step:
                # Extraction de la target N depuis le prompt du dernier step IA (regex "EXACTLY N")
                expected_n = None
                if last_ai_step_index is not None:
                    prompt_text = steps[last_ai_step_index].get("prompt", "") or ""
                    m = _re_local.search(r"EXACTLY\s+(\d+)\s+HUMAN", prompt_text)
                    if m:
                        try:
                            expected_n = int(m.group(1))
                        except (ValueError, TypeError):
                            expected_n = None
                try:
                    critical_validation = ai_validator.validate_critical_fields(
                        input_path, output_path,
                        expected_subject_count=expected_n,
                    )
                    total_cost_usd += critical_validation.get("cost_usd", 0)
                    total_duration_ms += critical_validation.get("duration_ms", 0)
                    # Fusion : on ajoute les violations dérivées au verdict narratif (déduplication)
                    derived = critical_validation.get("violations_derived", []) or []
                    existing = ai_validation.get("violations", []) or []
                    merged = list(existing)
                    for v in derived:
                        if v not in merged:
                            merged.append(v)
                    if merged != existing:
                        ai_validation["violations"] = merged
                        ai_validation["ok"] = False
                        # Note les violations qui viennent du structured validator (pour debug UI)
                        ai_validation["violations_from_critical_fields"] = derived
                    # Stocke les field_checks complets pour affichage UI (audit-trail)
                    ai_validation["critical_fields"] = critical_validation.get("field_checks") or {}
                    ai_validation["critical_fields_cost_usd"] = critical_validation.get("cost_usd", 0)

                    # ━━ Évaluation LOI structurées (Martin 15/05/2026, P1) ━━
                    # Calcule PASS/FAIL pour chaque LOI métier (P1 à P13) à partir des
                    # field_checks structurés + violations narratives. Pas de coût additionnel
                    # (calcul Python pur, pas d'appel LLM).
                    try:
                        import laws_engine
                        lois_result = laws_engine.evaluate_lois(
                            field_checks=ai_validation["critical_fields"],
                            violations=ai_validation.get("violations", []),
                        )
                        ai_validation["lois"] = lois_result
                    except Exception as e:
                        ai_validation["lois_error"] = str(e)[:200]
                except Exception as e:
                    # En cas d'échec : on ne bloque pas le pipeline, on note juste l'erreur
                    ai_validation["critical_fields_error"] = str(e)[:200]

            # ━━━ Auto-correction : retry avec prompt durci sur les violations actionables ━━━
            actionable_violations = [
                v for v in (ai_validation.get("violations") or [])
                if v in ACTIONABLE_VIOLATIONS
            ]
            if actionable_violations and last_ai_step_input and last_ai_step_index is not None:
                retry_attempted = True
                last_step = steps[last_ai_step_index]
                # primary_ai_action est passé en context pour permettre des renforcements
                # contextuels (ex: ai_add_pool_float + invented_furniture → bloc dédié
                # "NO support under float", Martin 19/05/2026).
                reinforced, retry_strategies = _reinforced_prompt(
                    last_step.get("prompt", ""),
                    actionable_violations,
                    primary_action=primary_ai_action,
                )
                # Trace les stratégies appliquées sur le step (visible dans ai_validation pour debug UI)
                if retry_strategies:
                    last_step["retry_strategies"] = retry_strategies
                try:
                    res2 = enhance_ai(last_ai_step_input, output_path, reinforced)
                    total_cost_usd += res2.get("cost_usd", 0)
                    total_duration_ms += res2.get("duration_ms", 0)
                    suffix = ",".join(retry_strategies) if retry_strategies else ",".join(actionable_violations[:2])
                    methods.append(f"retry_after_{suffix}")
                    # Re-validation
                    ai_validation2 = ai_validator.validate_ai_output(
                        input_path, output_path,
                        action_context=primary_ai_action,
                        actions_chain=ai_actions_chain,
                    )
                    total_cost_usd += ai_validation2.get("cost_usd", 0)
                    total_duration_ms += ai_validation2.get("duration_ms", 0)

                    # Re-validation critical fields aussi (cohérence avec le 1er pass)
                    if has_add_char_step:
                        try:
                            critical_validation2 = ai_validator.validate_critical_fields(
                                input_path, output_path,
                                expected_subject_count=expected_n,
                            )
                            total_cost_usd += critical_validation2.get("cost_usd", 0)
                            total_duration_ms += critical_validation2.get("duration_ms", 0)
                            derived2 = critical_validation2.get("violations_derived", []) or []
                            existing2 = ai_validation2.get("violations", []) or []
                            merged2 = list(existing2)
                            for v in derived2:
                                if v not in merged2:
                                    merged2.append(v)
                            if merged2 != existing2:
                                ai_validation2["violations"] = merged2
                                ai_validation2["ok"] = False
                                ai_validation2["violations_from_critical_fields"] = derived2
                            ai_validation2["critical_fields"] = critical_validation2.get("field_checks") or {}
                            ai_validation2["critical_fields_cost_usd"] = critical_validation2.get("cost_usd", 0)
                            # ━━ Évaluation LOI post-retry ━━
                            try:
                                import laws_engine
                                lois_result2 = laws_engine.evaluate_lois(
                                    field_checks=ai_validation2["critical_fields"],
                                    violations=ai_validation2.get("violations", []),
                                )
                                ai_validation2["lois"] = lois_result2
                            except Exception as e:
                                ai_validation2["lois_error"] = str(e)[:200]
                        except Exception as e:
                            ai_validation2["critical_fields_error"] = str(e)[:200]

                    still_bad = [
                        v for v in (ai_validation2.get("violations") or [])
                        if v in ACTIONABLE_VIOLATIONS
                    ]
                    # Note l'historique : on garde ai_validation2 mais on signale qu'il y a eu retry
                    # On préserve le SNAPSHOT COMPLET de la 1ère tentative (violations + fields + lois)
                    # pour permettre au UI d'afficher un "historique des tentatives" debug-friendly
                    # quand le fallback tombe (Martin 15/05/2026, P0 UX).
                    ai_validation2["retry_attempted"] = True
                    ai_validation2["violations_before_retry"] = ai_validation.get("violations", [])
                    ai_validation2["critical_fields_before_retry"] = ai_validation.get("critical_fields", {})
                    ai_validation2["lois_before_retry"] = ai_validation.get("lois", {})
                    ai_validation2["summary_before_retry"] = ai_validation.get("summary", "")
                    # Aussi : les stratégies appliquées au retry (swap scenario, préfixe pool, etc.)
                    ai_validation2["retry_strategies"] = retry_strategies if retry_strategies else []
                    ai_validation = ai_validation2

                    if still_bad:
                        # ━ FALLBACK INTELLIGENT (Martin 13/05/2026) ━
                        # Si chaînage type [ai_lighting → ai_add_character] et que add_character
                        # foire, on fallback sur le RÉSULTAT INTERMÉDIAIRE (= la photo jour après
                        # lighting, sans humain). Pas sur l'input brut (= scène nuit).
                        # `last_ai_step_input` pointe sur l'input de la dernière étape IA :
                        #   - Si chaînage : c'est l'output de l'étape précédente (déjà retouchée)
                        #   - Si étape IA unique : c'est l'input brut = même chose qu'input_path
                        try:
                            had_chain = last_ai_step_input != input_path and last_ai_step_input.exists()
                            if had_chain:
                                # On garde la version "après lighting" — la transformation nuit→jour
                                # est conservée, juste l'ajout perso est annulé.
                                shutil.copy(last_ai_step_input, output_path)
                                fallback_to_original = True
                                methods.append("fallback_to_intermediate")
                                ai_validation["fallback_to_original"] = True
                                ai_validation["fallback_kind"] = "intermediate_step"
                                ai_validation["fallback_kept_step"] = steps[last_ai_step_index - 1]["action"] if last_ai_step_index > 0 else "unknown"
                            else:
                                # Pas de chaînage utile : on tombe sur l'input brut (cas par défaut)
                                shutil.copy(input_path, output_path)
                                fallback_to_original = True
                                methods.append("fallback_original")
                                ai_validation["fallback_to_original"] = True
                                ai_validation["fallback_kind"] = "input_raw"
                        except Exception:
                            pass
                except Exception as e:
                    # Retry IA crashe → on garde l'output original IA (pas de fallback automatique)
                    ai_validation["retry_error"] = str(e)[:200]

        # ━━ Sauvegarde des intermédiaires pour debug (Martin 13/05/2026) ━━━━━━━
        # Sur chaînage (ai_lighting → ai_add_character), on conserve l'output de
        # chaque step intermédiaire dans `<enhanced_dir>/_intermediates/` pour
        # pouvoir inspecter visuellement OÙ un bug s'est produit (ex: les plantes
        # ont-elles disparu après step 1 ou step 2 ?). Coût disque négligeable.
        # Désactivable via env : KEEP_AI_INTERMEDIATES=0
        if os.getenv("KEEP_AI_INTERMEDIATES", "1") == "1" and intermediate_paths:
            interm_dir = output_path.parent / "_intermediates"
            interm_dir.mkdir(parents=True, exist_ok=True)
            for p in intermediate_paths:
                try:
                    final_path = interm_dir / p.name
                    if p.exists():
                        p.rename(final_path)
                except Exception:
                    pass
        else:
            for p in intermediate_paths:
                try:
                    p.unlink()
                except Exception:
                    pass

        # ━━━ Post-traitement obligatoire : LUT brand Dayuse (profil adaptatif) ━━━
        # Toutes les photos finales passent par la LUT pour cohérence inter-photos/inter-hôtels.
        # Sauf si la dernière step était DÉJÀ local_warm_boost (= la LUT a déjà été appliquée
        # avec le bon profil par enhance_local_warm).
        last_action = steps[-1]["action"] if steps else None
        brand_lut_applied = False
        if last_action != "local_warm_boost":
            try:
                brand_lut.apply_brand_lut(output_path, output_path, profile=lut_profile)
                methods.append(f"brand_lut[{lut_profile}]")
                brand_lut_applied = True
            except Exception:
                # Si la LUT échoue (rare), on garde l'output sans LUT
                pass

        # ━━━ Upscale Lanczos final ━━━
        # Toutes les photos finales sont upscalées (par défaut ×2 → 1264→2528)
        # pour servir une vraie HD aux UIs Dayuse desktop/Retina. On le fait
        # APRÈS la LUT pour éviter de retraiter 4× plus de pixels en colorimétrie.
        upscale_meta = upscale_lanczos_inplace(output_path)
        if upscale_meta and not upscale_meta.get("error"):
            methods.append(upscale_meta["method"])

        # ━ Message de fallback contextualisé (Martin 19/05/2026, G) ━━━━━━━━━━━
        # Avant : message générique "Validation post-IA échouée 2×". Maintenant on
        # précise l'action qui a foiré + la violation persistante, pour que la UI
        # explique clairement à Martin pourquoi la bouée (ou l'humain) a été annulée.
        if fallback_to_original:
            primary_action = (steps[last_ai_step_index]["action"]
                              if last_ai_step_index is not None and last_ai_step_index < len(steps)
                              else strategy["action"])
            final_violations = (ai_validation or {}).get("violations", []) or []
            violation_str = ", ".join(final_violations[:3]) if final_violations else "violations persistantes"
            if primary_action == "ai_add_pool_float":
                fallback_reason = (
                    f"🛑 Bouée annulée — Nano Banana persiste à inventer du mobilier "
                    f"(`{violation_str}`) malgré le retry avec renforcement spécifique. "
                    f"On garde l'originale plutôt qu'une retouche cassée."
                )
            elif primary_action == "ai_add_character":
                fallback_reason = (
                    f"🛑 Humain annulé — validation post-IA échouée 2× "
                    f"(`{violation_str}`). On garde la photo originale."
                )
            else:
                fallback_reason = (
                    f"🛑 Retouche IA annulée — `{primary_action}` n'a pas passé la "
                    f"validation après retry ({violation_str}). Fallback original."
                )
        else:
            fallback_reason = None

        return {
            "input_path": str(input_path),
            "output_path": str(output_path),
            "action": strategy["action"] if not fallback_to_original else "fallback_original",
            "reason": fallback_reason if fallback_to_original else strategy.get("reason", ""),
            "steps": [{
                "action": s["action"],
                "reason": s.get("reason", ""),
                # Expose le prompt envoyé à Gemini Image pour transparence + debug front (Martin 12/05/2026)
                "prompt": s.get("prompt"),
                "clutter_targets": s.get("clutter_targets"),
                "pool_float_used": s.get("pool_float_used"),
            } for s in steps],
            "method": " → ".join(methods),
            "cost_usd": round(total_cost_usd, 6),
            "duration_ms": total_duration_ms,
            "framing_changed": framing_changed,
            "framing_warning": framing_warning,
            "brand_lut_applied": brand_lut_applied or last_action == "local_warm_boost",
            "upscale": upscale_meta,
            "ai_validation": ai_validation,
            "retry_attempted": retry_attempted,
            "fallback_to_original": fallback_to_original,
            "persona_used": strategy.get("persona_used"),
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
