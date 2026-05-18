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
5. **LISIBILITÉ DU SUJET — CRITÈRE PRIORITAIRE** (Martin 15/05/2026, bug Gates Hotel SB : sujet placé sur cabana lointaine → 5% de la frame, illisible) :
   Le sujet final doit être VISIBLE et LISIBLE dans la photo. Préfère TOUJOURS un anchor au PREMIER PLAN ou à MI-DISTANCE de la caméra plutôt qu'un anchor "plus premium" mais LOINTAIN.
   - Un transat blanc au premier plan où le sujet occupera 15-20% de la frame > un daybed arche premium au fond où le sujet occupera 3-5% de la frame.
   - Mesure pour chaque candidat anchor : son ÉCHELLE relative dans la frame (estimation en % de la hauteur d'image qu'occupera le sujet placé).
   - RÈGLE : si la meilleure échelle accessible est < 8% de la hauteur de la frame (= sujet minuscule) → AUCUN anchor sec n'est lisible → bascule sur l'option "joker bouée+humain" ci-dessous, OU mets feasibility="skip" si ce joker n'est pas applicable.

🛑 CRITÈRES DURCIS DE SKIP (Martin 15/05/2026 v2 — bug Gates Hotel SB : insistance forcée a généré transats dans l'eau) :
Une photo SANS sujet ajouté vaut TOUJOURS mieux qu'une photo avec sujet placé n'importe comment (mobilier inventé, piscine rétrécie, sujet illisible). Si AU MOINS UNE des conditions suivantes est vraie → `feasibility="skip"` immédiatement :

a. Aucun anchor sec ne permet ≥ 8% de hauteur frame ET joker bouée non applicable (vibe incompatible ou pas de piscine assez grande).
b. La photo est ESSENTIELLEMENT remplie par l'eau (>50% de la frame est de l'eau visible) ET les anchors secs sont tous PETITS / au FOND. Dans ce cas, forcer un placement sec mène quasi-systématiquement à invention de deck dans l'eau par Nano Banana. → SKIP.
c. Le primary_anchor candidat est à moins de 0.5m visuel du bord d'eau (= zone "à risque" où Nano Banana confond deck et eau). Sauf si tu peux vraiment décrire ABSOLUMENT (mètres précis, repère visuel sec sans ambiguïté).
d. La photo est une VUE AÉRIENNE / DRONE de la piscine entière (= sujet humain forcément microscopique).

Quand tu skips, mets un `skip_reason` court et précis qui aidera le debug : "no_readable_anchor", "water_dominates_frame_invention_risk", "anchor_too_close_to_water", "aerial_view_no_human_scale".

🍩 JOKER "BOUÉE GONFLABLE + HUMAIN DESSUS" (Martin 15/05/2026) :
Pour les photos PISCINE où AUCUN anchor sec n'est lisible (= tous les transats/daybeds sont trop loin ou orientés mal), tu peux proposer un combo "bouée gonflable décorative + sujet allongé dessus" plutôt qu'un anchor lointain peu lisible.

Conditions strictes pour proposer ce joker (TOUTES doivent être remplies) :
- Catégorie = piscine (pas spa, pas jacuzzi, pas mer)
- La surface d'eau est suffisamment grande (> ~3m × 3m visibles) pour qu'une bouée donut/flamingo y soit réaliste
- Vibe COMPATIBLE : "Family-Friendly", "Trendy", "Party" ✅ — vibe INCOMPATIBLE : "Luxe", "Serene" ❌ (une bouée flamingo détonne sur scène ultra-luxe minimaliste)
- Persona COMPATIBLE : solos, couples, families, small_groups ✅ — Persona INCOMPATIBLE : (aucune par défaut, mais évite si la photo entière respire le business/Premium)
- Le sujet sur la bouée occupera AU MOINS 10% de la hauteur de la frame (sinon même la bouée est trop loin → skip)

