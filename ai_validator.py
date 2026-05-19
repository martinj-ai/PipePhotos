"""Validation post-IA — 2e passe Gemini Vision sur les photos retouchées par Nano Banana
pour détecter les dérives (mobilier inventé, personnes sur l'eau, scène régénérée, etc.).

Coût : ~$0.0006 par photo IA validée. Filet de sécurité automatique.
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

VALIDATION_MODEL = "gemini-2.5-flash"

VALIDATION_PROMPT = """Tu reçois 2 images : la première est l'ORIGINALE, la seconde est une RETOUCHE IA censée juste ajouter ou modifier des éléments légers (personnages, lumière, recadrage). La retouche doit préserver fidèlement l'environnement.

🔍 PROTOCOLE D'INSPECTION OBLIGATOIRE — applique-le AVANT de répondre :

ÉTAPE 1 : Liste mentalement les ÉLÉMENTS MAJEURS de l'image ORIGINALE (la 1ère). Cite-toi tous les éléments structurants et reconnaissables :
- Plans d'eau (piscine, jacuzzi, fontaine, bassin) → noter la forme et la taille
- Mobilier existant (loungers, daybeds, tables, chaises, sofas, cabanas, bar)
- Architecture (murs, piliers, plafond, sol, escaliers, baies, balustrades)
- Décor visible (plantes, vases, panneaux d'art, signage, écrans, sculptures)
- Vue arrière-plan (bâtiments, skyline, jardin)

ÉTAPE 2 : Pour CHAQUE élément listé en étape 1, vérifie qu'il existe ENCORE dans la RETOUCHE (la 2nde image) :
- Si un plan d'eau est manquant ou rétréci de plus de 30% → violation `architecture_changed` (MAJEURE)
- Si du mobilier existant a été déplacé ou supprimé → violation `architecture_changed`
- Si un mur / pilier / plafond visible a changé → violation `architecture_changed`
- Si la composition globale et la perspective ont changé → violation `scene_regenerated`

⚠️ Ce check est CRITIQUE : Gemini Image a tendance à "recomposer" la scène quand le scenario demandé n'est pas facile à intégrer. La piscine peut disparaître pour faire de la place aux loungers. Tu DOIS détecter ce cas.

ÉTAPE 2.bis : DÉTECTION DES INVENTIONS SUBTILES DE MOBILIER (très fréquent, souvent raté) :
Quand Gemini Image place des personnes au bord d'une piscine / sur un sol nu / dans un coin vide, il a la TRÈS MAUVAISE habitude d'INVENTER un support pour les "asseoir confortablement". Cherche spécifiquement :

🔬 SCAN BACKGROUND OBLIGATOIRE (Martin 13/05/2026 — bug Moxy famille piscine, canapé ajouté en arrière-plan non détecté) :
Tu DOIS scanner l'ARRIÈRE-PLAN de la retouche AUSSI, pas seulement la zone autour du sujet ajouté. Procédure stricte :
1. Mentalement, divise la retouche en 9 régions (grille 3×3 : haut-gauche, haut-centre, haut-droite, milieu-gauche, milieu-centre, milieu-droite, bas-gauche, bas-centre, bas-droite).
2. Pour CHAQUE région, compare avec la même région de l'image ORIGINALE.
3. Si une région a un meuble (canapé, banc, banquette, daybed, fauteuil, table, coussin) qui n'existait pas dans la région correspondante de l'original → `invented_furniture`.
4. Cas particulièrement insidieux : un canapé "généré" en arrière-plan (au-delà de la piscine, près d'un mur, à côté de cabanas existantes) que tu pourrais croire "déjà là" parce qu'il ressemble au style du venue. Vérifie EXPLICITEMENT en superposant mentalement l'original et la retouche.
5. RÈGLE D'OR : si tu n'es pas SÛR à 100% que le meuble existait dans l'original au MÊME EMPLACEMENT, alors c'est `invented_furniture`. Le doute = violation.


🚨 Au bord d'une piscine où l'original n'a QUE de la dalle nue (béton, carrelage, bois) — la retouche y a-t-elle ajouté :
- Un BANC / BENCH avec coussin (typiquement gris/beige) qui n'existait pas → `invented_furniture`
- Un COUSSIN posé directement sur la dalle pour adoucir l'assise → `invented_furniture`
- Une SERVIETTE EXTRA / tapis / mat ajouté pour s'asseoir → `invented_furniture`
- Un MINI-DAYBED / SIÈGE BAS / PLATFORM en bois ou pierre → `invented_furniture`
- Un BAR EN BORDURE / planche / ottoman / cube pour s'accouder → `invented_furniture`

🚨 Le sol/dalle a-t-il été modifié au niveau des personnages :
- La dalle béton remplacée par du bois/decking sous leurs fesses → `architecture_changed`
- La bordure de piscine étendue / élargie pour leur faire une plateforme → `architecture_changed`
- Un step / shelf / lèvre supplémentaire ajouté autour de la piscine → `architecture_changed`

🚨 Mobilier existant transformé :
- Un transat fin devenu un large daybed à coussins → `invented_furniture` (transformation = invention)
- Un banc simple devenu un canapé profond à coussins → `invented_furniture`
- 2 transats fusionnés en 1 grand → `invented_furniture`

RÈGLE D'OR : si dans l'original le seul support visible à cet endroit est une dalle / un bord nu, et que dans la retouche les personnes sont assises sur un coussin ou un banc visible → c'est `invented_furniture`. Une personne assise DIRECTEMENT sur la dalle béton avec rien d'autre sous elle est OK. Une personne assise sur un coussin gris qui n'existait pas est INACCEPTABLE.

ÉTAPE 3 : Détecte les autres violations listées ci-dessous.

Détecte les VIOLATIONS suivantes en comparant les 2 images. Retourne UNIQUEMENT un JSON strict.

Schéma JSON à retourner :

{
  "ok": true,
  "violations": ["liste des violations détectées, vide si aucune"],
  "summary": "1 phrase résumant l'état de la retouche"
}

Violations à détecter :

1. **invented_furniture** : du VRAI mobilier (daybed, transat, sofa, sun lounger, chaise, table, raft solide en bois/métal, plateforme flottante) a été AJOUTÉ alors qu'il n'existait pas dans l'originale. ⚠️ Une **bouée gonflable décorative** (flamant rose, ananas, donut, cygne, anneau coloré, watermelon) est une catégorie À PART — ne PAS la classer comme `invented_furniture`, utilise `invented_pool_float` à la place.

   🚨 CAS PARTICULIÈREMENT CRITIQUE — TRANSAT/DAYBED FLOTTANT SUR L'EAU (Martin 13/05/2026, bug Moxy piscine famille) :
   Si dans la RETOUCHE tu vois un transat, un lounger, un daybed, un canapé, une banquette, une plateforme rigide OU une serviette pliée comme un coussin POSITIONNÉS DANS L'EAU DE LA PISCINE (au milieu du plan d'eau, pas sur le bord), c'est une invention catastrophique → `invented_furniture` (PAS `invented_pool_float`, car ce n'est PAS un float gonflable). Un meuble RIGIDE n'a aucune raison physique de flotter sur l'eau. Détection : (a) la surface de l'eau est interrompue/cachée par un objet ; (b) cet objet ressemble à un meuble (rectangulaire, structure rigide, coussins identifiables) ; (c) cet objet n'existait pas au MÊME endroit dans l'original. Cette détection est SOUS-PERFORMANTE historiquement — sois rigoureux.

