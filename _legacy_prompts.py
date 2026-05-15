"""Backup prompts legacy (Martin 13/05/2026) — pour rollback rapide en cas
de régression sur la nouvelle version compacte de `build_persona_prompt`.

🔁 ROLLBACK :
    Si la version compacte (`build_persona_prompt` dans `enhance.py`) produit
    des résultats moins bons que cette V1 longue, set la variable d'env :

        export USE_LEGACY_PROMPT_V1=1

    Le code dans enhance.py teste cette variable et délègue à
    `build_persona_prompt_v1_long` (ci-dessous) si elle est à "1".

✏️  HISTORIQUE :
    V1 (cette version) : prompt cumulatif construit au fil des bugs rencontrés
        d'avril à mai 2026. ~5000 tokens. Beaucoup de doublons et contradictions
        (cf. audit 13/05/2026). Fonctionne mais signal critique dilué.

    V2 (nouvelle, dans enhance.py) : refonte 3 blocs courts ~2000 tokens.
        WHERE / WHO / RENDU. Plus claire pour Gemini Image.

⚠️  Ce fichier ne doit PAS être modifié — c'est un snapshot figé.
"""

from __future__ import annotations


def build_persona_prompt_v1_long(
    persona: str,
    category: str,
    vibe: str | None = None,
    safe_zones: list[str] | None = None,
    unsafe_zones: list[str] | None = None,
    max_humans: int | None = None,
    capacity: int | None = None,
    pool_float_hint: str | None = None,
    # Dépendances injectées depuis enhance.py au moment du call (pour éviter
    # un import circulaire ici) :
    scenario_block_override: str | None = None,
    _pick_human_scenario=None,
    _PERSONA_TEMPLATES=None,
    _CATEGORY_ACTION_HINT=None,
    _compute_target_humans=None,
    _detect_barrier_risk=None,
    _coerce_scenario_count=None,
) -> str:
    """Version legacy V1 du prompt ajout perso (snapshot 13/05/2026).

    Identique mot pour mot à la fonction `build_persona_prompt` qui existait
    dans enhance.py juste avant la refonte du 13/05/2026. Ne dépend que de
    fonctions/dicts passés en paramètres pour éviter un import circulaire.
    """
    # (Martin 13/05/2026 v4) — calcul target_n d'abord, puis bascule persona si
    # barrière détectée AVANT pick scenario.
    if capacity is not None and capacity > 0:
        target_n = _compute_target_humans(persona, capacity)
    elif max_humans is not None:
        target_n = max_humans
    else:
        target_n = 2 if persona in ("couples", "small_groups", "families", "groups") else 1
    target_n = max(1, min(target_n, 5))

    barrier_risk_for_cap = _detect_barrier_risk(unsafe_zones, category)
    if barrier_risk_for_cap and target_n > 1:
        target_n = 1

    # ━ CAP CRITIQUE PAR NOMBRE DE ZONES (Martin 13/05/2026 v4) ━━━━━━━━━━━━━━━
    # Bug Moxy piscine famille : target_n=4 mais seulement 3 safe_zones disponibles
    # (2 transats + 1 zone water) → Gemini Image a inventé un 4e support flottant
    # dans la piscine. Fix structurel : target_n ne peut JAMAIS dépasser len(safe_zones)
    # PLUS un bonus de 1 si une zone explicite mentionne "couple"/"famille" (= seat à
    # 2+ personnes : daybed, sofa, dining table).
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if safe_zones:
        n_zones = len(safe_zones)
        # Bonus seat multi-personnes : daybed, sofa, canapé, dining table peuvent
        # accueillir 2 personnes par zone (au lieu de 1 par défaut).
        multi_seat_kws = ("daybed", "day bed", "sofa", "canapé", "canape",
                          "dining table", "table à manger", "cabana", "bench", "banquette",
                          "lit de jour")
        bonus = sum(
            1 for z in safe_zones
            if any(k in (z or "").lower() for k in multi_seat_kws)
        )
        zone_capacity = n_zones + bonus
        if target_n > zone_capacity:
            target_n = max(1, zone_capacity)

    effective_persona = persona
    if barrier_risk_for_cap and persona in ("couples", "small_groups", "families", "groups"):
        effective_persona = "solos"

    # ━━ V5 (Martin 13/05/2026) : si scenario_block_override fourni (= généré par
    # scenario_writer.py via Gemini Vision sur la photo réelle), on l'utilise tel
    # quel. Plus de catalogue Python aveugle à la photo.
    if scenario_block_override:
        scenario_block = scenario_block_override
        # On applique quand même le coerce count au cas où Vision aurait mis le mauvais N
        if _coerce_scenario_count is not None:
            scenario_block = _coerce_scenario_count(scenario_block, target_n)
    else:
        scenario = _pick_human_scenario(effective_persona, category, safe_zones, capacity, target_n=target_n)
        if scenario:
            scenario_block = scenario["prompt_block"]
            if _coerce_scenario_count is not None:
                scenario_block = _coerce_scenario_count(scenario_block, target_n)
        else:
            scenario_block = None

    persona_desc = _PERSONA_TEMPLATES.get(persona, _PERSONA_TEMPLATES["couples"])
    action_hint = _CATEGORY_ACTION_HINT.get(category, "naturally placed in the scene, candid relaxed moment")

    vibe_mood = {
        "Family-Friendly": "warm family vacation energy, playful but tasteful",
        "Party":           "festive daytime vibe, friends having fun, never crowded",
        "Serene":          "quiet contemplative moment, peaceful luxury",
        "Luxe":            "effortless luxury, refined casual elegance",
        "Trendy":          "urban-leisure vibe, lifestyle editorial mood",
    }.get(vibe or "", "warm relaxed daytime moment, premium-accessible feel")

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
        safe_zones_block = """

🛑 NO SAFE ZONES IDENTIFIED for this photo — Gemini Vision concluded that there is no natural place to add a human subject without modifying the decor.

ABSOLUTE INSTRUCTION : DO NOT ADD any human subject to this image. Return the image unchanged.
"""

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

    barrier_risk = _detect_barrier_risk(unsafe_zones, category)
    barrier_lock_block = ""
    if barrier_risk:
        barrier_lock_block = """🚨🚨🚨 RULE #0 — SAFETY BARRIER ABSOLUTE LOCK (highest priority, READ TWICE):

This photo is a ROOFTOP / ELEVATED scene with a SAFETY BARRIER (glass panel, balustrade, railing, parapet) visible in the foreground. The barrier is the boundary between the SAFE INTERIOR (where existing loungers / daybeds / pool / decking live) and the VOID OUTSIDE (open air, drop, view).

📍 ABSOLUTE PLACEMENT LAW :
- ALL human subject(s) MUST be on the SAFE INTERIOR SIDE of the barrier — the SAME SIDE as the existing furniture / pool / decking.
- The barrier ITSELF, plus everything OUTSIDE / BEYOND it (sky, distant city, neighbouring rooftops, treetops), is a STRICT FORBIDDEN ZONE for any subject — NO EXCEPTIONS.
- DO NOT invent loungers, daybeds, platforms, decking, or any surface BEYOND the barrier to "make room" for the subject. There is NOTHING there — it is open air / a drop. Adding furniture there is both a physical impossibility AND a safety horror.
- DO NOT place a subject between the camera and the barrier IF the barrier is the foreground edge of the scene — the subject must visually sit BEHIND the existing furniture line, NOT in the thin band between camera and barrier (that band is usually outside the deck).

✅ VISUAL TEST you MUST apply before finalizing :
  Trace mentally the line of the barrier in the photo. Every human you've added : are they on the same side as the swimming pool / loungers / decking ? If even ONE subject is on the wrong side (= sky / void / city view behind them with no decking visible at their feet) → REMOVE that subject. Do not output the photo with a wrong-side subject — better return the image unchanged.

⛔ FORBIDDEN under any pretext :
- Placing a subject "on a lounger" that you've drawn BEYOND the barrier (the lounger itself is invented and floating in air).
- Placing a subject sitting on what looks like the BARRIER ITSELF (the parapet / balustrade is NOT a seat).
- Creating an "extra deck zone" past the barrier where a "scenic" lounger fits the city view.
- "Continuing" the deck past the barrier — the deck STOPS at the barrier in the original ; it must STOP there in the output.

If you cannot place the subject(s) clearly on the safe interior side using ONLY existing visible furniture, then return the image UNCHANGED. No subject is INFINITELY better than a subject placed in the void.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""

    return f"""{barrier_lock_block}🛑 RULE #1 — SUBJECT-ONLY ADDITION (THE MOST IMPORTANT RULE OF ALL):

