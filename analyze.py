"""POC — Analyse photo Gemini Flash.

3 nœuds minimalistes :
  N1 — Ingestion : charge l'image, vérifie taille min
  N2 — Analyse  : 1 appel Gemini Flash → JSON sémantique structuré
  N3 — Output   : écrit le JSON + log de parcours dans data/output/

Usage CLI :
  python analyze.py                       # toutes les photos de data/input/
  python analyze.py data/input/04_xxx.webp  # une seule

Usage programmatique (depuis Flask) :
  from analyze import get_model, analyze_image_full, analyze_batch
  model = get_model()
  result = analyze_image_full(path, model)
  results = analyze_batch(paths, model, parallel=5)
"""

from __future__ import annotations

import os
import sys
import json
import time
import re as _re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import google.generativeai as genai
from PIL import Image

load_dotenv()

ROOT = Path(__file__).parent
INPUT_DIR = ROOT / "data" / "input"
OUTPUT_DIR = ROOT / "data" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MIN_RESOLUTION = (500, 320)  # seuil bas : les variants Drupal 540x336 et autres mid-res passent

# Si ANALYZE_RATE_LIMIT=0 dans .env → pas de sleep (paid tier).
# Sinon valeur en secondes entre appels (défaut 13s = free tier 5 RPM).
RATE_LIMIT_SLEEP = float(os.getenv("ANALYZE_RATE_LIMIT", "0"))
MAX_RETRIES = 6  # paid tier rate limit peut taper sur de gros batchs

# Pricing Gemini 2.5 Flash paid tier 1 (avril 2026, USD/M tokens)
GEMINI_PRICE_INPUT_USD_PER_M = 0.30
GEMINI_PRICE_OUTPUT_USD_PER_M = 2.50