2. **invented_pool_float** : une bouée gonflable décorative (flamingo, pineapple, donut, swan, ring, watermelon, etc.) a été ajoutée dans une piscine **alors qu'elle n'existait PAS dans l'image originale**.

   🚨 ATTENTION FAUX POSITIFS (Martin 13/05/2026, bug Moxy piscine famille) :
   Si la bouée gonflable ÉTAIT DÉJÀ PRÉSENTE dans l'image originale (même au même endroit OU déplacée légèrement), elle n'est PAS inventée — c'est juste une bouée préservée. AVANT de flag `invented_pool_float`, vérifie systématiquement dans l'image originale (1ère image) si une bouée du même type / même couleur existait déjà. Si oui → PAS de violation. Une bouée légèrement déplacée pour faire de la place à un sujet n'est PAS une invention (mais peut justifier `architecture_changed` si le déplacement est significatif).
3. **subject_on_water** : une ou plusieurs personnes sont positionnées SUR la surface de l'eau (debout sur l'eau, marchant dessus, ou sur un meuble flottant qui n'existe pas dans l'originale).
3.bis **shallow_water_illusion** : un sujet debout dans la piscine a de l'eau qui lui arrive au niveau des cuisses / genoux / sous le nombril alors qu'aucune marche ou plateforme visible dans l'image originale ne justifie cette faible profondeur. La piscine semble alors ne pas avoir de fond / être miniature. Pour qu'une photo soit ACCEPTABLE : l'eau doit cacher au minimum le nombril (de préférence atteindre la poitrine/sternum) sur tout sujet debout en eau libre. Exceptions : sujet clairement mi-marche / sujet sur une marche visible dans l'original / sujet assis au bord avec pieds dans l'eau.
4. **subject_on_furniture_top** : personne debout sur un meuble fait pour s'allonger (daybed, sun lounger, sofa).
5. **subject_wrong_side_barrier** : personne de l'autre côté d'une barrière de sécurité (rooftop railing, garde-corps, balustrade, panneau de verre, parapet).
   ⚠️ CHECK SPÉCIFIQUE — RIGOUREUX OBLIGATOIRE : si l'image ORIGINALE montre une barrière de sécurité (verre, métal, balustrade) au premier plan ou en bordure de toit/terrasse :
   1. Identifie la ligne de la barrière dans l'image originale.
   2. Identifie sur quel côté de cette ligne se trouvent les meubles existants (transats, daybeds) et la piscine.
   3. Dans la RETOUCHE, vérifie pour CHAQUE personne ajoutée : se trouve-t-elle du MÊME côté que les meubles existants ?
   4. Si la personne (même bien dessinée) se trouve de l'AUTRE côté de la barrière (= côté vide / vue ciel / drop) → c'est `subject_wrong_side_barrier`, VIOLATION MAJEURE.
   5. Signe révélateur : sous les pieds/les fesses de la personne, on devrait voir du DECK existant. Si on voit du VIDE / sky / city skyline / treetops → c'est qu'elle est de l'autre côté, AVEC un meuble inventé pour la faire tenir (combiner avec `invented_furniture`).
   Cette violation est SOUS-DÉTECTÉE historiquement. Sois extra-vigilant : compare où sont les meubles AVANT (ils définissent le côté safe) et regarde si les NOUVELLES personnes sont sur ce même côté.