{subject_only_intro}

You MUST NEVER reduce / resize / move / shrink / DELETE ANY existing element of the scene to "make room" for the subject.

🚨🚨🚨 PARTICULAR ATTENTION — INVENTED FLOATING FURNITURE (Martin 13/05/2026, bug Moxy famille) :
- NEVER, under ANY circumstance, draw a lounger / daybed / sun lounger / bench / sofa / rigid platform / pool bed / floating mat FLOATING ON THE WATER SURFACE if it doesn't already exist there in the input. A solid piece of furniture cannot physically float on a swimming pool. Yet you tend to invent one to "seat" subjects when the existing loungers are full / occupied / on the wrong side.
- Concrete failure mode : input = pool with empty loungers on the deck around it. You decide to add a family of 4. The existing loungers can only accommodate 2-3 people. Rather than reducing the count or putting some in the water, you CONJURE a brand new lounger FLOATING IN THE MIDDLE OF THE POOL and place the subjects on it. ABSOLUTE FORBIDDEN.
- If the count requested exceeds the existing visible seating capacity → REDUCE the count. Place fewer subjects. The pool deck stops where the water starts ; no exception.

🚨🚨 PARTICULAR ATTENTION — WATER / POOL DELETION IS THE #1 CATASTROPHIC FAILURE MODE :
- If the original photo has a swimming pool / jacuzzi / fountain / pond, that water surface MUST be 100% present in the output, in the EXACT same shape and position.
- DO NOT delete the pool to align 3 loungers at the center of the scene. DO NOT cover the pool with decking to "improve the composition". DO NOT shrink the pool to make space for the subjects.
- If the scenario asks for "3 adjacent loungers" but the original photo only has loungers spaced around a pool, place ONLY the number of subjects that fit on the visible existing loungers (could be 1, 2 — even if target is 3), OR place some subjects in the pool water, OR DO NOT ADD anyone.
- Removing the pool is INSTANT REJECTION. Better an unmodified original photo than a photo with a missing pool.