SYSTEM_PROMPT = """Tu es un analyseur d'images pour Dayuse, plateforme hôtelière de location à la journée.

Tu regardes une photo d'hôtel et tu retournes UNIQUEMENT un objet JSON strict (pas de markdown, pas d'explication).

# Contexte brand Dayuse (DayPass = accès aux aménités à la journée : piscine, cabana, rooftop, spa, F&B, pas la chambre)

## DO visuels (ambiance cible)
- Lumineux, naturel, ensoleillé, chaleureux
- Cadrage propre, peu de bruit visuel
- Palette brand : bleu turquoise → crème → jaune/orange chaud
- Mots-clés émotionnels cibles : bien-être, paisibilité, liberté, soleil, joie, pureté, nature, équilibre, chaleur douce, sophistication

## DON'T
- Ambiances sombres ou scènes de nuit
- Contrastes trop durs
- Angles plats, cadrage non centré sur le sujet
- Tons froids dominants (sauf bleu piscine)

## Piliers marque Dayuse (score chaque photo 0-100 sur chacun)
- FREEDOM : liberté d'usage, contrôle du temps, dépaysement accessible
- WELLNESS : détente, soleil, eau, calme, ritual self-care
- EXPERIENCE : mémorable, partageable, instagrammable, aspirationnel

## Règles IA (utile pour la suite)
- IA générative autorisée sur aménités (ajout humain sur transat, cabana, piscine)
- IA générative INTERDITE sur chambres (pas de fabrication)
- IA OK pour F&B (ajout plat/cocktail si réaliste avec l'offre de l'hôtel)

# Schéma JSON à retourner

{
  "factual": {
    "category": "piscine | piscine_vue_aerienne | cabana | transat | rooftop | spa | f_and_b | beach | gym | chambre | interieur_commun | exterieur | facade | detail | staff | autre",
    "_category_definitions_HELP": "Définitions strictes (ne pas confondre) :\n- piscine : bassin d'eau pour nager (vue à hauteur d'eau ou perspective normale)\n- piscine_vue_aerienne : piscine prise du dessus / drone\n- cabana : structure couverte type tente/pavillon avec mobilier dedans (lounge area)\n- transat : sun lounger / chaise longue (focus mobilier sans cabana)\n- rooftop : toit-terrasse aménagé, perspective vers le ciel ou la ville\n- spa : SOINS — tables de massage, hammam, sauna, jacuzzi, salles de soins, ambiance bien-être. JAMAIS la salle de sport.\n- gym : SALLE DE SPORT — haltères, poids, machines de muscu, tapis, vélos. JAMAIS spa. Si tu vois des poids/dumbbells/machines = gym.\n- f_and_b : UNIQUEMENT si tu vois clairement de la nourriture servie, des boissons (verres remplis, bouteilles), des plats, du couvert dressé, OU une cuisine ouverte. Une scène lounge avec fauteuils/sofas/tables vides MAIS sans bouffe/boisson visible n'est PAS f_and_b — utilise interieur_commun ou exterieur. Le simple fait qu'il y ait des tables ne suffit PAS.\n- beach : plage, sable, océan en perspective ground-level\n- chambre : chambre d'hôtel (lit principal, dressing, salle de bain attenante)\n- interieur_commun : lobby, salon, salle de banquet, hall, espace commun intérieur autre que chambre/spa/gym\n- exterieur : façade, jardin, allée, parking, vue extérieure du bâtiment\n- facade : focus sur la façade du bâtiment\n- detail : zoom sur un objet, mobilier, décor (sans contexte large)\n- staff : photo centrée sur des employés en tenue de travail\nN'invente pas une catégorie pour 'remplir' — utilise 'autre' si vraiment rien ne colle.",
    "categories_secondary": ["liste optionnelle de catégories supplémentaires applicables — IMPORTANT : si une piscine est visible (même partielle, même en arrière-plan), ajoute 'piscine'. Si rooftop, ajoute 'rooftop'. Une scène cabana en bord de pool peut avoir ['cabana', 'piscine']. Renvoie [] si aucune catégorie secondaire."],
    "subjects": ["liste", "des", "éléments", "visibles"],
    "human_count": 0,
    "human_face_visible": false,
    "human_presence_type": "none | partial | full_visible — IMPORTANT : 'none' = aucun humain. 'partial' = on ne voit QUE des fragments (mains tenant un verre, bras, pieds, dos sans visage) — la scène n'est PAS incarnée narrativement, on ne ressent pas de présence humaine. 'full_visible' = au moins une personne avec corps complet OU visage visible — la scène est vraiment incarnée. Cette distinction est cruciale pour décider si la photo a besoin d'humains supplémentaires.",
    "face_visibility": "complete | cropped | not_visible | no_human — 'complete' = visage(s) entier(s) visible(s), pas coupé(s) par le bord du cadre. 'cropped' = visage coupé/décapité par le cadre (front coupé, menton coupé) OU on ne voit que le bas du visage / la moitié. 'not_visible' = humain présent mais visage non visible (de dos, masqué, focus torse seul, gros plan sur les mains/jambes). 'no_human' = aucun humain dans la photo. CRITIQUE pour filtrer les photos lifestyle de qualité.",
    "time_of_day": "jour | nuit | aube_crepuscule | indetermine",
    "background": "description brève du décor"
  },
  "emotional": {
    "sensations": ["3 à 5 mots, ex: détente, liberté, chaleur"],
    "brand_keywords": ["mots du vocabulaire brand Dayuse qui matchent, ex: paisibilité, soleil, nature"],
    "pillar_scores": {
      "freedom": 0,
      "wellness": 0,
      "experience": 0
    }
  },
  "technical_hints": {
    "ambiance": "lumineux-chaud | lumineux-froid | sombre-chaud | sombre-froid | mixte",
    "palette_alignment": "aligned-warm | aligned-cool | off-brand | neutral",
    "ia_compatible": true,
    "ia_reason": "pourquoi cette photo tolère ou refuse de l'édition IA"
  },
  "recommended_placement": {
    "primary": "hero_home | product_page_amenities | product_page_incarnated | rejected | other_touchpoint",
    "reason": "1 phrase justifiant"
  },
  "ai_add_character_candidate": {
    "is_candidate": true,
    "reason": "Photo zoomée sur daybeds vides, idéale pour ajout personnage"
  },
  "hero_quality": {
    "score": 0,
    "is_slot1_worthy": false,
    "reason": "Score 0-100 du 'WOW factor' aspirationnel — capacité à servir de photo de couverture (slot 1) qui fait rêver. Critères CUMULÉS (tous nécessaires pour score ≥ 60) : (a) composition spectaculaire (perspective qui valorise, sujet plein cadre, rule of thirds), (b) AMENITY VISIBLE COMME SUJET PRINCIPAL — la piscine/cabana/rooftop occupe le cadre, PAS en arrière-plan, PAS derrière un humain qui prend toute la place, (c) lumière magique (golden hour, contre-jour, vibrance brand), (d) scène aspirationnelle (donne envie d'y être), (e) absence d'éléments distrayants. \nÉCHELLE : 0-30 = banal/raté, 30-60 = correct, 60-85 = très bien, 85-100 = wow exceptionnel. \nis_slot1_worthy = true UNIQUEMENT si score ≥ 70 ET sujet principal est une amenity dominante (pas un humain en gros plan, pas un objet, pas un intérieur banal). \n\n⚠️ INTERDICTIONS STRICTES — is_slot1_worthy DOIT être false dans CES cas, peu importe la beauté de la photo : photo centrée sur un humain en gros plan/torse/bikini avec amenity en background flou ; portrait sous-marin d'un humain ; selfie ; gros plan d'un cocktail/plat seul ; vue d'objet sans contexte amenity."
  },
  "shot_type": {
    "type": "close_up | medium | wide | aerial",
    "human_can_be_prominent": false,
    "reason": "Type de plan, avec exemples STRICTS :\n- close_up : sujet plein cadre, gros plan. Ex: portrait d'humain (visage/torse occupent ≥ 30% du cadre), femme en bikini cadrée à mi-corps, vue sous-marine d'un nageur, gros plan d'un cocktail/plat, focus serré sur un détail décoratif. SI on voit principalement UN humain avec amenity floue derrière → close_up.\n- medium : plan moyen équilibré. On voit UN sujet humain entier (dans la scène) ET un contexte amenity reconnaissable (transats, eau, structure). Ex: vue de la piscine à hauteur d'eau avec transat au premier plan, table dressée pour 4 avec arrière-plan rooftop visible.\n- wide : plan large. Pas d'humain dominant le cadre. Vue d'ensemble : tout l'aménagement piscine + transats + déco visible, ou tout le rooftop + skyline.\n- aerial : vue du ciel / drone (perspective verticale ou très haute).\n\nhuman_can_be_prominent = true UNIQUEMENT si shot_type ∈ {close_up, medium} ET il y a un mobilier/zone au premier plan où placer un humain qui sera bien visible.\n\n⚠️ RÈGLE STRICTE : si on voit principalement UN HUMAIN qui occupe une grande partie du cadre (torse, gros plan, mi-corps) → c'est OBLIGATOIREMENT close_up, MÊME si on aperçoit l'amenity en arrière-plan."
  },
  "amenity_dominance": {
    "primary_amenity_visible_pct": 0,
    "is_amenity_focused": false,
    "reason": "Sur 0-100, % de la photo occupé visuellement par l'amenity COMME SUJET (PAS comme arrière-plan).\n\n⚠️ RÈGLE CRITIQUE : si l'amenity n'est qu'un fond derrière un humain au premier plan, dominance ≤ 20%, peu importe la couleur de l'eau au fond.\n\nÉCHELLE :\n- 0-15 : amenity invisible ou simplement en arrière-plan flou (humain au premier plan qui prend toute l'attention)\n- 15-30 : amenity partiellement visible mais pas le sujet principal\n- 30-50 : amenity visible, partagée avec d'autres éléments\n- 50-70 : amenity clairement le sujet, occupe le cadre\n- 70-100 : amenity domine totalement, vue panoramique/large\n\nEXEMPLES CRITIQUES :\n- Photo d'une femme en bikini avec piscine derrière elle floue → 10-15% (pas plus). PAS 60%.\n- Vue sous-marine d'un nageur → 5% (la piscine n'est pas le sujet, c'est juste l'eau).\n- Vue large de la piscine avec transats → 70%.\n- Cocktail close-up sur une table avec piscine au fond → 5-10%.\n\nis_amenity_focused = true UNIQUEMENT si primary_amenity_visible_pct ≥ 50 ET l'amenity est clairement le sujet identifiable de la photo."
  },
  "placement_capacity": {
    "seats": 0,
    "water_zones": 0,
    "total": 0,
    "breakdown": "Description courte du décompte (ex: '3 transats vides au premier plan + 1 zone de nage = 4')",
    "reason": "Sur 0-10+, combien de PLACES utilisables identifiées dans la photo pour des humains ajoutés. Compte STRICTEMENT les sièges/transats/chairs/cabana_seats/sofa_spots ENCORE LIBRES (pas occupés) + les zones d'eau exploitables (1 nageur par zone visible). seats = sièges existants vides. water_zones = sections d'eau où on peut placer un nageur ou une personne au bord. total = seats + water_zones. Vide si aucune place naturelle (ex: gros plan d'objet, vue aérienne, photo F&B, intérieur banal sans sièges)."
  },
  "family_friendly_indicators": {
    "is_family_friendly": false,
    "indicators": ["liste descriptive des éléments qui suggèrent un environnement family-friendly : toboggan, aire de jeux enfants, kid pool / pataugeoire, chaise haute, kid menu visible, jouets de piscine, splash pad. Vide si aucun indicateur."],
    "intimacy_indicators": ["liste des éléments qui suggèrent un environnement intime/adulte : éclairage tamisé, bar à cocktails sophistiqué, ambiance lounge feutrée, design minimaliste premium. Vide si aucun."],
    "reason": "is_family_friendly=true UNIQUEMENT si au moins 1 indicateur family clair est visible. Sinon false. Servira à choisir le persona contextuel (families pour photos avec toboggan, couples/solos pour scènes intimistes, etc.). Si la photo est neutre (pas d'enfants visibles, pas de bar adulte), retourne is_family_friendly=false avec listes vides — le pipeline utilisera couples/solos par défaut."
  },
  "safe_zones_for_humans": {
    "safe_areas": ["⚠️ CRITIQUE — c'est le SEUL endroit où un humain pourra être ajouté SANS que l'IA invente du décor. Sois précis et liste UNIQUEMENT les zones VRAIMENT visibles. \n\nFormat de chaque zone : (1) LOCALISATION en référence à des éléments visibles ('sur le 3e transat orange depuis la gauche au premier plan', 'rebord de piscine en bas à droite près de l'échelle visible', 'sur la chaise verte vide à droite de la table en bois'), (2) POSE compatible ('assise jambes dans l'eau', 'allongée en train de lire', 'debout sur la terrasse face caméra', 'nageant breaststroke'), (3) TAILLE relative ('humain de la taille des transats existants'). \n\n🟢 SOIS GÉNÉREUX sur les zones évidentes : si on voit une terrasse / un sol carrelé / des chaises vides / un transat / un sofa / un coin lounge, LISTE-LE comme safe_zone (un humain debout ou assis sur ces éléments existants ne casse rien). Pour TOUTES les photos extérieures avec espace au sol visible, il y a au moins 1-2 safe_zones plausibles. \n\n🔴 NE LISTE PAS : zones cachées partiellement par autre chose, surfaces qui n'existent pas dans le cadre (ex: 'rebord de piscine en haut' alors qu'on voit la vue aérienne), murs verticaux, plafond, surface d'eau (sauf nage). \n\n⛔ Liste VIDE UNIQUEMENT pour les vraies impasses : gros plan d'un objet, vue purement architecturale du toit, photo d'un plat servi, intérieur de salle de bain, photo focus sur un détail décoratif. PAS pour des photos d'extérieur ou intérieur avec espace habitable visible. \n\nMax 3 zones, classées par priorité (la plus aspirationnelle en 1ère)."],
    "unsafe_areas": ["zones À ÉVITER spécifiques à cette photo — ex: 'la surface de l'eau de la piscine (sauf si nageant)', 'derrière la barrière de sécurité', 'sur le toit du daybed', 'devant la fenêtre au fond (humain serait minuscule)', 'rebord de piscine non visible'"],
    "max_recommended": "nombre maximum recommandé d'humains pour cette scène (1-3 selon l'espace dispo). 0 UNIQUEMENT pour les impasses (gros plan objet, vue architecturale toit, etc.). Pour les espaces extérieurs/intérieurs avec sol/sièges visibles, au moins 1."
  },
  "clutter_to_remove": ["liste d'éléments visibles parasites à idéalement retirer pour la version brand. Inclure : câbles électriques, prises, gobelets/bouteilles/serviettes oubliés, jouets de plage (sceau/seau, pelle, ballon, jouets plastique colorés), sacs/sandales/affaires personnelles éparpillées, panneaux/posters, détritus, barrières, hose, extincteurs muraux, eyesores techniques : caméras de surveillance / CCTV, escaliers de secours sur bâtiments voisins, antennes / paraboles, climatiseurs extérieurs / unités AC, gaines de ventilation, grilles techniques, drains visibles, ET AUSSI les LOGOS de marques tierces (sur parasols, coussins, serviettes, panneaux, mobilier — sauf le branding sobre de l'hôtel lui-même). Liste 1 ligne par objet, max 6. Vide si rien. NE PAS lister un cocktail/plat servi sur table dressée (ce n'est pas du clutter, c'est aspirationnel). NE PAS lister du mobilier hôtelier (transats, daybeds, parasols, tables) — seul le LOGO du parasol pose problème, pas le parasol lui-même."],
  "recommended_crop": {
    "should_crop": false,
    "x_min_pct": 0,
    "y_min_pct": 0,
    "x_max_pct": 100,
    "y_max_pct": 100,
    "reason": "Si la photo gagnerait à être recadrée (espace mort à supprimer, sujet à recentrer selon règle des tiers, horizon à redresser, zoom intéressant sur l'élément principal), passe should_crop=true et donne la box de crop en pourcentage de l'image (0-100). Ne propose PAS de crop si la photo est déjà bien cadrée. Le crop ne doit PAS supprimer plus de 40% de l'image (zone à conserver ≥ 60%). \n\n🚨 RÈGLE STRICTE : si un ou plusieurs humains sont visibles dans la photo (corps entier, visage), JAMAIS proposer un crop qui couperait leur tête, leur visage, ou leur corps. Soit le crop préserve TOUS les humains visibles INTÉGRALEMENT, soit should_crop=false. Couper une personne au-dessus du nombril ou décapiter un humain est interdit. \n\nSi pas pertinent, laisse should_crop=false."
  },
  "crop_safe_zones": {
    "humans_bboxes": [
      {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0, "_doc": "Une bbox PAR humain visible. Coordonnées normalisées 0-1 (x,y = coin haut-gauche ; w,h = largeur/hauteur). La bbox englobe tête + torse + membres entiers. Liste vide si aucun humain. CRITIQUE pour le multi-format crop : ces zones ne doivent JAMAIS être coupées."}
    ],
    "main_amenity_bbox": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "_doc": "Bbox de l'amenity principale (piscine, cabana, lit spa, bar, transat, vue rooftop) en coordonnées normalisées 0-1. Si toute la photo est dédiée à l'amenity, x=0,y=0,w=1,h=1. Servira pour centrer le crop multi-format autour de l'élément clé."},
    "critical_zones": [
      {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0, "label": "ex: logo Dayuse discret en bas à droite", "_doc": "Liste d'autres zones à préserver lors d'un crop multi-format (logos sobres, vue significative, élément architectural unique). Vide si rien de critique au-delà des humains et de l'amenity principale."}
    ]
  },
  "slowmo_potential": {
    "has_motion_subject": false,
    "motion_subject": "none",
    "motion_strength": 0,
    "reason": "Évalue si la photo se prête à une animation slow-motion en boucle seamless (cinemagraph). On cherche UN sujet visible qui produit naturellement un mouvement AMBIANT, NON-DIRECTIONNEL et BOUCLABLE — pour qu'un loop ping-pong (forward+reverse) reste invisible.\n\nVALEURS de motion_subject (choisir UNE seule, la plus dominante) :\n- 'water' : surface d'eau visible (piscine, bassin, jacuzzi, mer, lac) — ripples idéaux à animer\n- 'curtains' : voilages, rideaux, tissus légers exposés à un courant d'air\n- 'foliage' : végétation visible (palmiers, plantes, feuillage en extérieur) susceptible de bouger au vent\n- 'fire' : flammes visibles (cheminée, bougies, brasero, feu de camp)\n- 'steam' : vapeur visible (jacuzzi fumant, hammam, plat fumant, douche extérieure chaude)\n- 'fountain' : fontaine ou jet d'eau actif visible\n- 'none' : aucun sujet de mouvement ambiant convenable (intérieur sec, vue architecturale figée, plat servi, gros plan d'objet immobile)\n\nmotion_strength (0-100) : intensité du potentiel de slowmo réussi. Critères :\n- 0-30 : sujet de mouvement présent mais minoritaire dans le cadre, ou compromis (humain qui bouge déjà, sujet directionnel comme un vélo/voiture)\n- 30-60 : sujet bien visible mais partage le cadre avec d'autres éléments\n- 60-85 : sujet de mouvement OCCUPE une portion significative du cadre, idéal pour cinemagraph\n- 85-100 : photo dédiée au sujet (gros plan piscine plein cadre, voilages dominants, cheminée centrée) → slowmo wow\n\nhas_motion_subject = true UNIQUEMENT si motion_subject != 'none' ET motion_strength ≥ 30.\n\n⚠️ INTERDICTIONS — has_motion_subject DOIT être false dans CES cas :\n- photo avec humains très visibles au premier plan (le slowmo va animer leurs cheveux/vêtements bizarrement)\n- photo de nourriture/cocktail close-up (sauf si vapeur dominante)\n- vue aérienne pure (drone) sans surface d'eau\n- photo de nuit sombre (le mouvement passera inaperçu)\n- intérieur banal sans élément animable\n- photo où le sujet motion est minuscule en arrière-plan"
  },
  "issues": ["liste problèmes éventuels pour le brand: sombre, nuit, cadrage raté, etc. Vide si rien."]
}

Retourne STRICTEMENT le JSON. Pas de texte avant/après, pas de ```json fences```.
"""


