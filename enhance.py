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
PROMPT_REMOVE_CLUTTER = """🛑 ADDITION-FREE RULE (#1, MOST IMPORTANT) :
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
- Construction debris, hazard barriers, tape, hose, fire-extinguisher boxes on a wall

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
- Any food/drinks SERVED on a dining table (cocktail, plate of food → keep; abandoned dirty glass on a lounger → remove)
- All people present in the scene

Reconstruct the underlying surface (sand, tile, wood, fabric, wall, sky) seamlessly where the clutter was. If a structural eyesore is too embedded to remove cleanly, leave it rather than create a glitch.

Photorealistic editorial lifestyle photography. The result must look like a professional cleanup crew passed and the technical building services had been hidden — same scene, just polished.

NEGATIVE PROMPT (HARD avoid):
- ANY new element added to the scene (cranes, scaffolding, trucks, vehicles, construction, birds, people, plants, decorations, signs, text overlays)
- "improvements" that go beyond cleaning (do NOT add a sky, do NOT add clouds, do NOT add greenery)
- removed furniture, altered architecture, missing decor, ghost outlines, blurred patches, CGI artifacts, structural deformation
- removed served food or cocktails on a dining table, removed people
- duplicated parts of the scene (a wall section pasted twice, water duplicated)
- color shifts in regions that were not edited"""


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

    return f"""🛑 RULE #1 — SUBJECT-ONLY ADDITION (THE MOST IMPORTANT RULE OF ALL):

You are ONLY allowed to add human subject(s) — and only the items they personally hold or wear (swimwear, dress, sunglasses, hat, drink in hand, sarong, towel held by them).

You MUST NEVER add ANY of the following — NO EXCEPTIONS:
- A lounger, daybed, sofa, sun lounger, beach chair, raft, float, pool noodle, bench, table, ottoman, bed
- A pool ladder, pool steps, pool rail, handrail, ladder of any kind (if there is no ladder visible in the input, DO NOT add one)
- A pillow, towel placed on the ground/lounger, blanket, rug
- A plant, vase, decoration, lamp, candle, sign, board
- Any new equipment, drinkware (a drink in their HAND is OK; a tray, additional glasses on a fictional table are NOT OK)
- Any modification to existing pool water shape, decking size, walls, doors, windows, pillars, plants, fences, railings

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

If you cannot add the subject without modifying the surrounding scene, choose option (3) of the placement priority (place them IN water if water is present, OR standing on existing ground), OR return the image unchanged.

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

🔥 PRIORITY RULE FOR POOL/WATER SCENES — the most common failure mode:
If the photo features a swimming pool and there is NO clearly visible empty lounger/daybed in the foreground, place the subject IN the water:
  - Swimming gently breaststroke (head above water, calm wake)
  - Emerging from the pool at the edge (water dripping, hair wet, elbows leaning on rim)
  - OR sitting at the pool edge with legs/calves submerged in water
This is FAR BETTER than inventing a lounger/raft/daybed. Body must be partially submerged, hair wet if in water, water displacement visible, splashes acceptable.

🌊 WATER DEPTH PHYSICS (ABSOLUTE RULE — most common failure on pool photos) :

When a subject is STANDING UPRIGHT in the pool, the water level on their body must follow real physics :
  - Pool with NO visible steps/ladder/shelf → standing subjects MUST be **CHEST-DEEP** (water at sternum / upper-chest level — only upper-torso, shoulders, neck, head visible above water). This is the DEFAULT and CORRECT level for adult swimming pools.
  - 🚫 NEVER show belly button, hips, swimsuit waistband, shorts waistband, or thighs above water for standing subjects. This makes the pool look like a kiddie pool.
  - WAIST-DEEP (water at hip) is acceptable ONLY for : (a) child-sized subject, (b) subject clearly walking INTO the water (mid-step transition), (c) pool clearly very shallow as visible in original.
  - To show subjects lower in the water (knees / thighs visible), they MUST be either :
       (a) Sitting on the EDGE of the pool with feet/calves submerged (NOT standing in the water).
       (b) On a clearly visible existing pool STEP / Baja shelf / raised platform — and the step itself must be in the input image.
  - Sitting in water on a step : water level matches the actual sitting depth (typically waist or chest level on the seated subject).
  - Multiple subjects in same pool → ALL the same water level (not one chest-deep + another knee-deep without geometric reason).
  - If subject's swimsuit color shows above water at hip level on a 1.4m+ deep pool → it is WRONG. The water should hide everything below chest.

If you cannot place subjects respecting these depth rules → put them at the edge (sitting on the dry deck with calves in water), OR have them swimming horizontally (head + upper back above water), OR DO NOT add them. Wrong water levels are immediately recognizable as fake and ruin the photo.

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
- inventing, adding, or hallucinating new furniture — ESPECIALLY a new lounger, daybed, beach chair, sofa, raft, float, towel-on-the-ground, ottoman, table, pool ladder, pool steps, handrail, ladder of any kind — that is not 100% clearly visible in the input
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
        # persona_override permet à app.py d'alterner solos/couples/small_groups dans le pack
        persona = persona_override or (personas_allowed[0] if personas_allowed else "couples")
        # Récupère les safe zones décrites par Gemini sur cette photo précise
        safe_zones_block = analysis.get("safe_zones_for_humans") or {}
        safe_zones = safe_zones_block.get("safe_areas") or []
        unsafe_zones = safe_zones_block.get("unsafe_areas") or []
        max_h_raw = safe_zones_block.get("max_recommended")
        try:
            max_h = int(max_h_raw) if max_h_raw is not None else None
        except (ValueError, TypeError):
            max_h = None

        # ━ NEW : si pas de safe_zone OU max_recommended=0 → on ne tente PAS l'ajout ━
        # Gemini a conclu qu'il n'y a pas de place naturelle pour un humain. Forcer l'IA
        # à en mettre un produirait une scène modifiée (rebord inventé, etc.). Skip propre.
        if not safe_zones or (max_h is not None and max_h == 0):
            return {
                "action": "local_warm_boost",
                "prompt": None,
                "reason": "ajout perso skip (Gemini : aucune safe_zone identifiée — l'IA inventerait du décor)",
            }
        return {
            "action": "ai_add_character",
            "prompt": build_persona_prompt(persona, cat, vibe, safe_zones=safe_zones,
                                           unsafe_zones=unsafe_zones, max_humans=max_h),
            "reason": f"ajout personnage IA ({persona}) sur {cat or 'scène vide'} — zone précise Gemini",
            "persona_used": persona,
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
        return {
            "action": "ai_remove_clutter",
            "prompt": PROMPT_REMOVE_CLUTTER,
            "reason": f"nettoyage clutter : {clutter_desc}",
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
    """Retourne un step crop si Gemini a recommandé un crop pertinent (60-95% conservé).

    🛡 Garde-fou : si la photo a un humain bien visible (full_visible), on désactive le crop.
    Trop risqué de couper la tête/corps. La photo originale est gardée telle quelle dans ces cas.
    """
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


def pick_strategy(
    analysis: dict | None,
    category: str | None = None,
    personas_allowed: list[str] | None = None,
    vibe: str | None = None,
    add_character: bool = False,
    persona_override: str | None = None,
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
    main_step = _pick_main_action(analysis, category, personas_allowed, vibe, add_character, persona_override)

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
                "prompt": PROMPT_REMOVE_CLUTTER,
                "reason": f"pré-nettoyage clutter avant ajout perso : {clutter_desc}",
            })

        # Si on a déjà cropé ET que l'action principale est juste warm_boost (rien d'urgent), on saute le warm
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
    # Expose persona si un step ai_add_character a été utilisé
    persona_step = next((s for s in steps if s.get("persona_used")), None)
    if persona_step:
        out["persona_used"] = persona_step["persona_used"]
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

    # 1ère tentative
    response = _call_with_prompt(prompt)
    image_data, text_response = _extract_image(response)

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
            # Récupère TOUTES les actions IA du chaînage pour unionner les whitelists du validateur.
            # Cas critique : chaînage ai_lighting → ai_add_character → le validateur doit accepter
            # à la fois les violations légitimes de ai_lighting (scene_regenerated, architecture_changed)
            # ET celles de ai_add_character. Sinon faux positif → retry → fallback original.
            ai_steps = [s for s in steps if s["action"].startswith("ai_")]
            ai_actions_chain = [s["action"] for s in ai_steps]
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