Similar deletions that are NEVER acceptable :
- Removing or merging existing loungers / daybeds / sofas
- Removing visible walls, pillars, ceilings, decorative panels
- Removing existing decor (plants, vases, art panels, signage)
- Changing the floor material (tile to wood, etc.)
- Shifting the camera angle to "frame the subjects better"

If the scene does not have a natural place for a human subject (no empty existing seat clearly visible AND no water to enter AND no solid ground to stand on), then DO NOT ADD anyone. Return the image unchanged. A scene without a subject is INFINITELY better than a scene with invented furniture OR a scene with a deleted pool.

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

📏 SCALE LOCK — match the subject size to existing visible furniture (CRITICAL — non-negotiable) :
The HUMAN HEIGHT in the output is fully constrained by the size of the existing furniture/architecture visible in the input. Use these references :
- A STANDING ADULT is ≈ 2× the height of an empty pool lounger / daybed (lounger ≈ 80cm tall, adult ≈ 170cm). If the lounger in the photo appears N pixels tall, the standing adult should be ≈ 2N pixels tall.
- An ADULT SITTING UPRIGHT on a lounger / chair is ≈ 1.3× the height of the seat (head sticking up).
- An ADULT LYING / RECLINING on a lounger occupies ≈ 1× the lounger length.
- An ADULT'S HEAD in the water (pool swimming) is ≈ ½ the width of a typical pool lane (≈ 1m).

🚨 ABSOLUTE SIZE LIMIT (Martin 13/05/2026 — fix bug "humains géants") :
- For a WIDE / PANORAMIC / ROOFTOP / OUTDOOR DECK shot showing the full venue, each subject's HEAD must occupy at MOST 6-8% of the frame height. Their FULL BODY must occupy at MOST 25% of the frame width.
- For a CLOSE / MEDIUM shot (e.g. a single lounger or seating zone), the head can be 12-15% frame height max.
- If you find yourself drawing subjects that occupy MORE than 30% of the frame width, you've placed them TOO CLOSE to the camera → re-place them FURTHER BACK / DEEPER in the scene, OR omit them entirely.
- Mental visual test : compare the subject's silhouette to the visible furniture next to them. A subject taller than 2× a lounger height is WRONG.

⚠️ If you cannot find a visible chair/lounger/parasol/window in the frame to anchor the scale, the photo is likely a wide shot or aerial — DO NOT add a full human, instead OMIT the addition (return the image WITHOUT a human) rather than guessing scale. A wrong-scale human (giant or tiny) is much worse than no human.

Common failure mode to avoid : in a "panoramic" frame where the pool is small (e.g. drone-style shot of the whole hotel), do NOT place a person standing next to the pool sized like a regular ground-level photo — they would appear as 2-3× the pool width, completely breaking realism.

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

🎨 PHOTOGRAPHY STYLE — California influencer travel aesthetic :
Premium lifestyle travel photography in the Reformation / Solid&Striped / Aman aspirational
aesthetic. Saturated warm tones, golden hour ambient light, slight Kodak Portra 800 film
quality (warm highlights, rich shadows, natural grain, no digital crispness). Sony A7R IV
or Fujifilm X-T5 35mm-equivalent lens, f/2.8-f/4, candid travel-magazine "real captured
moment" feel — never staged studio. Real skin texture (slightly dewy, subtle pores), no
artificial smoothing, no airbrushing, no plastic finish. Slight film grain. Composition
intentional but unposed.