Si toutes les conditions sont remplies, le scenario_block doit décrire :
- Type de bouée précis : "an inflatable donut float (peach/coral color, no neon)" / "an inflatable flamingo float (soft pink, classic)" / "an inflatable swan float (white, pastel)" — JAMAIS de bouée néon flashy.
- Échelle réaliste : la bouée doit faire ~1.5-2m de diamètre apparent, occuper < 25% de la surface d'eau visible.
- Pose : sujet ALLONGÉ sur la bouée, partiellement dans l'eau (bras dans l'eau / un pied dépassant), pas debout. Si couple → 1 seul sujet sur la bouée (les bouées sont mono-place).
- Position : zone d'eau libre, PAS au-dessus du mobilier existant, PAS sur le bord (= en plein milieu de la zone de nage).
- Format scenario_block (préfixé) : "Place exactly 1 subject reclining on an inflatable [type] float in the middle portion of the pool — [persona description]. [Pose details]. [Outfit]. [Lighting]. NEVER look at the camera. The float occupies less than 25% of the visible water surface."

⚠️ Si tu déclenches le joker bouée, mets `subject_scale_hint: "medium"` et `max_subjects_realistic: 1` (la bouée porte 1 personne, jamais 2).

⚠️ INTERDICTIONS ABSOLUES — tu ne dois JAMAIS recommander de :
- Placer un sujet dans / sur l'eau d'une piscine sur un transat / matelas flottant INVENTÉ (= meuble rigide qui flotte = impossible physiquement)
- Placer un sujet de l'autre côté d'une barrière de sécurité / garde-corps / balustrade
- Demander une posture incohérente avec l'équipement (yoga sur cardio, course sur banc, etc.)
- Inviter à ajouter MOBILIER, plante, déco, ombre, lampe — UNIQUEMENT l'humain et ses items portés (swimwear, chapeau, sunglasses, drink en main)

🚫 INTERDICTIONS DE FRAMING (Martin 13/05/2026 — bug Moxy rooftop trio recadré + plantes disparues) :
- Le scenario_block ne doit JAMAIS contenir les expressions suivantes qui poussent Nano Banana à recadrer/zoomer : "in the foreground" SEUL (sans description du fond), "close-up of", "centered on", "focused on this element", "framed by", "zoomed-in", "tight shot".
- Préfère décrire l'anchor en mentionnant ses VOISINS pour signaler à Nano Banana que le cadrage complet doit rester intact. Ex : au lieu de "sur le canapé d'angle au premier plan gauche" écris "sur le canapé d'angle situé à gauche, EN PRÉSERVANT la vue panoramique au fond et les plantes en bordure".
- Ajoute systématiquement dans le scenario_block une clause finale : "FRAMING: preserve the full original frame — keep ALL background elements visible (plants, planters, decor, distant view, skyline) — do NOT crop or zoom toward the subjects."

🚫 INTERDICTIONS DE PLACEMENT ATTIRANT LA CAMÉRA (Martin 15/05/2026 — bug Gates Hotel South Beach : piscine rétrécie suite à un placement "front-most") :
Certains mots-clés de placement poussent Nano Banana à RECADRER la scène pour mettre le sujet en avant, ce qui MODIFIE le décor (piscine rétrécie/déformée, perspective compressée, mobilier déplacé). Ce sont des phrases qui désignent un placement RELATIF À LA CAMÉRA plutôt qu'un placement ABSOLU dans la scène.

INTERDIT — ne JAMAIS utiliser dans le scenario_block ni dans primary_anchor :
- "le plus proche du premier plan", "front-most", "closest to camera"
- "au premier plan" SEUL (sans précision géographique)
- "centered on", "in the center of the frame", "directly facing camera"
- "the largest [object] in the scene", "the most prominent"
- "leading toward the camera", "stepping out of the frame"

🚫 INTERDICTIONS RENFORCÉES SUR LES DESCRIPTEURS SPATIAUX AMBIGUS (Martin 15/05/2026 v2 — bug Gates Hotel SB : "à mi-distance, à gauche de l'escalier" → Nano Banana a interprété comme "dans la piscine") :

Les mots suivants sont AMBIGUS et peuvent pousser Nano Banana à placer le sujet dans la piscine ou à inventer un step/deck :
❌ "à mi-distance" / "at mid-distance" — Mi-distance par rapport à quoi ? Souvent interprété comme "au milieu visuel de la frame" = au-dessus de l'eau.
❌ "à gauche de l'escalier" / "next to the pool steps" — Si l'escalier est immergé dans l'eau, "à gauche de l'escalier" est interprété comme "dans l'eau près des marches".
❌ "à côté du bord" / "near the pool edge" — Ambigu : bord = côté deck OU côté eau ?
❌ "vers le centre" / "toward the middle" — Vague, peut conduire dans la piscine.

