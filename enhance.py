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
    "Create a realistic, inviting, premium look with a clear sunny daytime ambiance.\n\n"
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
    "in the output, just illuminated by additional warm daylight ambient light.\n"
    "- The light SOURCES visible in the input (windows, lamps, skylights) stay at their "
    "original positions, sizes and shapes — only their COLOR / INTENSITY can change.\n"
    "- Walls, ceilings, floors keep their materials and patterns identical. Tiles, paint, "
    "wood, carpet remain unchanged.\n\n"
    "If you cannot brighten the scene without inventing new windows or removing existing "
    "decor → return the image with ONLY a global warm color/exposure shift on the existing "
    "pixels (no structural change). A photo that is just 'a bit brighter' is acceptable. "
    "A photo with fabricated architecture is REJECTED.\n\n"
    "NEGATIVE PROMPT : new windows, new openings, invented skylights, fabricated city view, "
    "removed artwork, removed neon, replaced wall panels, walls turned into glass facades, "
    "alcoves turned into windows, transformed displays, new architectural elements."
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
def build_pool_float_only_prompt(float_desc: str) -> str:
    """Prompt pour ajouter UNIQUEMENT une bouée dans une photo piscine, sans toucher au reste.

    Utilisé quand la photo n'a pas besoin d'add_character mais qu'on veut quand même
    booster son côté playful (Martin 11/05/2026 : "ça peut être ajouté en plus des humains,
    pas un critère unique").
    """
    return f"""🛑 ABSOLUTE RULE — ADD ONE SINGLE POOL FLOAT, NOTHING ELSE:

You are ONLY allowed to add ONE pool float in the existing pool water of this image — specifically: {float_desc}.

You MUST NEVER add ANY of the following:
- Any human, person, character, body part, hand, leg, shadow of a person
- Any furniture, lounger, daybed, towel, plant, decoration, drink, sign, logo
- Any new architecture, wall, column, ceiling, balustrade, railing
- Any modification to the existing water shape, decking, plants, walls, ceiling, lighting
- Any second float — exactly ONE float, no more
- Any change to the framing, composition, perspective, lighting, color grading

🎯 PLACEMENT RULES for the float:
- Place it IN the existing pool water, in a zone that is currently EMPTY (no swimmers, no decoration in that spot already).
- Pick a natural-looking position : near the center of the water surface, or gently drifting near the edge.
- Realistic SCALE : the float must be proportional to the pool (typically 0.8–1.8m long for a flamingo/swan, ~1m diameter for a donut). It must NEVER cover more than ~25% of the visible water surface.
- Realistic INTEGRATION with the water:
  - Subtle wake / ripple around it
  - Slight reflection of the float's underside on the water
  - Float partially sitting on water, NOT floating dry above it
- Realistic LIGHTING : the float must receive the same sunlight direction as the rest of the scene; cast a soft natural shadow on the water consistent with the existing shadow direction.
- Color/style remain PHOTOREALISTIC — no over-saturated CGI candy palette. Slight wear/dust is fine.

🚫 IF YOU CANNOT add the float naturally according to ALL the rules above → DO NOT add it. Return the image UNCHANGED. A photo without a float is always acceptable. A bad fake float ruins the photo.

🚫 FRAMING LOCK : DO NOT zoom in/out, DO NOT crop, DO NOT change the camera angle, focal length, or any pixel of the image OTHER than the small region where the float sits.

🚫 STRUCTURAL PRESERVATION : every other pixel of the image MUST remain pixel-identical to the input. Same architecture, same walls, same plants, same furniture, same humans (if any present in original), same lighting, same color grading, same shadows except the new one cast by the float.

NEGATIVE PROMPT:
- new humans, new people, hands, legs, body parts
- new furniture, new objects beyond the single float
- duplicated floats, multiple floats, more than one float
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
                       "indoor_seating", "gym_mat", "unknown"}
    """
    z = (zone_text or "").lower()
    # Eau / piscine — priorité sur edge si "in the water" est explicite
    if any(k in z for k in ["in the pool water", "in the water", "in the pool", "pool water",
                              "swimming", "submerged", "wading in"]):
        return "in_water"
    if any(k in z for k in ["pool edge", "pool rim", "rim of the pool", "edge of the pool",
                              "sitting at the edge", "edge of pool"]):
        return "pool_edge"
    if any(k in z for k in ["lounger", "sun lounger", "sunbed", "sun bed", "deck chair", "transat"]):
        return "lounger"
    if any(k in z for k in ["cabana", "daybed", "day bed", "pool bed"]):
        return "cabana_daybed"
    if any(k in z for k in ["dining table", "restaurant table", "around the table",
                              "at the table", "bar counter"]):
        return "dining_table"
    if any(k in z for k in ["rooftop", "roof terrace", "skydeck"]):
        return "rooftop_deck"
    if any(k in z for k in ["yoga mat", "yoga", "gym floor", "stretching mat"]):
        return "gym_mat"
    if any(k in z for k in ["sofa", "armchair", "lounge chair", "bench", "indoor seat"]):
        return "indoor_seating"
    if any(k in z for k in ["deck", "patio", "terrace", "ground", "floor"]):
        return "outdoor_deck"
    return "unknown"