⛔ NEGATIVE PROMPT — DO NOT do any of the following, organized by category :

[SCENE PRESERVATION]
- ANY zoom-in, crop, or camera angle change vs input
- Removing, hiding, or modifying ANY existing element : TVs / screens (even off/black), signs,
  panels, posters, drainage grilles, manholes, AC units, fire escapes, cameras, antennas,
  balustrades, industrial railings, electrical boxes, cables. Black screens stay black. Ugly
  stuff stays ugly.
- Altering, replacing, or "beautifying" architecture, windows, balconies, fire escapes,
  buildings, or skylines in background
- 🚨 Shrinking / resizing / moving any existing element (pool, deck, plants, furniture, walls)
  to "make space" for the subject — existing scene must remain pixel-identical
- Adding plants, vases, props, decor, lamps, candles, signs not requested for the subject(s)
- Inventing furniture not 100% visible in input — especially new loungers, daybeds, beach
  chairs, sofas, rafts, towels-on-the-ground, ottomans, tables, pool ladders, pool steps,
  handrails, ladders of any kind{(" (NOTE: ONE pool float is conditionally allowed per the POOL FLOAT block — but ONLY that one)" if pool_float_hint else " ; floats / pool noodles also forbidden")}

[ATTIRE & STYLING]
- Subject wearing street clothes / long dress / robe / business attire on a pool scene — pool
  scenes REQUIRE proper swimwear (bikini / one-piece / bandeau / monokini)
- 🚫 Lingerie / underwear / sheer tops / lace / cheeky-cut / Brazilian / micro bikinis / nipple
  visible / bare buttocks. Swimwear must be premium-modern but TASTEFUL (Reformation / Solid&Striped
  / Hunza G aesthetic — NEVER Bang / micro-influencer racy aesthetic)
- 🚫 Heavy makeup : contouring lines, dark eyeshadow, sharp eyeliner wing, TikTok-style strobing,
  glittery eyes, bold lipstick. ONLY soft natural "beach glam" : sheer bronzer, peachy blush,
  mascara, glossy nude lips
- 🚫 Visible smartphones / devices in the subject's hand (timeless brand intent — no 2023 prop)
- 🚫 Flashy / chunky jewelry, statement earrings, gemstones, watches. ONLY layered fine gold
  chains and small gold hoops max
- 🚫 Multiple hair colors clashing in a group (e.g. one pink hair + one bleach blonde) — keep
  all subjects in natural sun-kissed tones (blonde / brown / dark brown)

[POSES & PHYSICS]
- Subjects standing on top of water as if walking on it, or floating dry without flotation device
- Subjects standing in pool with knees / thighs / hips / belly button / swimsuit waistband visible
  ABOVE water (must reach CHEST level for standing adults — pool always has a real bottom)
- Inconsistent water levels between multiple subjects in the same pool
- Standing on daybeds / sun loungers / sofas / tables / any furniture meant for sitting or lying
- Subjects on the wrong side of railings, barriers, glass panels, balustrades
- Leaning over rooftop edges, climbing structures, unsupported balancing
- Impossible / dangerous / acrobatic poses, levitation, floating bodies
- 🚫 Sultry / aguicheur poses : mirror selfie with backside to camera, kneeling on bed, bending
  over with bottom prominent, lip biting, hand on hip arched-back model pose. ALLOWED : confident
  candid smile, relaxed leaning, mid-laugh, reading, holding a drink — never staged photoshoot

[ANATOMY & FACES]
- Inconsistent scale between subjects (one twice the size of another at same distance)
- More than 4 people total, scattered groups in 3+ disconnected zones
- Doubled limbs, distorted anatomy, extra fingers, missing limbs, fused hands, mismatched shadows
- 🚨 Blurry / smudged / distorted faces, faceless figures, melted faces, mannequin-like skin,
  eyeless figures, missing nose/mouth, plastic CGI face
- 🚫 Exaggerated / unrealistic body proportions : Photoshopped tiny waist, hyper-large hips,
  fake-looking implants, cartoon-like curves. Bodies must be lean-toned-natural, not silicone

[RENDERING]
- Posed models facing camera, fake smiles, stiff catalog postures
- Business attire, drunk/loud party, recognizable celebrity faces
- Cartoon style, CGI look, oversaturated candy palette, glowing edges, over-sharpened plastic skin
- Harsh HDR, blown highlights, crushed shadows
- 🚫 Studio strobe lighting flat on subject's face — light must always feel natural
  (sun / window / ambient), never strobed
"""
