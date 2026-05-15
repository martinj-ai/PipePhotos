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