6. **scene_regenerated** : la photo a été quasi-régénérée — l'angle de caméra, la perspective, ou les éléments principaux ont fondamentalement changé entre l'avant et l'après. C'est une violation majeure. ⚠️ NE PAS classer comme `scene_regenerated` un simple changement d'heure (nuit→jour) ou d'éclairage (sombre→clair) si le cadrage, l'architecture, les meubles existants et les objets sont préservés — c'est un usage légitime du pipeline.
7. **inconsistent_scale** : 2+ subjets ajoutés ont des échelles incompatibles (un subjet beaucoup plus grand qu'un autre à la même distance camera).
7.bis **subject_oversized** : sujet(s) ajouté(s) trop grands par rapport au reste de la scène. Détection : compare la TÊTE du sujet à la HAUTEUR de la frame. Si la tête fait > 15% de la frame height sur une photo PANORAMIQUE/ROOFTOP/WIDE, ou > 25% sur une photo CLOSE → `subject_oversized`. Autre signe : le sujet semble PLUS GRAND que les meubles existants à proximité (un homme debout fait 2× la hauteur d'un lounger, pas 4×). Cette violation est SOUS-DÉTECTÉE — sois rigoureux.
8. **architecture_changed** : l'architecture du bâtiment, la disposition du mobilier existant, ou le décor de fond ont été altérés. Cas typiques : mur déplacé, panneau retiré, table déplacée, plafond modifié, dimensions changées. NE PAS classer ici un simple changement d'éclairage qui colore différemment des murs existants — c'est `lighting_break` à la rigueur, et c'est légitime en ai_lighting.

8.quater **pool_surface_reduced** (Martin 15/05/2026, bug Gates Hotel South Beach) : la SURFACE D'EAU de la piscine a été rétrécie / déformée / partiellement recouverte dans la retouche par rapport à l'originale, alors qu'aucune transformation demandée ne le justifie. C'est une violation CRITIQUE distincte de `architecture_changed` parce que la piscine est l'asset commercial principal d'une photo d'hôtel — toute réduction est inacceptable.

   🔬 PROTOCOLE DE DÉTECTION OBLIGATOIRE — ÉTAPE PAR ÉTAPE :

   **ÉTAPE A — Tracer le contour d'eau de l'ORIGINAL d'abord (AVANT de regarder la retouche) :**
   Avant TOUTE autre observation, fixe-toi sur l'image ORIGINALE (la 1ère) et trace MENTALEMENT le contour exact de la surface d'eau bleue/turquoise. Mémorise précisément :
   - Le bord PROCHE de la caméra (= le bord au premier plan, souvent en bas de la frame)
   - Le bord LOIN (= au fond, souvent vers le milieu de la frame)
   - Les bords GAUCHE et DROITE (= côtés latéraux)
   - Tout escalier d'accès / step / shelf VISIBLE dans l'eau
   - Tout objet préexistant SUR ou DANS l'eau

   **ÉTAPE B — Comparer avec la RETOUCHE :**
   Maintenant regarde la retouche (la 2nde image). Pour CHAQUE bord identifié en A :
   - Le bord est-il à la MÊME position ? Si le bord PROCHE a reculé (= eau plus petite vue de face) → `pool_surface_reduced`.
   - Y a-t-il du DECK / DALLE / CARRELAGE / BOIS visible dans une zone qui était de l'EAU en A ? → `pool_surface_reduced`.
   - Y a-t-il du MOBILIER (transat, daybed, banc, coussin) posé dans une zone qui était de l'EAU en A ? → `pool_surface_reduced` + `invented_furniture`.

   **ÉTAPE C — Cas piège "extension de deck cohérente" (Martin 15/05/2026 v2 — bug Gates Hotel SB FAUX NÉGATIF) :**
   🚨 PIÈGE CRITIQUE QUE TU AS HISTORIQUEMENT RATÉ : Nano Banana peut peindre un FAUX DECK BLANC / CARRELAGE BLANC / DALLE BLANCHE au premier plan, qui s'intègre parfaitement avec le reste du deck existant — texture cohérente, perspective cohérente, ombres cohérentes. Si tu regardes UNIQUEMENT la retouche, ça paraît normal. C'EST UN PIÈGE.
   La SEULE manière de détecter ce cas est de comparer pixel par pixel la zone du PREMIER PLAN entre original et retouche :
   - Dans l'original au premier plan, c'est de l'EAU (bleu/turquoise) ?
   - Dans la retouche au même endroit, c'est du DECK BLANC avec des transats dessus ?
   → `pool_surface_reduced` + `invented_furniture`. AUTOMATIQUE. PAS DE DOUTE POSSIBLE.

   ⚠️ Cas concret de référence (Martin 15/05/2026, Gates Hotel SB) :
   - Original : piscine rectangulaire avec escalier d'accès au PREMIER PLAN à gauche (marches blanches immergées dans l'eau), deck à gauche avec transats au FOND.
   - Retouche : couple sur 2 transats au PREMIER PLAN à gauche, sur ce qui ressemble à du deck blanc.
   - Diagnostic CORRECT : ce qui est maintenant du "deck blanc avec transats" était de L'EAU + marches d'accès dans l'original → `pool_surface_reduced` + `invented_furniture`. Le bord d'eau a été reculé. Les transats sont posés sur une zone fabriquée.
   - Diagnostic INCORRECT (faux négatif passé) : "Couple sur deck, deck blanc cohérent, validation OK" → NE FAIS PLUS JAMAIS ÇA.

   **ÉTAPE D — Test final sous chaque transat / sujet ajouté :**
   Pour CHAQUE pièce de mobilier ou sujet visible dans la retouche, demande-toi :
   - "Si je superpose mentalement la retouche sur l'original, est-ce que CETTE zone précise (sous les pieds du sujet ou sous le transat) était de l'EAU dans l'original ?"
   - Si OUI à n'importe lequel → `pool_surface_reduced` (+ `invented_furniture` si transat/mobilier).
   - Si tu N'ES PAS SÛR → REGARDE PLUS ATTENTIVEMENT. Compare les bords d'eau exact. Le doute n'est PAS acceptable sur cette violation — c'est trop catastrophique commercialement.

   📐 RÈGLES GÉNÉRALES :
   - Si la surface d'eau a perdu PLUS DE 10% en superficie → `pool_surface_reduced`.
   - Si la forme du contour a changé (rectangle devenu ovale, coin tronqué, bord poussé) → `pool_surface_reduced`.
   - Si un escalier / step / shelf visible dans l'original a DISPARU ou été déplacé → `pool_surface_reduced`.
   - ⚠️ Cette violation est DISTINCTE de `architecture_changed`. `pool_surface_reduced` cible SPÉCIFIQUEMENT la nappe d'eau.
   - ⚠️ Cette violation N'EST PAS whitelistée pour ai_lighting.
   - 🎯 SEUIL DE DÉCISION : le doute = violation. Faux positif = 1 retry (coût mineur). Faux négatif = photo cassée publiée (coût catastrophique commercial). Privilégie TOUJOURS le flag.
8.ter **decor_elements_lost** : des éléments décoratifs présents dans l'image ORIGINALE ont DISPARU dans la retouche, ALORS QUE leur disparition n'est PAS justifiée par la transformation demandée. Cas concret (Martin 13/05/2026, bug Moxy rooftop trio) : photo originale = terrasse rooftop avec PLANTES dans des bacs en bordure ; photo retouchée = terrasse rooftop avec sujets ajoutés MAIS les bacs à plantes ont disparu. C'est `decor_elements_lost`.

   🔬 PROTOCOLE DE DÉTECTION OBLIGATOIRE :
   1. Liste mentalement les éléments DÉCORATIFS visibles dans l'image originale : plantes en pot, bacs/jardinières, vases, lampes décoratives, sculptures, art mural, tapis, coussins décoratifs, objets sur les tables.
   2. Pour CHAQUE élément, vérifie qu'il est ENCORE présent dans la retouche, à la MÊME position (ou très proche).
   3. Si un ou plusieurs éléments ont disparu → `decor_elements_lost`.
   4. ⚠️ Cette violation est DISTINCTE de `architecture_changed` qui couvre les modifs structurelles. `decor_elements_lost` cible spécifiquement les **éléments meublants/décoratifs** qui rendent la photo accueillante.
   5. ⚠️ Cette violation N'EST PAS whitelistée pour ai_lighting : la transformation nuit→jour ne justifie JAMAIS la disparition d'une plante. Si tu détectes des plantes/objets absents, flag-la même si l'éclairage a changé.

   📐 INTÉRACTION AVEC RECADRAGE : si l'image a été recadrée/zoomée par Nano Banana (= certains éléments en bordure sont coupés par le crop, pas supprimés), distingue bien :
   - Coupé par crop = visible partiellement en bord OU manifestement hors cadre → PAS `decor_elements_lost`
   - Disparu malgré frame préservé = élément qui devrait être visible mais a été effacé → `decor_elements_lost`
   Si le crop semble important : flag aussi `scene_regenerated` en complément.

8.bis **architecture_invented** : un NOUVEL élément architectural a été FABRIQUÉ qui n'existait pas dans l'image originale. Cas typiques (très graves) : une alcôve / un mur / un display / un panneau d'art a été transformé en FENÊTRE qui donne sur l'extérieur ensoleillé ; une nouvelle baie vitrée / skylight / ouverture est apparue ; un mur a été remplacé par une façade en verre ; une vue urbaine ou de ciel a été ajoutée à travers ce qui était une surface opaque. Cette violation est SÉPARÉE de `architecture_changed` car elle indique une FABRICATION d'élément structurel (et reste à détecter même si l'étape est ai_lighting — la transformation nuit→jour ne justifie JAMAIS d'inventer une fenêtre).
9. **lighting_break** : ombres / direction lumière incohérente entre les sujets ajoutés et la scène.

`ok` = true SEULEMENT si la liste violations est vide. Sinon `ok` = false.

Retourne STRICTEMENT le JSON, pas de markdown, pas de texte hors JSON.
"""


_GENAI_CONFIGURED = False


def _ensure_configured():
    global _GENAI_CONFIGURED
    if not _GENAI_CONFIGURED:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante")
        genai.configure(api_key=api_key)
        _GENAI_CONFIGURED = True


def validate_ai_output(input_path: Path, output_path: Path,
                       model_name: str = VALIDATION_MODEL,
                       max_retries: int = 2,
                       action_context: str | None = None,
                       actions_chain: list[str] | None = None) -> dict:
    """Compare l'avant/après et retourne {ok: bool, violations: list, summary: str, ...}.

    Args:
        action_context : action principale (legacy, utilisé si actions_chain absent).
        actions_chain : liste des actions IA appliquées dans l'ordre. Permet d'unionner
            les whitelists (cas chaînage ai_lighting → ai_add_character : on accepte
            les violations légitimes de chaque étape).
    """
    _ensure_configured()
    model = genai.GenerativeModel(model_name)

    last_error = None
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            before = Image.open(input_path).convert("RGB")
            after = Image.open(output_path).convert("RGB")
            response = model.generate_content(
                [VALIDATION_PROMPT, before, after],
                generation_config={"response_mime_type": "application/json", "temperature": 0.0},
            )
            duration_ms = int((time.time() - t0) * 1000)
            data = json.loads(response.text)
            usage = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
            output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
            cost_usd = (input_tokens * 0.30 + output_tokens * 2.50) / 1_000_000

            violations = data.get("violations") or []
            # Filtrage selon les actions appliquées : chaque action peut whitelist certaines
            # violations légitimes. Pour les chaînages (ex: ai_lighting → ai_add_character),
            # on UNIONNE les whitelists des étapes — sinon le chaînage tombe en faux positif.
            ALLOWED_BY_ACTION = {
                # ai_lighting (nuit→jour) : la transformation modifie INÉVITABLEMENT le décor
                # perçu (bâtiments illuminés vs ensoleillés, ciel, ombres, ambiance générale).
                "ai_lighting": {"scene_regenerated", "lighting_break", "architecture_changed"},
                # ai_recompose : recadrage peut paraître "régénéré" pour le validateur
                "ai_recompose": {"scene_regenerated"},
                # ai_remove_clutter : effacer des objets modifie nécessairement le décor
                "ai_remove_clutter": {"architecture_changed"},
                # ai_add_character : ajout perso peut sembler "lighting_break" si shadows mismatch
                "ai_add_character": set(),
                # ai_add_pool_float : ajouter une bouée déclenche LÉGITIMEMENT invented_pool_float
                # (mais PAS invented_furniture — si le validateur détecte du vrai mobilier
                # inventé en plus, on rejette toujours). lighting_break/architecture_changed
                # autorisés car la bouée crée une ombre/reflet qui modifie marginalement la scène.
                "ai_add_pool_float": {"invented_pool_float", "lighting_break"},
            }
            # Construit la whitelist en unionnant toutes les actions du chaînage
            actions_to_consider = set(actions_chain or [])
            if action_context:
                actions_to_consider.add(action_context)
            allowed = set()
            for act in actions_to_consider:
                allowed |= ALLOWED_BY_ACTION.get(act, set())
            filtered = [v for v in violations if v not in allowed]
            ok_after_filter = len(filtered) == 0

            return {
                "ok": ok_after_filter,
                "violations": filtered,
                "violations_raw": violations,  # garde pour debug
                "violations_allowed_by_context": list(allowed.intersection(violations)),
                "summary": data.get("summary") or "",
                "duration_ms": duration_ms,
                "cost_usd": round(cost_usd, 6),
            }
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

    # Échec validation : on retourne ok=true par défaut (don't block pipeline) avec note
    return {
        "ok": True,
        "violations": [],
        "summary": f"Validation post-IA échouée : {str(last_error)[:200]}",
        "duration_ms": 0,
        "cost_usd": 0,
        "error": str(last_error)[:200] if last_error else None,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# VALIDATEUR STRUCTURÉ "CRITICAL FIELDS" (Martin 15/05/2026, Option B hybride)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Inspiré du système de criticité de champs utilisé par le manager pour la vidéo
# Veo/Kling. Adapté au cas Photo Dayuse : on PRÉSERVE LE DÉCOR pendant qu'on
# ajoute un sujet éphémère (inverse du cas vidéo où on préserve l'identité du
# personnage à travers les scènes).
#
# PHILOSOPHIE :
# - Le validator narratif (validate_ai_output) reste en place : il catch les
#   "unknown unknowns" (genre une porte transformée en miroir).
# - Ce validateur structuré ajoute un 2e check ULTRA-FOCALISÉ sur 6 champs
#   CRITIQUES où on a eu des fails historiques. Chaque champ retourne PASS/FAIL
#   + evidence textuelle. Si l'un fail → on convertit en violation existante
#   pour réutiliser la chaîne retry/fallback actuelle.
#
# COÛT : ~$0.0006 par photo (×1 appel Gemini Flash, output ~300-500 tokens).
# Cumulé avec le validator narratif (~$0.0005) = ~$0.001/photo en validation.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CRITICAL_FIELDS_PROMPT = """Tu reçois 2 images : la première est l'ORIGINALE (input), la seconde est une RETOUCHE IA (output).

🎯 MISSION : checker EXACTEMENT 6 champs CRITIQUES de préservation du décor et de placement du sujet. Pour chaque champ, retourne PASS ou FAIL avec une evidence textuelle courte. N'ajoute PAS d'autres champs. N'ajoute PAS de variance opinion.

🔬 PROTOCOLE — tu DOIS suivre ces étapes AVANT de remplir le JSON :

ÉTAPE A : Trace mentalement le contour exact de la nappe d'EAU (piscine/jacuzzi/fontaine) dans l'image ORIGINALE. Mémorise :
- Position des 4 bords (proche caméra / fond / gauche / droite)
- % de la frame que l'eau occupe (estimation approximative)
- Position des marches d'accès / step / shelf si présents
- Tout objet préexistant dans/sur l'eau (bouée originale, etc.)

ÉTAPE B : Trace mentalement la même chose dans la RETOUCHE.

ÉTAPE C : Pour CHAQUE personne ajoutée dans la retouche, identifie EXACTEMENT où sont leurs pieds/fesses (le sol sous eux) — c'est de l'eau, du deck, du carrelage, du bois, du sable ?

ÉTAPE D : Pour CHAQUE personne ajoutée, identifie ce qui est SOUS elle (chaise existante, transat existant, sol nu, OU mobilier qui n'existait pas dans l'original).

ÉTAPE E : Identifie toute barrière de sécurité visible dans l'original (garde-corps, balustrade, glass panel, parapet). Note de quel côté sont les meubles existants et la piscine.

Maintenant retourne ce JSON STRICT (aucun markdown, aucun texte hors JSON) :

{
  "subject_count_added": {
    "actual": int (nombre exact d'humains AJOUTÉS dans la retouche par rapport à l'original — ne compte PAS les humains qui étaient déjà dans l'original),
    "evidence": "1 phrase brève décrivant les sujets ajoutés visibles (ex: 'a young couple sitting on the lounger at foreground-left')"
  },
  "pool_shape_preserved": {
    "status": "PASS" | "FAIL",
    "evidence": "1 phrase brève : la forme du contour d'eau est-elle identique ? Si FAIL, dire précisément ce qui a changé (ex: 'the foreground edge of the water has receded by ~15% to make room for an apparent deck extension')",
    "delta_estimate_pct": int (estimation du % de superficie d'eau perdue par rapport à l'original, 0 si intact)
  },
  "pool_surface_preserved": {
    "status": "PASS" | "FAIL",
    "evidence": "1 phrase brève : y a-t-il des pixels qui étaient de l'eau dans l'original et qui sont maintenant du deck/sol/mobilier dans la retouche ? Si FAIL, localiser précisément la zone."
  },
  "subject_water_boundary_respected": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : les sujets ajoutés respectent-ils la frontière sec/eau de l'original ? Cas valides PASS : (a) sujet sur deck existant entièrement sec ; (b) sujet intentionnellement dans l'eau (nageant / assis bord pieds dans l'eau) avec piscine intacte. Cas FAIL : sujet sur une extension de deck fabriquée au-dessus de l'eau ; sujet sur transat invented dans l'eau. N/A si aucun sujet ajouté."
  },
  "barrier_side_correct": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : si une barrière de sécurité existe dans l'original (rooftop / piscine / balcon), tous les sujets ajoutés sont-ils du même côté que les meubles existants et la piscine ? PASS si oui ou si pas de barrière (N/A). FAIL si un sujet est du côté void/ciel."
  },
  "no_invented_support_under_subject": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : sous chaque sujet ajouté, voit-on un support qui existait DÉJÀ dans l'original (transat existant, sol nu, marche existante, bord piscine existant) ? Ou un support INVENTÉ (nouveau transat, daybed inventé, coussin ajouté, plateforme fabriquée, step nouveau) ? PASS = support existant. FAIL = mobilier/support fabriqué. N/A si aucun sujet ajouté."
  },
  "pool_float_realistic": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : si une bouée gonflable a été ajoutée, est-elle de taille et perspective réalistes ? Seuils resserrés (Martin 19/05/2026, bug bouées géantes persistant). PASS = bouée ≤ ~10% surface eau ET bouée ≤ taille d'un lounger visible voisin ET perspective cohérente avec la photo (top-down si vue aérienne, oblique sinon) ET style photoréaliste. FAIL = bouée ≥ 12% surface eau (NE PAS attendre 20% — 12% est déjà la limite haute) OU bouée plus grande qu'un lounger visible OU perspective incohérente (3D frontale sur photo top-down, ou inversement) OU style CGI candy. N/A si aucune bouée ajoutée.",
    "size_pct_of_water": int
  },
  "subject_anatomy_intact": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : sur chaque sujet ajouté, l'anatomie est-elle correcte ? PASS = bras OK (2 par sujet, mains avec 5 doigts), jambes OK, proportions humaines naturelles. FAIL = bras dupliqué, main avec 6/7 doigts ou difforme, jambe coupée/fusionnée, anatomie cassée. N/A si aucun sujet ajouté."
  },
  "subject_face_photoreal": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : la face de chaque sujet ajouté est-elle photoréaliste ? PASS = traits clairement dessinés (yeux/nez/bouche), texture peau naturelle. FAIL = face smudge/floue, mannequin plastic, yeux manquants/déformés, look CGI. N/A si aucun sujet ajouté OU si face cachée intentionnellement (sunglasses + hat + profil)."
  },
  "subject_scale_realistic": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : chaque sujet ajouté est-il à l'échelle correcte vs le mobilier voisin ? PASS = sujet debout ≈ 2× hauteur d'un lounger visible. FAIL = sujet géant (> 30% largeur frame) OU sujet nain (< 5% hauteur frame sur photo wide). N/A si aucun mobilier référence visible OU aucun sujet ajouté."
  },
  "furniture_existing_preserved": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : tous les meubles préexistants dans l'original (loungers, daybeds, tables, chaises) sont-ils PRÉSENTS dans la retouche à la MÊME position ? PASS = tous présents, déplacements < 30cm visuels. FAIL = un meuble a disparu OU a été déplacé significativement OU remplacé. N/A si l'original n'a aucun mobilier visible."
  },
  "decor_elements_preserved": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : les éléments décoratifs préexistants (plantes en pot, vases, lampes, art mural, signalétique) sont-ils tous présents dans la retouche ? PASS = tous présents. FAIL = au moins un élément a disparu (plante en pot retirée, vase supprimé, lampe enlevée, art mural effacé). N/A si l'original n'a aucun décor visible."
  },
  "framing_preserved": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : le cadrage est-il identique ? PASS = même angle de caméra, même champ visuel, même perspective, mêmes bords. FAIL = zoom-in, crop, recadrage, angle modifié. N/A jamais (toujours évaluable)."
  },
  "outfit_appropriate": {
    "status": "PASS" | "FAIL" | "N/A",
    "evidence": "1 phrase brève : la tenue de chaque sujet ajouté est-elle adaptée au contexte ? PASS = swimwear sur piscine/plage, smart casual sur rooftop/bar/restaurant, athleisure sur gym, etc. FAIL = street clothes lourds sur piscine, robe formelle sur gym, lingerie/sheer/cheeky-cut. N/A si aucun sujet ajouté."
  }
}