# Catalogue des scenarios. Chaque entrée = (persona, zone_type) → bloc texte précis.
# Le bloc DOIT décrire UNE seule pose, position, attribut. Pas de "ou", pas de "(1)/(2)/(3)".
# Termes anglais car Gemini Image y répond mieux en pratique.
_SCENARIO_CATALOG: dict[tuple[str, str], str] = {

    # ━━ COUPLES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ("couples", "in_water"): (
        "Place exactly TWO subjects in the existing pool water — a mixed-race adult couple "
        "(one woman late 20s, one man late 20s). Both are STANDING in chest-deep water "
        "near the visible center of the pool. Water level on both : sternum / upper-chest "
        "(only upper torso, shoulders, neck and head are above water — belly button, hips, "
        "thighs MUST be fully submerged). She wears a sleek navy one-piece swimsuit. He wears "
        "classic dark swim shorts. They face each other in profile to the camera, sharing a "
        "soft natural smile mid-conversation. Their hands are clasped between them at chest "
        "height in the water. Hair slightly wet at the temples. Soft water ripples around both "
        "bodies. They do NOT look at the camera."
    ),
    ("couples", "pool_edge"): (
        "Place exactly TWO subjects sitting on the dry pool deck right at the pool edge — "
        "a mixed-race adult couple (one woman late 20s, one man late 20s). They sit side by "
        "side with feet and calves submerged in the pool water (water at mid-calf). Both lean "
        "slightly toward each other, sharing a candid laughing moment. She wears a chic bikini "
        "with a thin gold chain ; he wears swim shorts, no shirt. He holds a tall glass of cold "
        "drink in his outer hand. Neither looks at the camera ; they look at each other. Soft "
        "water reflections on their lower legs."
    ),
    ("couples", "lounger"): (
        "Place exactly TWO subjects on two adjacent existing sun loungers visible in the photo "
        "— a mixed-race adult couple (one woman late 20s, one man late 20s). The woman reclines "
        "on the lounger closest to the camera, sunglasses on, reading a slim paperback book. "
        "The man reclines on the adjacent lounger, propped on one elbow, looking out at the "
        "scene with a relaxed half-smile. Both wear stylish swimwear (her: olive one-piece ; "
        "him: navy swim shorts). They do NOT touch ; they share calm relaxed energy. Neither "
        "looks at the camera."
    ),
    ("couples", "cabana_daybed"): (
        "Place exactly TWO subjects together on the existing cabana daybed / pool sofa visible "
        "in the photo — a mixed-race adult couple (one woman late 20s, one man late 20s). They "
        "sit close, the woman leaning her shoulder against his, both gazing out at the pool. "
        "She wears a chic bikini with a light sarong tied at her hips ; he wears swim shorts, "
        "no shirt. He holds a tall iced drink in one hand resting on his knee. Soft mid-day "
        "shadow under the cabana canopy. Neither looks at the camera ; they share a quiet "
        "candid moment."
    ),
    ("couples", "dining_table"): (
        "Place exactly TWO subjects around the existing dining table visible in the photo — "
        "a mixed-race adult couple (one woman late 20s, one man late 20s) sitting across from "
        "each other. The woman is pouring a glass of sparkling water for him while smiling. "
        "Both wear casual smart attire (her: light linen dress, him: linen shirt). One existing "
        "wine glass and one water glass on the table. They are mid-conversation, NOT looking at "
        "the camera."
    ),
    ("couples", "rooftop_deck"): (
        "Place exactly TWO subjects standing on the rooftop deck near the railing (on the safe "
        "interior side of the existing balustrade) — a mixed-race adult couple (one woman late "
        "20s, one man late 20s). They stand close, the woman's shoulder leaning against him, "
        "both looking out at the city skyline (NOT at the camera). She wears a chic light "
        "summer dress, he wears a linen shirt and tailored shorts. He holds a cocktail glass "
        "in his outer hand. Natural late-afternoon warm light on their profiles."
    ),
    ("couples", "outdoor_deck"): (
        "Place exactly TWO subjects standing casually on the existing outdoor deck — a "
        "mixed-race adult couple (one woman late 20s, one man late 20s). They face each other "
        "in profile to the camera, mid-conversation, the woman holding a takeaway coffee cup. "
        "Both wear stylish casual resort attire (her: light dress, him: linen shirt and shorts). "
        "Neither looks at the camera."
    ),
    ("couples", "indoor_seating"): (
        "Place exactly TWO subjects on the existing sofa or lounge chair visible in the photo — "
        "a mixed-race adult couple (one woman late 20s, one man late 20s). The woman sits "
        "cross-legged on one end of the sofa, scrolling on her phone with a half-smile. The man "
        "sits at the other end, an open laptop on his lap, glancing toward her. Both wear casual "
        "smart attire. Neither looks at the camera."
    ),

    # ━━ SOLOS (1 femme adulte) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ("solos", "in_water"): (
        "Place exactly ONE subject in the existing pool water — a mixed-race adult woman late "
        "20s. She is swimming gentle breaststroke in the center of the visible water surface, "
        "her head above water (chin level), arms making soft swim motion with slight wake "
        "behind her. Wet hair slicked back. She wears a sleek black one-piece swimsuit. Soft "
        "mid-day sun on her shoulders. She does NOT look at the camera ; her gaze is directed "
        "slightly ahead of her along the water surface."
    ),
    ("solos", "pool_edge"): (
        "Place exactly ONE subject sitting at the existing pool edge — a mixed-race adult "
        "woman late 20s. She sits on the dry pool deck with her legs dangling into the water "
        "(water at her mid-calf). She wears a stylish white one-piece swimsuit with a thin gold "
        "chain. Sunglasses pushed up in her hair. She is reading a slim paperback book held in "
        "both hands, looking down at the page with a relaxed half-smile. Soft water reflection "
        "on her lower legs. She does NOT look at the camera."
    ),
    ("solos", "lounger"): (
        "Place exactly ONE subject on the existing sun lounger visible in the photo — a "
        "mixed-race adult woman late 20s. She reclines comfortably on the lounger, propped "
        "slightly up on a flat cushion, sunglasses on, slim paperback book held open in one "
        "hand. She wears a chic olive bikini with a thin gold chain. A wide-brimmed straw hat "
        "rests on the lounger next to her. She is reading, gaze on the book — NOT at the camera. "
        "Natural mid-afternoon sun on her body."
    ),
    ("solos", "cabana_daybed"): (
        "Place exactly ONE subject on the existing cabana daybed visible in the photo — a "
        "mixed-race adult woman late 20s. She sits cross-legged with her back against the "
        "cushions, holding a tall iced drink in one hand and her phone in the other. She wears "
        "a chic bikini with a light open sarong tied at her hips. Sunglasses on. She looks "
        "down at her phone with a half-smile — NOT at the camera."
    ),
    ("solos", "rooftop_deck"): (
        "Place exactly ONE subject standing on the rooftop deck near (but on the safe interior "
        "side of) the existing railing — a mixed-race adult woman late 20s. She holds a "
        "cocktail glass in one hand, the other hand resting lightly on the railing. She looks "
        "out at the city skyline, profile to the camera, with a soft serene expression. She "
        "wears a chic light summer dress. Natural late-afternoon warm light on her side."
    ),
    ("solos", "outdoor_deck"): (
        "Place exactly ONE subject standing casually on the existing outdoor deck — a "
        "mixed-race adult woman late 20s. She holds a takeaway coffee cup in one hand, looking "
        "out at the scene with a relaxed half-smile, profile to the camera. She wears a stylish "
        "summer dress. She does NOT look at the camera."
    ),
    ("solos", "indoor_seating"): (
        "Place exactly ONE subject on the existing sofa or lounge chair visible in the photo — "
        "a mixed-race adult woman late 20s. She sits cross-legged at one end, an open laptop "
        "on her lap, glancing at the screen with a focused half-smile. She wears casual smart "
        "attire (light shirt, slim trousers). A coffee cup sits on the nearby existing table "
        "(only if one is clearly visible in the input). She does NOT look at the camera."
    ),
    ("solos", "gym_mat"): (
        "Place exactly ONE subject on the existing yoga mat / gym floor visible in the photo — "
        "a mixed-race adult woman late 20s in a downward-dog yoga pose, focused expression "
        "looking down. She wears matching athleisure (high-waist black leggings and a fitted "
        "sports bra). Natural light on her toned body. She does NOT look at the camera."
    ),

    # ━━ FAMILIES (couple + 1-2 enfants) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ("families", "in_water"): (
        "Place exactly THREE subjects in the existing pool water — a young mixed-race family "
        "(mother late 20s, father late 20s, one child age 6). Both parents stand chest-deep "
        "(water at sternum on adults). The mother holds the child in front of her at the water "
        "surface, helping the child gently splash and laugh. The father stands next to them, "
        "smiling and lightly splashing the water with one hand. Adult swimwear : navy one-piece "
        "for mother, dark swim shorts for father. Child wears a colorful kids' swimsuit. None "
        "look at the camera ; their gaze is on the child / between each other."
    ),
    ("families", "pool_edge"): (
        "Place exactly THREE subjects at the pool edge — a young mixed-race family (mother late "
        "20s, father late 20s, one child age 7). Mother sits on the dry pool deck with her "
        "feet in the water, holding the child's hand who sits beside her with a wide laughing "
        "smile. Father kneels next to them on the deck, smiling at the child. All wear casual "
        "swimwear. None look at the camera."
    ),
    ("families", "lounger"): (
        "Place exactly THREE subjects on existing sun loungers visible in the photo — a young "
        "mixed-race family. Mother reclines on one lounger, smiling at the child age 6 who is "
        "sitting up at her feet showing her a colorful inflatable beach ball. Father reclines "
        "on the adjacent lounger, propped on one elbow, looking at them with a relaxed smile. "
        "All wear casual swimwear. None look at the camera."
    ),
    ("families", "cabana_daybed"): (
        "Place exactly THREE subjects together on the existing cabana daybed — a young "
        "mixed-race family. Father at one end, mother at the other end, the child age 6 sitting "
        "between them with a wide smile, showing the parents a small toy. All wear casual "
        "swimwear / beach attire. They are mid-laugh, none looking at the camera."
    ),
    ("families", "dining_table"): (
        "Place exactly THREE subjects around the existing dining table — a young mixed-race "
        "family. Mother at one side passing a small dish to the child age 7 sitting across "
        "from her. Father next to the child, mid-conversation. Existing wine/water glasses "
        "on the table only. They are sharing a candid laughing meal moment. None looks at "
        "the camera."
    ),
    ("families", "outdoor_deck"): (
        "Place exactly THREE subjects on the existing outdoor deck — a young mixed-race family "
        "casually standing close, the child age 6 between the parents, all smiling at "
        "something just out of frame (off-camera). Mother wears a light summer dress, father "
        "wears linen shirt and shorts, child wears casual summer clothes. None looks at the "
        "camera."
    ),

    # ━━ SMALL_GROUPS (2-3 amis trendy) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ("small_groups", "in_water"): (
        "Place exactly THREE subjects in the existing pool water — three trendy mixed-race "
        "friends, two women and one man, all late 20s. All three stand chest-deep near the "
        "center of the pool, forming a loose triangle, laughing together mid-conversation. "
        "Adult swimwear : two stylish bikinis (one navy, one olive) and dark swim shorts. "
        "Water level at sternum on all three. None look at the camera ; they look at each "
        "other / off-frame."
    ),
    ("small_groups", "pool_edge"): (
        "Place exactly THREE subjects sitting in a row at the existing pool edge — three "
        "trendy mixed-race friends, all late 20s, casual conversation, feet and calves in the "
        "water. Adult swimwear visible. The one in the center is holding a cold drink, telling "
        "a story while the others laugh. None look at the camera."
    ),
    ("small_groups", "lounger"): (
        "Place exactly THREE subjects on three adjacent existing sun loungers — three trendy "
        "mixed-race friends, all late 20s. The center friend sits up reading a magazine ; the "
        "other two recline relaxed with sunglasses on. Adult swimwear visible. None looks "
        "at the camera."
    ),
    ("small_groups", "cabana_daybed"): (
        "Place exactly THREE subjects on the existing cabana daybed / large pool sofa — three "
        "trendy mixed-race friends, all late 20s, sharing a candid laughing moment. Existing "
        "cocktail glasses visible in their hands only if a tray/glasses are already in the "
        "input. None looks at the camera."
    ),
    ("small_groups", "dining_table"): (
        "Place exactly THREE subjects around the existing dining table — three trendy "
        "mixed-race friends late 20s, mid-meal candid moment, one passing a small bread "
        "basket to another. Casual smart attire. None looks at the camera."
    ),
    ("small_groups", "rooftop_deck"): (
        "Place exactly THREE subjects standing on the rooftop deck (on the safe interior side "
        "of the existing railing) — three trendy mixed-race friends late 20s. They form a "
        "loose group facing each other in profile, cocktails in hand, mid-laugh. Casual chic "
        "evening attire. None looks at the camera."
    ),
    ("small_groups", "outdoor_deck"): (
        "Place exactly THREE subjects standing in a loose group on the existing outdoor deck "
        "— three trendy mixed-race friends late 20s, sharing a candid laugh. Casual chic resort "
        "attire. None looks at the camera."
    ),

    # ━━ GROUPS (4-5 amis énergie festive) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ("groups", "in_water"): (
        "Place FOUR subjects in the existing pool water — a group of trendy mixed-race friends "
        "late 20s (two men, two women), all standing chest-deep near the center, laughing "
        "together. Adult swimwear visible. Water level at sternum on all. None looks at the "
        "camera ; festive but tasteful daytime vibe."
    ),
    ("groups", "pool_edge"): (
        "Place FOUR subjects sitting in a row at the existing pool edge with feet in the water "
        "— a group of trendy mixed-race friends late 20s. They are mid-conversation, two of "
        "them mid-laugh. None looks at the camera."
    ),
    ("groups", "lounger"): (
        "Place FOUR subjects on four adjacent existing sun loungers — a group of trendy "
        "mixed-race friends late 20s. They share a candid relaxed moment, one of them sitting "
        "up to talk to the others. None looks at the camera."
    ),
    ("groups", "rooftop_deck"): (
        "Place FOUR subjects standing in a loose semicircle on the rooftop deck (on the safe "
        "interior side of the existing railing) — a group of trendy mixed-race friends late "
        "20s, mid-toast with cocktails in hand. Casual chic evening attire. None looks at "
        "the camera."
    ),
    ("groups", "outdoor_deck"): (
        "Place FOUR subjects standing in a loose semicircle on the existing outdoor deck — a "
        "group of trendy mixed-race friends late 20s, mid-laugh. Casual chic resort attire. "
        "None looks at the camera."
    ),
    ("groups", "dining_table"): (
        "Place FOUR subjects around the existing dining table — a group of trendy mixed-race "
        "friends late 20s, mid-meal candid moment, one of them mid-laugh raising a glass. "
        "Casual smart attire. None looks at the camera."
    ),
}