def get_model(model_name: str = "gemini-2.5-flash"):
    """Configure Gemini et retourne un modèle utilisable."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY manquante dans l'environnement (voir .env)")
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(model_name)


def load_image(path: Path) -> tuple[Image.Image | None, str | None]:
    """N1 — Ingestion. Retourne (image, error_reason). image=None + reason si refus."""
    try:
        img = Image.open(path)
        if img.width < MIN_RESOLUTION[0] or img.height < MIN_RESOLUTION[1]:
            return None, f"résolution trop faible ({img.width}x{img.height} < {MIN_RESOLUTION[0]}x{MIN_RESOLUTION[1]})"
        return img, None
    except Exception as e:
        return None, f"PIL load failed: {type(e).__name__}: {str(e)[:200]}"


def _call_gemini(img: Image.Image, model) -> tuple[dict, int, dict]:
    """Appel Gemini avec retry auto sur 429.

    Returns:
        (analysis, duration_ms, usage) où usage = {input_tokens, output_tokens, cost_usd}
    """
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        t0 = time.time()
        try:
            response = model.generate_content(
                [SYSTEM_PROMPT, img],
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": 0.2,
                },
            )
            duration_ms = int((time.time() - t0) * 1000)

            # Tokens & coût
            usage_meta = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage_meta, "prompt_token_count", 0) if usage_meta else 0
            output_tokens = getattr(usage_meta, "candidates_token_count", 0) if usage_meta else 0
            cost_usd = (
                input_tokens * GEMINI_PRICE_INPUT_USD_PER_M / 1_000_000
                + output_tokens * GEMINI_PRICE_OUTPUT_USD_PER_M / 1_000_000
            )
            usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost_usd, 6),
            }
            return json.loads(response.text), duration_ms, usage
        except Exception as e:
            err = str(e)
            last_error = e
            # 429 / 500 / 502 / 503 / 504 / ResourceExhausted / deadline → retry avec backoff
            is_retryable = (
                "429" in err or "500" in err or "502" in err or "503" in err or "504" in err
                or "deadline" in err.lower() or "resource" in err.lower() or "exhausted" in err.lower()
                or "unavailable" in err.lower() or "timeout" in err.lower()
            )
            if is_retryable and attempt < MAX_RETRIES:
                m = _re.search(r"retry in (\d+(?:\.\d+)?)\s*s", err)
                # Si Gemini nous donne un retry_delay, on l'utilise. Sinon exponential backoff.
                wait = (float(m.group(1)) + 2) if m else min(2 ** attempt * 4, 90)
                time.sleep(wait)
                continue
            # Erreur non-retryable OU retries épuisées : on raise avec un type identifiable
            raise RuntimeError(f"Gemini analyze failed after {attempt + 1} attempts: {type(e).__name__}: {err[:300]}")
    raise last_error


def analyze_image_full(source_path: Path, model, write: bool = True) -> dict:
    """Pipeline complet pour 1 photo : N1 + N2 + (N3 optionnel).

    Args:
        source_path : chemin de la photo
        model : modèle Gemini configuré (via get_model())
        write : si True, sauvegarde le JSON dans data/output/{stem}.json

    Returns:
        Le payload complet (input, trace, analysis).
    """
    trace = []
    abs_path = source_path.resolve()

    # N1 — Ingestion
    t0 = time.time()
    img, ingestion_reason = load_image(source_path)
    n1_entry = {
        "node": "N1_ingestion",
        "result": "pass" if img else "reject",
        "duration_ms": int((time.time() - t0) * 1000),
    }
    if ingestion_reason:
        n1_entry["error"] = ingestion_reason
    trace.append(n1_entry)

    img_size = (img.width, img.height) if img else (0, 0)
    analysis = None
    usage = None

    # N2 — Analyse
    if img is not None:
        t0 = time.time()
        try:
            analysis, duration_ms, usage = _call_gemini(img, model)
            trace.append({
                "node": "N2_analyze_gemini",
                "result": "pass",
                "duration_ms": duration_ms,
                "usage": usage,
            })
        except Exception as e:
            trace.append({
                "node": "N2_analyze_gemini",
                "result": "error",
                "duration_ms": int((time.time() - t0) * 1000),
                "error": str(e)[:500],
            })

    payload = {
        "input": {
            "filename": source_path.name,
            "width": img_size[0],
            "height": img_size[1],
            "path_absolute": str(abs_path),
            "file_url": f"file://{abs_path}",
        },
        "trace": trace,
        "analysis": analysis,
    }

    if write:
        out_path = OUTPUT_DIR / f"{source_path.stem}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    return payload


def analyze_batch(
    paths: list[Path],
    model,
    parallel: int = 5,
    output_dir: Path | None = None,
    progress_callback=None,
    use_cache: bool = False,
) -> list[dict]:
    """Analyse N photos en parallèle (paid tier supporte ~1000 RPM).

    Args:
        paths : liste de paths
        model : modèle Gemini configuré
        parallel : nombre de workers concurrents
        output_dir : si fourni, sauve les JSON dans ce dossier au lieu de OUTPUT_DIR
        progress_callback : callable(done, total, last_filename) appelé après chaque photo
        use_cache : si True, ré-utilise les JSON déjà présents dans output_dir au lieu
                    de relancer Gemini. Permet de skip l'analyse pour économiser ~10min
                    sur des tests itératifs (cf. /api/run resume_from=selection).

    Returns:
        Liste des payloads dans le même ordre que paths.
    """
    results: list[dict | None] = [None] * len(paths)

    def _task(idx_path):
        idx, path = idx_path
        # Cache lookup : si le JSON existe déjà → on le charge directement
        if use_cache and output_dir:
            cached_path = output_dir / f"{path.stem}.json"
            if cached_path.exists():
                try:
                    with open(cached_path) as f:
                        cached = json.load(f)
                    # Marqueur pour debug : la photo a été chargée du cache, pas analysée
                    cached["_from_cache"] = True
                    return idx, cached
                except Exception:
                    # Cache corrompu → on relance l'analyse
                    pass
        # write=False, on gère l'écriture nous-même pour pouvoir customiser output_dir
        payload = analyze_image_full(path, model, write=False)
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            with open(output_dir / f"{path.stem}.json", "w") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        return idx, payload

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = [ex.submit(_task, (i, p)) for i, p in enumerate(paths)]
        done = 0
        for fut in as_completed(futures):
            idx, payload = fut.result()
            results[idx] = payload
            done += 1
            if progress_callback:
                progress_callback(done, len(paths), paths[idx].name)

    # ━ Retry automatique des photos en erreur (1 fois en série, pas en parallèle) ━
    # Beaucoup d'erreurs sont des timeouts/429 transients sur 1 photo isolée.
    # Un retry séquentiel après le batch initial récupère ces photos qui sinon partent en 0/300.
    failed_indexes = []
    for i, r in enumerate(results):
        if r is None:
            failed_indexes.append(i)
            continue
        trace = r.get("trace") or []
        gemini_t = next((t for t in trace if t.get("node") == "N2_analyze_gemini"), None)
        if not gemini_t or gemini_t.get("result") != "pass":
            failed_indexes.append(i)

    if failed_indexes:
        print(f"[analyze_batch] {len(failed_indexes)} photo(s) en erreur, retry séquentiel…")
        for i in failed_indexes:
            try:
                payload = analyze_image_full(paths[i], model, write=False)
                if output_dir:
                    with open(output_dir / f"{paths[i].stem}.json", "w") as f:
                        json.dump(payload, f, indent=2, ensure_ascii=False)
                results[i] = payload
                trace = payload.get("trace") or []
                gemini_t = next((t for t in trace if t.get("node") == "N2_analyze_gemini"), None)
                if gemini_t and gemini_t.get("result") == "pass":
                    print(f"  ✓ retry réussi : {paths[i].name}")
                else:
                    err = gemini_t.get("error", "?") if gemini_t else "no trace"
                    print(f"  ✗ retry encore échoué : {paths[i].name} — {err[:120]}")
            except Exception as e:
                print(f"  ✗ retry crash : {paths[i].name} — {e}")

    return [r for r in results if r is not None]


# ============= CLI =============

def main():
    try:
        model = get_model()
    except RuntimeError as e:
        print(f"ERREUR : {e}")
        sys.exit(1)

    if len(sys.argv) > 1:
        paths = [Path(p) for p in sys.argv[1:]]
    else:
        paths = sorted(INPUT_DIR.glob("*"))
        paths = [p for p in paths if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]

    print(f"Traitement de {len(paths)} photo(s)...")

    if RATE_LIMIT_SLEEP > 0:
        # Mode séquentiel avec sleep (free tier)
        for i, p in enumerate(paths):
            if i > 0:
                print(f"  ⏳ pause {RATE_LIMIT_SLEEP}s")
                time.sleep(RATE_LIMIT_SLEEP)
            print(f"\n# {p.name}")
            payload = analyze_image_full(p, model)
            t = next((e for e in payload["trace"] if e["node"] == "N2_analyze_gemini"), None)
            if t and t.get("result") == "pass":
                a = payload["analysis"]
                print(f"  [N2] OK — {t['duration_ms']}ms | catégorie={a['factual']['category']} | humains={a['factual']['human_count']}")
            elif t and t.get("result") == "error":
                print(f"  [N2] ERROR: {t.get('error', '')[:200]}")
    else:
        # Mode parallèle (paid tier)
        def cb(done, total, name):
            print(f"  [{done}/{total}] {name}")
        results = analyze_batch(paths, model, parallel=5, progress_callback=cb)
        ok = sum(1 for r in results if r["analysis"])
        print(f"\n✓ {ok}/{len(results)} analyses réussies")

    print(f"\n✓ Sortie dans {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