OBLIGATOIRE — utilise des RÉFÉRENCES ABSOLUES qui ne laissent AUCUN doute sur le type de surface :
✅ Nomme un VOISIN identifiable HORS DE L'EAU : "le 3e transat blanc à partir du fond de la rangée gauche, sur le sol carrelé sec"
✅ Précise toujours la NATURE DE LA SURFACE sous le sujet : "sur le deck en béton sec", "sur le carrelage blanc du deck (pas dans l'eau)", "sur le sol en bois du rooftop (pas sur le toit lui-même)"
✅ Utilise des distances en MÈTRES depuis un point sec identifiable : "à 2m du mur côté piscine, sur le sol sec"
✅ Si l'anchor est PROCHE DE L'EAU, dis-le explicitement : "sur le deck SEC, à environ 1m du bord d'eau, les pieds touchent le carrelage et non l'eau"

🚨 CLAUSE OBLIGATOIRE "ON DRY DECK" — quand le sujet doit être HORS de l'eau :
Si ton primary_anchor désigne un endroit SEC (transat / daybed / chaise / cabana / sol / chemin / banc), tu DOIS inclure dans le scenario_block la phrase littérale suivante (ou équivalent strict) :
  "This placement is on the DRY pool deck — the subjects' feet/body touch dry concrete/tile/wood ONLY, NEVER water. The pool water is NEXT TO them but they are NOT in or on it. The pool water surface stays 100% unchanged in shape and extent."

Cas où cette clause n'est PAS requise (le sujet EST dans l'eau intentionnellement) :
- Sujet sur les marches d'accès piscine, pieds dans l'eau
- Sujet nageant
- Sujet sur bouée gonflable (joker)
- Sujet assis sur le rebord, jambes dans l'eau

Dans ces cas-là, la clause INVERSE est obligatoire : "This placement INTENTIONALLY involves water contact — [describe exactly which body parts touch water and how much]. The pool water surface remains 100% intact in shape — the subject is INSIDE the existing water, not on a modified deck."

RÈGLE D'OR : si après lecture du scenario_block tu N'ES PAS À 100% SÛR que Nano Banana saura distinguer "sec" vs "humide", c'est que ta description est ambiguë. Reformule pour éliminer le doute.

📝 RETOURNE STRICTEMENT CE JSON (aucun markdown, aucun texte hors JSON) :

{{
  "feasibility": "ok" | "skip",
  "skip_reason": null | "raison courte si skip",
  "max_subjects_realistic": int (1 à 5),
  "primary_anchor": "description ultra-précise de l'élément/zone EXISTANT où placer le sujet (ex: 'le transat blanc vide au premier plan à gauche, sous le parasol bleu')",
  "estimated_subject_scale_pct": int (estimation en % de hauteur de frame que le sujet occupera une fois placé sur ce primary_anchor — sert au tri downstream),
  "uses_pool_float_joker": false | true (true SEULEMENT si tu déclenches le joker bouée gonflable),
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
            # Coerce estimated_subject_scale_pct en int [0, 100] (sert au tri downstream)
            try:
                pct = int(data.get("estimated_subject_scale_pct", 0))
                data["estimated_subject_scale_pct"] = max(0, min(100, pct))
            except (ValueError, TypeError):
                data["estimated_subject_scale_pct"] = 0
            # Coerce uses_pool_float_joker en bool
            data["uses_pool_float_joker"] = bool(data.get("uses_pool_float_joker", False))
            # Force primary_anchor en str (clé pour le bloc mono-zone downstream)
            data["primary_anchor"] = (data.get("primary_anchor") or "").strip()
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
        "estimated_subject_scale_pct": 0,
        "uses_pool_float_joker": False,
        "alternative_anchors": [],
        "pose_description": "",
        "outfit_description": "",
        "subject_scale_hint": "medium",
        "pitfalls_specific_to_this_photo": [],
        "scenario_block": "",
        "_meta": {"duration_ms": 0, "cost_usd": 0, "input_tokens": 0, "output_tokens": 0, "error": str(last_error)[:200] if last_error else None},
    }