def pick_human_scenario(
    persona: str,
    category: str,
    safe_zones: list[str] | None,
    capacity: int | None,
) -> dict | None:
    """Choisit UN scenario unique pour cette photo. Retourne None si pas de scenario valide.

    Approche : on regarde la safe_zone N°1 (priorité Gemini) et on map vers un zone_type.
    Si un mapping persona×zone_type existe → on retourne le bloc texte précis.
    Sinon → None (= ne pas ajouter d'humain pour cette photo).
    """
    if not safe_zones:
        return None
    # Catégorie spéciale : vue aérienne piscine → jamais d'humain
    cat_lower = (category or "").lower()
    if cat_lower in ("piscine_vue_aerienne", "facade", "chambre", "staff"):
        return None

    # Map la priority safe_zone
    zone_text = safe_zones[0] if safe_zones else ""
    zone_type = _classify_safe_zone(zone_text)

    # Fallback heuristique selon la catégorie si zone_type unknown
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
            zone_type = "gym_mat"
        elif cat_lower in ("interieur_commun",):
            zone_type = "indoor_seating"
        else:
            zone_type = "outdoor_deck"

    # Cherche le scenario exact (persona, zone_type)
    block = _SCENARIO_CATALOG.get((persona, zone_type))
    if block is None:
        # Fallback : on essaie avec persona=couples si rien d'autre, puis solos
        for fallback_persona in ("couples", "solos", "small_groups"):
            block = _SCENARIO_CATALOG.get((fallback_persona, zone_type))
            if block:
                break
    if block is None:
        return None

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