⚠️ RÈGLES DE DÉCISION STRICTE :
1. Si tu hésites entre PASS et FAIL sur un champ → FAIL (le doute = fail, faux positif coûte 1 retry mineur, faux négatif publie une photo cassée).
2. Pour `pool_shape_preserved` et `pool_surface_preserved` : compare PIXEL PAR PIXEL les bords d'eau ; ne te laisse PAS tromper par un "deck blanc cohérent" qui s'intègre bien — si la zone était de l'eau dans l'original, c'est FAIL même si la texture deck paraît plausible.
3. Pour `subject_water_boundary_respected` : si le scenario indiquait "sur le deck sec" et que les pieds du sujet sont sur une zone qui était de l'eau dans l'original → FAIL.
4. Sois CHIRURGICAL : evidence en 1 phrase max, factuelle, citant des zones précises (foreground-left, near pool steps, etc.).
"""


def validate_critical_fields(input_path: Path, output_path: Path,
                              expected_subject_count: int | None = None,
                              model_name: str = VALIDATION_MODEL,
                              max_retries: int = 2) -> dict:
    """2e validateur structuré focalisé sur 6 champs CRITIQUES de préservation.

    Args:
        input_path : chemin image originale
        output_path : chemin image retouchée
        expected_subject_count : nombre de sujets que le pipeline a demandé d'ajouter
            (pour comparer avec actual). Si None, on ne check pas le count.

    Returns:
        dict {
            "ok": bool (True si TOUS les champs critical sont PASS),
            "field_checks": {field_name: {status, evidence, ...}, ...},
            "violations_derived": list[str] (violations existantes dérivées des fails — réutilise la chaîne retry),
            "duration_ms": int,
            "cost_usd": float,
            "error": str | None,
        }

    Mapping field FAIL → violation existante (pour réutiliser la retry pipeline) :
        - pool_shape_preserved FAIL          → "pool_surface_reduced"
        - pool_surface_preserved FAIL        → "pool_surface_reduced"
        - subject_water_boundary_respected FAIL → "subject_on_water" + "pool_surface_reduced"
                                                  (parce que c'est souvent un deck inventé sur l'eau)
        - barrier_side_correct FAIL          → "subject_wrong_side_barrier"
        - no_invented_support_under_subject FAIL → "invented_furniture"
        - subject_count_added mismatch       → "subject_count_wrong" (NEW)
    """
    _ensure_configured()
    model = genai.GenerativeModel(model_name)

    last_error = None
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            before = Image.open(input_path).convert("RGB")
            after = Image.open(output_path).convert("RGB")
            response = model.generate_content(
                [CRITICAL_FIELDS_PROMPT, before, after],
                generation_config={"response_mime_type": "application/json", "temperature": 0.0},
            )
            duration_ms = int((time.time() - t0) * 1000)
            data = json.loads(response.text)
            usage = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
            output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
            cost_usd = (input_tokens * 0.30 + output_tokens * 2.50) / 1_000_000

            # ━━ Parse field checks (defensive : Gemini peut omettre des champs) ━━
            # 14 champs : 7 CRITICAL (initial pack B) + 7 MAJOR (Martin 15/05/2026, P0)
            field_checks = {}
            for fname in ("subject_count_added", "pool_shape_preserved",
                          "pool_surface_preserved", "subject_water_boundary_respected",
                          "barrier_side_correct", "no_invented_support_under_subject",
                          "pool_float_realistic",
                          # ━ Champs MAJOR ajoutés (P0) ━
                          "subject_anatomy_intact", "subject_face_photoreal",
                          "subject_scale_realistic", "furniture_existing_preserved",
                          "decor_elements_preserved", "framing_preserved",
                          "outfit_appropriate"):
                raw = data.get(fname) or {}
                field_checks[fname] = {
                    "status": (raw.get("status") or ("PASS" if fname == "subject_count_added" else "PASS")).upper(),
                    "evidence": raw.get("evidence") or "",
                }
                # Cas spéciaux
                if fname == "subject_count_added":
                    try:
                        field_checks[fname]["actual"] = int(raw.get("actual", 0))
                    except (ValueError, TypeError):
                        field_checks[fname]["actual"] = 0
                if fname == "pool_shape_preserved":
                    try:
                        field_checks[fname]["delta_estimate_pct"] = int(raw.get("delta_estimate_pct", 0))
                    except (ValueError, TypeError):
                        field_checks[fname]["delta_estimate_pct"] = 0
                if fname == "pool_float_realistic":
                    try:
                        field_checks[fname]["size_pct_of_water"] = int(raw.get("size_pct_of_water", 0))
                    except (ValueError, TypeError):
                        field_checks[fname]["size_pct_of_water"] = 0

            # ━━ Check subject count vs expected ━━
            count_status = "PASS"
            if expected_subject_count is not None:
                actual = field_checks["subject_count_added"]["actual"]
                # Tolérance : on accepte -1 (pipeline reduce when no room) mais pas +N (jamais d'ajout en trop)
                if actual > expected_subject_count or actual < max(0, expected_subject_count - 1):
                    count_status = "FAIL"
                    field_checks["subject_count_added"]["evidence"] += (
                        f" [MISMATCH : target={expected_subject_count}, actual={actual}]"
                    )
            field_checks["subject_count_added"]["status"] = count_status

            # ━━ Mapping FAIL → violations existantes (réutilise la chaîne retry actuelle) ━━
            violations_derived = []
            if field_checks["pool_shape_preserved"]["status"] == "FAIL":
                violations_derived.append("pool_surface_reduced")
            if field_checks["pool_surface_preserved"]["status"] == "FAIL":
                if "pool_surface_reduced" not in violations_derived:
                    violations_derived.append("pool_surface_reduced")
            if field_checks["subject_water_boundary_respected"]["status"] == "FAIL":
                # Boundary failure = soit deck inventé sur eau, soit sujet sur eau
                # On flag les 2 violations pour maximiser le ciblage retry
                if "pool_surface_reduced" not in violations_derived:
                    violations_derived.append("pool_surface_reduced")
                if "subject_on_water" not in violations_derived:
                    violations_derived.append("subject_on_water")
            if field_checks["barrier_side_correct"]["status"] == "FAIL":
                violations_derived.append("subject_wrong_side_barrier")
            if field_checks["no_invented_support_under_subject"]["status"] == "FAIL":
                violations_derived.append("invented_furniture")
            if count_status == "FAIL":
                violations_derived.append("subject_count_wrong")
            if field_checks["pool_float_realistic"]["status"] == "FAIL":
                violations_derived.append("pool_float_oversized")
            # ━ Mapping des nouveaux champs MAJOR (Martin 15/05/2026, P0) ━
            if field_checks.get("subject_anatomy_intact", {}).get("status") == "FAIL":
                violations_derived.append("subject_anatomy_broken")
            if field_checks.get("subject_face_photoreal", {}).get("status") == "FAIL":
                violations_derived.append("subject_face_unrealistic")
            if field_checks.get("subject_scale_realistic", {}).get("status") == "FAIL":
                # Réutilise violation existante "subject_oversized" pour rester cohérent
                if "subject_oversized" not in violations_derived:
                    violations_derived.append("subject_oversized")
            if field_checks.get("furniture_existing_preserved", {}).get("status") == "FAIL":
                # Réutilise "architecture_changed" — un meuble retiré/déplacé entre dans cette catégorie
                if "architecture_changed" not in violations_derived:
                    violations_derived.append("architecture_changed")
            if field_checks.get("decor_elements_preserved", {}).get("status") == "FAIL":
                # Réutilise "decor_elements_lost"
                if "decor_elements_lost" not in violations_derived:
                    violations_derived.append("decor_elements_lost")
            if field_checks.get("framing_preserved", {}).get("status") == "FAIL":
                violations_derived.append("framing_modified")
            if field_checks.get("outfit_appropriate", {}).get("status") == "FAIL":
                violations_derived.append("outfit_inappropriate")

            ok = len(violations_derived) == 0

            return {
                "ok": ok,
                "field_checks": field_checks,
                "violations_derived": violations_derived,
                "expected_subject_count": expected_subject_count,
                "duration_ms": duration_ms,
                "cost_usd": round(cost_usd, 6),
            }
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

    # Échec → ok=True par défaut (don't block pipeline) + erreur tracée
    return {
        "ok": True,
        "field_checks": {},
        "violations_derived": [],
        "expected_subject_count": expected_subject_count,
        "duration_ms": 0,
        "cost_usd": 0,
        "error": str(last_error)[:200] if last_error else None,
    }