POOL_FLOATS_OPTIONS = [
    # Classiques iconiques (toujours efficaces)
    "a classic pink inflatable flamingo float — full body, gold details, photogenic top-pose",
    "a giant inflatable pineapple float — bright yellow body with realistic green leaves on top",
    "a colorful donut pool float — pink frosting with rainbow sprinkles, glossy finish",
    "a white inflatable swan float — elegant, large wings, gold beak accents",
    "a watermelon slice inflatable float — pink flesh with dark seeds and green rind",
    "a translucent pastel-colored inflatable ring — clean minimalist aesthetic, soft mint or peach tone",
    # Instagrammable / influenceur-friendly (Martin 12/05/2026)
    "a magical inflatable unicorn float — pastel rainbow mane, gold horn, soft white body",
    "a giant inflatable rainbow arch float — multicolor stripes, photogenic from above",
    "an avocado pool float — green outer ring with a centered brown stone (you can sit IN it)",
    "an inflatable ice cream cone float — pastel scoop on a waffle cone pattern, cherry on top",
    "a golden swan float — same as classic swan but in metallic gold finish (luxe instagram aesthetic)",
    "an inflatable peacock float — turquoise and emerald body with realistic tail feather pattern",
    "an inflatable shell float — iridescent pearl-pink scallop, mermaidcore aesthetic",
    "an inflatable lemon slice float — bright yellow with white pulp pattern, summer-fresh look",
]

POOL_FLOAT_BASE_PROBABILITY = 0.35


def pick_pool_float_hint(
    category: str | None,
    vibe: str | None,
    photo_filename: str | None,
) -> str | None:
    """Retourne la description du float à autoriser, ou None pour skip.

    Déterministe par filename → un même run replay donne le même résultat.
    Pas systématique : la randomisation déterministe est CRITIQUE pour que ça
    reste "occasionnel et naturel" comme demandé par Martin.
    """
    if not category:
        return None
    cat_lower = category.lower()
    # Seuls les scènes piscine sont éligibles (rooftop ok ssi le mot pool est dedans)
    if "piscine" not in cat_lower and "pool" not in cat_lower:
        return None
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


def build_persona_prompt(persona: str, category: str, vibe: str | None = None,
                         safe_zones: list[str] | None = None,
                         unsafe_zones: list[str] | None = None,
                         max_humans: int | None = None,
                         capacity: int | None = None,
                         pool_float_hint: str | None = None) -> str:
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
    # ━━ Scenario déterministe (Martin 12/05/2026) ━━
    # On choisit UN seul scenario en Python en fonction du persona + de la 1ère safe_zone Gemini.
    # Le bloc texte renvoyé est ULTRA-précis (1 pose, 1 position, 1 outfit) — pas d'options.
    scenario = pick_human_scenario(persona, category, safe_zones, capacity)
    if scenario:
        scenario_block = scenario["prompt_block"]
        scenario_id_for_log = scenario["scenario_id"]
    else:
        # Aucun scenario valide → on instruira l'IA de ne pas ajouter
        scenario_block = None
        scenario_id_for_log = "no_scenario"

    # Legacy : utilisé uniquement pour les valeurs par défaut si le scenario block est absent.
    persona_desc = PERSONA_TEMPLATES.get(persona, PERSONA_TEMPLATES["couples"])
    action_hint = CATEGORY_ACTION_HINT.get(category, "naturally placed in the scene, candid relaxed moment")

    # ━ Arbre humains × capacity : adapte le nombre cible selon la capacity de la scène ━
    if capacity is not None and capacity > 0:
        target_n = compute_target_humans(persona, capacity)
    elif max_humans is not None:
        target_n = max_humans
    else:
        # Fallback : 1 ou 2 selon persona
        target_n = 2 if persona in ("couples", "small_groups", "families", "groups") else 1
    target_n = max(1, min(target_n, 5))  # cap dur 1-5

    vibe_mood = {
        "Family-Friendly": "warm family vacation energy, playful but tasteful",
        "Party":           "festive daytime vibe, friends having fun, never crowded",
        "Serene":          "quiet contemplative moment, peaceful luxury",
        "Luxe":            "effortless luxury, refined casual elegance",
        "Trendy":          "urban-leisure vibe, lifestyle editorial mood",
    }.get(vibe or "", "warm relaxed daytime moment, premium-accessible feel")

    # ━━ SAFE ZONES = SOURCE DE VÉRITÉ ABSOLUE pour le placement ━━
    # Si Gemini a identifié des zones précises, l'IA DOIT les utiliser strictement.
    # Sinon (zones vides) → on instruit l'IA de NE PAS ajouter d'humain.
    has_safe_zones = bool(safe_zones)
    safe_zones_block = ""
    if has_safe_zones:
        safe_list = "\n".join(f"  ZONE {i+1}: {z}" for i, z in enumerate(safe_zones))
        unsafe_list = "\n".join(f"  - {z}" for z in (unsafe_zones or []))
        max_h = max_humans or 1
        safe_zones_block = f"""

🎯 ABSOLUTE PLACEMENT RULE — THE SCENE-SPECIFIC SAFE ZONES (analyzed by Gemini Vision on THIS exact photo):

The subject(s) MUST be placed in ONE of these specific zones (and ONLY these zones). These are the ONLY locations identified as physically/visually possible WITHOUT inventing decor:
{safe_list or "  - (no specific safe zones identified — apply generic physical rules)"}

{safe_list}

📌 STRICT RULES on these zones :
- Pick exactly ONE zone (zone 1 has highest priority, then zone 2, etc.)
- Place the subject EXACTLY where described — same location, same pose. Do NOT slide them somewhere "more aspirational" if it's not in the zones list.
- Do NOT create a new edge, new step, new ledge, new platform, new lounger to make a different placement work.
- If you cannot place the subject naturally in ANY of these zones → DO NOT ADD anyone. Return the image unchanged.

FORBIDDEN placements in this scene (do NOT place subjects here under any circumstance):
{unsafe_list or "  - (none specific)"}

Maximum subjects to add for this scene: {max_h} (less is better).
"""
    else:
        # Aucune safe_zone identifiée par Gemini → on instruit l'IA de NE PAS ajouter
        safe_zones_block = """

🛑 NO SAFE ZONES IDENTIFIED for this photo — Gemini Vision concluded that there is no natural place to add a human subject without modifying the decor.

ABSOLUTE INSTRUCTION : DO NOT ADD any human subject to this image. Return the image unchanged.
"""

    # ━━ Pool float (optionnel, déclenché ~35% sur piscine ; voir pick_pool_float_hint) ━━
    # Si pool_float_hint est set → on autorise UN float décrit, on l'enlève de la
    # forbidden list, et on précise les règles d'intégration. Sinon : règle stricte
    # actuelle (aucun float, pas de pool noodle).
    if pool_float_hint:
        subject_only_intro = f"""You are ONLY allowed to add human subject(s), the items they personally hold or wear (swimwear, dress, sunglasses, hat, drink in hand, sarong, towel held by them), AND optionally a single pool float that the subject is using (see "POOL FLOAT" block below).

You MUST NEVER add ANY of the following — NO EXCEPTIONS:
- A lounger, daybed, sofa, sun lounger, beach chair, bench, table, ottoman, bed
- A pool ladder, pool steps, pool rail, handrail, ladder of any kind (if there is no ladder visible in the input, DO NOT add one)
- More than ONE pool float — exactly one or zero
- A pool noodle, separate floating drink tray, foam mat, raft other than the requested float
- A pillow, towel placed on the ground/lounger, blanket, rug
- A plant, vase, decoration, lamp, candle, sign, board
- Any new equipment, drinkware (a drink in their HAND is OK; a tray, additional glasses on a fictional table are NOT OK)
- Any modification to existing pool water shape, decking size, walls, doors, windows, pillars, plants, fences, railings"""
        pool_float_block = f"""

🍩 POOL FLOAT (MANDATORY — must appear in the final image) :
You MUST add ONE pool float in the water — specifically : {pool_float_hint}.

This pool float is a CRITICAL element of the final composition — its absence breaks the brand intent. The float MUST be visible and identifiable in the output image. Do NOT skip it.

Strict rules for the float:
- Place it IN the water of the existing pool, in a zone that is ALREADY empty water (not over the existing decking, not blocking existing furniture).
- The float must be ENGAGED with a subject : either the subject is lounging on/in it, holding it, sitting next to it, OR pushing it gently. A solo decorative float floating empty is acceptable ONLY if the pool would otherwise look completely empty and lifeless.
- Realistic scale : the float must be in proportion with the pool size. NEVER make it bigger than the pool or covering more than ~25% of the visible water surface.
- Realistic interaction with water : water displacement around the float, subtle wake if motion implied, partial reflection on water surface.
- Color/style must remain photorealistic — no over-saturated CGI candy palette. Slight wear/use is fine.
- The float counts AS the subject's support : if the subject is ON the float, the water-depth rules above are relaxed (they can be at the surface, lying on the float). But the float must look stable, not tipping.
- The float CANNOT replace any existing furniture or decor.

ONLY EXCEPTION where the float may be omitted : the visible water surface is < 2m × 2m (= float would be impossible to place at realistic scale). In that ONE case, return the image without the float. In ALL other cases, the float MUST be present in the output.

If you cannot place this float naturally according to ALL the rules above → DO NOT add it. The photo without a float is always acceptable.
"""
    else:
        subject_only_intro = """You are ONLY allowed to add human subject(s) — and only the items they personally hold or wear (swimwear, dress, sunglasses, hat, drink in hand, sarong, towel held by them).

You MUST NEVER add ANY of the following — NO EXCEPTIONS:
- A lounger, daybed, sofa, sun lounger, beach chair, raft, float, pool noodle, bench, table, ottoman, bed
- A pool ladder, pool steps, pool rail, handrail, ladder of any kind (if there is no ladder visible in the input, DO NOT add one)
- A pillow, towel placed on the ground/lounger, blanket, rug
- A plant, vase, decoration, lamp, candle, sign, board
- Any new equipment, drinkware (a drink in their HAND is OK; a tray, additional glasses on a fictional table are NOT OK)
- Any modification to existing pool water shape, decking size, walls, doors, windows, pillars, plants, fences, railings"""
        pool_float_block = ""

    return f"""🛑 RULE #1 — SUBJECT-ONLY ADDITION (THE MOST IMPORTANT RULE OF ALL):

{subject_only_intro}

You MUST NEVER reduce / resize / move / shrink ANY existing element of the scene to "make room" for the subject.
For example: shrinking the pool to add a lounger, or moving real loungers to add a fictional one — STRICTLY FORBIDDEN.

If the scene does not have a natural place for a human subject (no empty existing seat clearly visible AND no water to enter AND no solid ground to stand on), then DO NOT ADD anyone. Return the image unchanged. A scene without a subject is INFINITELY better than a scene with invented furniture.

ABSOLUTE FRAMING LOCK (RULE #2):
- DO NOT zoom in or out. DO NOT crop. DO NOT change the camera angle, height, or focal length.
- Preserve the EXACT same field of view and image dimensions as the input.
- If you cannot honor this constraint, return the image unchanged.
- The output MUST look like the SAME photograph, just with a human subject added.

🚨 STRUCTURAL PRESERVATION (CRITICAL — VIOLATION CAUSES IMMEDIATE REJECTION):

The ONLY thing you are allowed to add to this image is the requested human subject(s).
EVERYTHING ELSE in the original must remain PIXEL-IDENTICAL.

You MAY NOT, under any circumstance:
- Remove, hide, or modify ANY object visible in the original — including: TVs and screens (even if turned off / black), signs, panels, posters, balustrades, railings, drainage grilles, manholes, electrical boxes, AC units, fire-escape staircases, surveillance cameras, antennas, cables, columns, walls, floors.
- Change the architecture or any building visible in the background (windows, balconies, fire escapes, neighbouring buildings, skyline, vegetation).
- Replace existing furniture, decor, or scenic elements with "prettier" ones (a black TV stays a black TV — not artwork ; an industrial railing stays as is).
- Alter the scene composition, perspective, lighting direction, or color grading.
- Add ANY plant, vase, prop, decoration, drink, or accessory that is not requested for the subject(s).
- "Improve" or "clean up" perceived eyesores. THAT IS NOT YOUR JOB. The cleanup is handled by a separate dedicated step.

Concretely : if the original has an ugly screen on a wall and an industrial drainage grille on the floor, both MUST appear UNCHANGED in your output. The only difference between input and output should be a human-shaped region where the subject is placed (and the immediate shadow/reflection of that subject).

If you cannot follow the SCENARIO described below without modifying the surrounding scene, return the image UNCHANGED. Do not improvise an alternative pose.

🎬 THE ONLY SCENARIO YOU MUST EXECUTE (no alternatives, no creative variations) :
{scenario_block if scenario_block else "(no human scenario was selected for this photo — DO NOT add anyone; return the image unchanged.)"}

{safe_zones_block}
{pool_float_block}

🎨 GLOBAL MOOD & STYLE:
{vibe_mood}. Mid-action, candid moment, slight asymmetry — feels like a real captured moment, not staged. Premium-accessible editorial travel-magazine feel.

🔢 QUANTITY HARD LOCK — EXACTLY {target_n} HUMANS, NO MORE NO LESS:
- 🎯 TARGET = **{target_n}** subjects (computed from persona × scene capacity).
- This is a HARD MAX. You MUST count the humans you place and STOP at {target_n}. NEVER add a {target_n}+1th person under any pretext. {target_n} = {target_n}, period.
- If for "compositional balance" you feel like adding one more, DON'T. The instruction is {target_n} exactly.
- NEVER exceed the visible EMPTY capacity of the scene either. If there's only 1 empty lounger + 1 water zone → max 2 people total, even if target_n says more.
- TRADE-OFF RULE: if you cannot place {target_n} subjects on EXISTING furniture/water WITHOUT inventing → PLACE FEWER (target_n − 1, target_n − 2, or even just 1). Better fewer than fabricated.
- ALL added subjects share the SAME camera-relative scale (perspective). One person at 10m is half the size of one at 5m.
- Place subjects in ONE coherent group (or 2 max if persona = families/groups). Do not scatter in 3+ disconnected zones.

🔢 COUNTING DOUBLE-CHECK (before finalizing):
Before submitting your output, count the visible humans you've added. If count > {target_n}, REMOVE the extra people. The output must have EXACTLY {target_n} ADDED humans (in addition to any humans that were already in the original photo, which you must preserve).

PHYSICAL SAFETY & PLAUSIBILITY (CRITICAL — non-negotiable):

🔥 PRIORITY RULE FOR POOL/WATER SCENES — the most common failure mode:
If the photo features a swimming pool and there is NO clearly visible empty lounger/daybed in the foreground, place the subject IN the water:
  - Swimming gently breaststroke (head above water, calm wake)
  - Emerging from the pool at the edge (water dripping, hair wet, elbows leaning on rim)
  - OR sitting at the pool edge with legs/calves submerged in water
This is FAR BETTER than inventing a lounger/raft/daybed. Body must be partially submerged, hair wet if in water, water displacement visible, splashes acceptable.

🌊🚨 WATER DEPTH PHYSICS — THE #1 FAILURE MODE ON POOL PHOTOS — READ THIS TWICE :

For ANY subject standing in pool water, the water level MUST hide AT LEAST the belly button — preferably reaching the chest/sternum (CHEST-DEEP is the DEFAULT and CORRECT level). A pool with shallow water visible at knee or thigh level on standing adults looks LIKE THE POOL HAS NO BOTTOM — it ruins the photo immediately and makes the hotel look fake.

VISUAL TEST you MUST apply before finalizing : look at the subject's body in the water :
  ✅ ACCEPTABLE : water at chest / sternum / shoulders / armpits (only upper torso + head visible)
  ✅ ACCEPTABLE : water at upper waist (just above belly button) — if subject is clearly mid-stride walking into deeper water
  ✅ ACCEPTABLE : subject SITTING on the pool edge with only feet/calves in water (NOT standing)
  ✅ ACCEPTABLE : subject swimming horizontal, head + upper-back above water
  ❌ FORBIDDEN : water below the belly button on a standing subject (hips visible, swimsuit waistband visible, shorts waistband visible)
  ❌ FORBIDDEN : water at the thighs / mid-thigh on a standing subject — this makes the pool look depthless
  ❌ FORBIDDEN : water at the knees on a standing subject — IMMEDIATE photo failure
  ❌ FORBIDDEN : subject standing on what looks like the pool floor when there's no visible Baja shelf / step in the input

RULES :
  1. Default position : CHEST-DEEP for standing adults. If unsure, go DEEPER not shallower.
  2. If you cannot achieve chest-deep (e.g. you put the subject too close to the camera) → put them at the pool EDGE sitting on the dry deck, calves dangling in water.
  3. Multiple subjects in the same pool → ALL the same water level. Geometric impossibility otherwise.
  4. NEVER show a step/shelf that is not in the original input. If the input pool has no visible Baja shelf, the floor is at swimming depth (1.2m+) everywhere.
  5. If you fail rules 1-4, the photo will be REJECTED and we'll fall back to the original. So if you can't honor them, DON'T add the subject.

⚠️ FINAL CHECK BEFORE OUTPUT : for each standing subject in water, does the water hide the belly button? If NO → re-pose them deeper or put them at the edge. If you're still not sure → don't add them.

- Subjects MUST be placed on plausible, safe supports: seated on chairs / loungers / sofas / daybeds **THAT ALREADY EXIST IN THE PHOTO**, OR standing on solid floor/ground/decking, OR realistically immersed IN water (swimming, floating, wading waist-deep, sitting at pool edge).
- **DO NOT INVENT OR ADD any furniture, daybed, lounger, raft, platform, float, or any object that is not visibly present in the original input image.** If there is no plausible existing seat for a subject AND the scene has water → place them IN the water (priority rule above). Otherwise, place them standing on solid ground, OR DO NOT add the subject at all.
- NEVER ON the water surface as if standing on it. NEVER walking on water. NEVER floating dry without realistic immersion. NEVER on a fabricated raft/float that isn't in the original.
- If a subject is IN the pool, ensure realistic immersion: body partially submerged (waist-deep, or fully reclining for floating pose), water displacement around them, wet hair/skin if relevant, splashes acceptable. The water surface MUST react to their presence.
- NEVER standing or sitting ON TOP of furniture meant for lying (no standing on daybeds, sun loungers, or sofas).
- NEVER on the wrong side of any safety barrier, railing, glass panel, or balustrade. Subjects must always be on the safe interior side of any rooftop/balcony/pool railing.
- NEVER in physically dangerous, awkward, or improbable positions (no climbing, no leaning over edges, no unsupported balancing).
- Respect human-scale physics: feet touch ground or seat, hands rest on plausible surfaces, weight is correctly supported.
- If the scene has a railing/barrier (rooftop, balcony, pool edge, terrace), keep ALL subjects on the SAME safe side as the existing furniture.

FACE QUALITY (CRITICAL — most common Nano Banana failure mode):
- If the subject(s) occupy LESS than 25% of the frame height (= small/medium-distance figure), prefer 3/4 angle or PROFILE pose. Frontal small faces tend to come out distorted/blurred ("AI-old-school" look).
- For ALL subjects regardless of size : faces must be PHOTOREALISTIC with clearly drawn eyes, nose, mouth, and natural skin texture — NOT smudged, NOT eyeless, NOT mannequin-like, NOT plastic.
- For subjects at medium distance, eyes can be lightly closed (sunbathing, wearing sunglasses, looking down) to avoid eye-rendering issues.
- Sunglasses are GOOD on small/medium-distance subjects (hides eye detail issues).
- If you cannot render a clean photorealistic face at the required scale, use a hat brim casting shadow on the face, OR a side-profile with hair partially covering, OR sunglasses — anything that masks the precise face details while keeping the figure recognizable as human.

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
- removing, hiding, or modifying ANY existing element of the scene : TVs, screens (even off/black), signs, panels, posters, drainage grilles, manholes, AC units, fire escapes, surveillance cameras, antennas, balustrades, industrial railings, electrical boxes, cables. Black screens stay black. Ugly stuff stays ugly.
- altering, replacing, or "beautifying" any architecture, window, balcony, fire escape, neighbouring building, or skyline visible in background
- 🚨 SHRINKING / RESIZING / MOVING any existing element (pool, deck, plants, furniture, walls) to "make space" for the subject — the existing scene must remain pixel-identical in size and position
- adding any new plant, vase, prop, decor, lamp, food/drink, or accessory not requested for the subject(s)
- inventing, adding, or hallucinating new furniture — ESPECIALLY a new lounger, daybed, beach chair, sofa, raft, towel-on-the-ground, ottoman, table, pool ladder, pool steps, handrail, ladder of any kind — that is not 100% clearly visible in the input{(" (NOTE: ONE pool float is conditionally allowed per the POOL FLOAT block above — but ONLY that one and ONLY following its rules)" if pool_float_hint else " ; floats / pool noodles also forbidden")}
- subject wearing street clothes / long dress / robe / business attire on a pool scene — the subject MUST be in proper SWIMWEAR (bikini / one-piece swimsuit / monokini) on pool scenes
- subjects standing on top of water as if walking on it, or floating dry without a flotation device
- subjects standing in pool with knees / thighs / hips / belly button / swimsuit waistband / shorts waistband visible ABOVE the water (impossible without a step/shelf — water must reach CHEST level for standing adults)
- inconsistent water levels between multiple subjects in the same pool
- standing on daybeds / sun loungers / sofas / tables / any furniture meant for sitting or lying
- subjects on the wrong side of railings, barriers, glass panels, balustrades
- leaning over rooftop edges, climbing structures, unsupported balancing
- impossible / dangerous / acrobatic poses, levitation, floating bodies
- inconsistent scale between subjects (one person twice the size of another at the same distance)
- more than 3 people total, scattered groups in 3+ disconnected zones
- doubled limbs, distorted anatomy, extra fingers, mismatched shadows
- 🚨 BLURRY / SMUDGED / DISTORTED faces, faceless figures, melted faces, mannequin-like skin, eyeless figures, missing nose/mouth, plastic CGI face
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
    persona_override: str | None = None,
    photo_filename: str | None = None,
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
        pool_float = pick_pool_float_hint(cat, vibe, photo_filename)
        fallback_tag = " [fallback safe_zones]" if used_fallback else ""
        return {
            "action": "ai_add_character",
            "prompt": build_persona_prompt(
                persona, cat, vibe,
                safe_zones=safe_zones, unsafe_zones=unsafe_zones,
                max_humans=max_h, capacity=capacity_total,
                pool_float_hint=pool_float,
            ),
            "reason": f"ajout personnage IA ({persona}, target={compute_target_humans(persona, capacity_total or 2)}) sur {cat or 'scène vide'}{fallback_tag}" + (f" + bouée 🍩 {pool_float[:30]}…" if pool_float else ""),
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
    main_step = _pick_main_action(analysis, category, personas_allowed, vibe, add_character, persona_override, photo_filename)

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
        steps.append({
            "action": "ai_add_character",
            "prompt": build_persona_prompt(persona, cat, vibe, safe_zones=safe_zones,
                                           unsafe_zones=unsafe_zones, max_humans=max_h),
            "reason": f"ajout personnage IA ({persona}) sur scène ensoleillée (étape 2/2)",
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
        float_hint = pick_pool_float_hint(primary_cat, vibe, photo_filename)
        if float_hint:
            # Insertion juste avant un éventuel local_warm_boost final (pour que le warm_boost
            # apparaisse comme une dernière étape de finition). Si pas de warm_boost, on
            # ajoute en fin.
            float_step = {
                "action": "ai_add_pool_float",
                "prompt": build_pool_float_only_prompt(float_hint),
                "reason": f"ajout bouée 🍩 ({float_hint[:40]}…) — playful touch sur piscine",
                "pool_float_used": float_hint,
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
      2. Champ explicite Gemini ai_add_character_candidate.is_candidate
      3. Heuristique : catégorie ∈ AI_OK + pas de présence humaine narrative
    """
    if not analysis:
        return False

    factual = analysis.get("factual") or {}
    cat = (factual.get("category") or "").lower()

    # ━ Guard #1 : catégories métier exclues — court-circuit indépendant de Gemini ━
    if cat in _BUSINESS_RULE_EXCLUDED_CATS:
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
    "architecture_changed": (
        "DO NOT ALTER THE ARCHITECTURE: walls, structures, decor, plants, water shape, sky, and overall composition must remain identical to the input. Only requested transformations apply."
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
                        input_path, output_path,
                        action_context=primary_ai_action,
                        actions_chain=ai_actions_chain,
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
