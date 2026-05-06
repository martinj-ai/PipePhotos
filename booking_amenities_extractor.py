"""Extraction des amenities + métadonnées hôtel depuis une page Booking.com via Gemini.

Inspiré du `passe1_v4.py` (Rooftop project) — adapté à notre stack Gemini + taxo étendue.

Workflow :
  1. Scrape HTML de la page Booking via Playwright (réutilise booking_scraper)
  2. Extrait le texte brut (BeautifulSoup, ~15K chars)
  3. Envoie à Gemini Flash avec prompt strict (règles d'exclusion, sections, validation FAQ)
  4. Parse le JSON retourné → {name, city, country, amenities_normalized}
  5. Le format de sortie est compatible avec rp_scraper.scrape() (drop-in replacement)

Taxonomie : pool / cabana / rooftop / spa / beach / food / bar / gym
(au lieu des 3 amenities du passe1_v4 : rooftop / pool / spa)

Usage :
    from booking_amenities_extractor import extract_hotel_data_from_booking
    data = extract_hotel_data_from_booking("https://www.booking.com/hotel/us/yotel-miami.html")
    # → {name, city, country, amenities_normalized, vibe_primary, personas_allowed, ...}
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from google import genai
from google.genai import types
from dotenv import load_dotenv

import booking_scraper  # pour _clean_url et patterns

load_dotenv()


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Liste des amenities cibles (notre taxo Day Pass)
TARGET_AMENITIES = ["pool", "cabana", "rooftop", "spa", "beach", "food", "bar", "gym"]


# ━━ Prompt Gemini (basé sur passe1_v4 Rooftop, étendu et adapté Day Pass) ━━

EXCLUSION_RULES = """=== RÈGLES D'EXCLUSION STRICTES ===
Ne compter QUE les amenities qui APPARTIENNENT à l'hôtel lui-même et sont OUVERTES À TOUS LES CLIENTS Day Pass.
Tu DOIS exclure TOUTES les mentions suivantes :
- Attractions touristiques à proximité ("Sky Garden 5 km", "near Observation Deck", "10 min walk from...")
- Noms de chambres ou suites ("Rooftop Suite", "Pool View Room", "Spa Deluxe Room")
- Amenities accessibles uniquement depuis certaines chambres ("Executive Rooms give access to Roof Top Lounge", "Suites offer access to the pool", "Club rooms include spa access"). Si l'accès est conditionné à un type de chambre, ce n'est PAS un amenity ouvert.
- Mentions de type "rooms overlook the pool", "room with pool view" (descriptions de vue, pas d'amenities)
- Questions FAQ : "Y a-t-il une piscine ?" → seule la RÉPONSE compte (voir règles FAQ ci-dessous)
- Mentions négatives ("no pool", "doesn't have a rooftop", "there is no swimming pool")
- "spa bath" (baignoire balnéo), "spa bathrobe" (peignoir), "spa-inspired bathroom" : ce ne sont PAS des spas
- Noms de rues / quartiers ("Spa Road", "Pool Street", "Rooftop Gardens district")
- Amenities privées dans la chambre ("in-room jacuzzi", "private plunge pool in suite")
- Amenities situées dans un AUTRE établissement ("free use of pool at nearby property", "spa at sister hotel", "shared pool with adjacent property")
- Toute mention "nearby", "affiliated", "sister hotel", "partner hotel", "km away", "minutes away", "discounts at" suivie d'une amenity = EXCLURE
- Breadcrumbs de navigation, headers, footers, menus, suggestions d'hôtels similaires, prix, disponibilités, fine print"""


AMENITY_DEFINITIONS = """=== DÉFINITIONS DES AMENITIES (taxonomie Day Pass) ===

POOL — piscine intérieure / extérieure / à débordement / chauffée / bassin de nage / pataugeoire / plunge pool de l'hôtel (pas en chambre).

CABANA — structures couvertes type tente / pavillon avec mobilier lounge dedans (cabana, daybed, lounge area).
  Attention : un simple "outdoor seating area" ou des transats sans cabana = PAS cabana.

ROOFTOP — espace SUR LE TOIT ou en ÉTAGE ÉLEVÉ avec vue. Mots-clés : rooftop bar, rooftop restaurant, rooftop pool, sky bar, sky lounge, sky garden, terrasse panoramique en hauteur, bar au dernier étage.
  CONDITION : indication explicite que c'est en hauteur (rooftop, roof, sky, top floor, highest, panoramic from above, étage élevé).
  NE PAS compter : "terrace", "outdoor terrace", "garden", "patio", "courtyard", "balcony" sans mention "sur le toit" / "en hauteur".

SPA — hammam, sauna, bain de vapeur, massage, soins, jacuzzi (de l'hôtel pas chambre), salon de beauté, balnéo, centre de bien-être, espace détente, spa center.

BEACH — accès à une plage privée ou directe. Mots-clés : private beach, beachfront, direct beach access, beach club. NE PAS compter : "près de la plage" / "5 min walk to beach" / "plage à proximité".

FOOD — restaurant servant des repas (breakfast / lunch / dinner / brunch) sur place. Restaurant on site, fine dining, bistro, brasserie. Important : confirmer que c'est un VRAI restaurant, pas juste un "snack" ou "minibar".

BAR — bar à cocktails, lounge bar, pool bar, lobby bar, sky bar (peut être ROOFTOP en plus). Confirmé si on parle de "drinks", "cocktails", "wine list", "bar menu".

GYM — salle de sport / fitness center / fitness room / 24h gym. Présence d'équipement musculation / cardio. NE PAS compter : "yoga classes" sans gym physique, "outdoor exercise area" sans équipement."""


SYSTEM_PROMPT = f"""Tu es un analyste hôtelier qui détecte les amenities ouvertes à tous les clients Day Pass d'un hôtel à partir de sa page BOOKING.

Ton output doit être un JSON STRICT avec :
1. Métadonnées hôtel (name, city, country) extraites du contenu Booking
2. Pour chaque amenity de la taxonomie : présence boolean + niveau de confiance + types détectés + phrases clés

{EXCLUSION_RULES}

=== RÈGLE FAQ / Q&A BOOKING (TRÈS IMPORTANT) ===
La page Booking contient souvent des sections FAQ "Is there a swimming pool?". Règles strictes :
- FAQ qui dit explicitement "doesn't have", "is no", "there is no" → amenity ABSENTE (override toutes autres mentions)
- FAQ qui mentionne accès EXTERNE ("nearby", "discounts at", "partner property") → amenity ABSENTE (pas sur place)
- FAQ qui confirme positivement ("Yes, there's an indoor pool") → confirmation forte
- Les QUESTIONS seules ("Is there a pool?") ne confirment ni n'infirment rien

=== SECTIONS À IDENTIFIER ===
- desc = description de l'établissement (property description)
- facilities = équipements listés (Most popular facilities, Property facilities)
- restaurants = restaurants et bars sur place (Food & Drink, Restaurants on site)
- reviews = avis clients (guest reviews, score breakdown)

{AMENITY_DEFINITIONS}

=== FORMAT DE RÉPONSE JSON ===
{{
  "hotel_meta": {{
    "name": "Nom officiel de l'hôtel extrait de Booking",
    "city": "Ville",
    "country": "Pays"
  }},
  "amenities": {{
    "pool":    {{"present": true, "confidence": "high|medium|low", "types": ["indoor", "outdoor"], "evidence": ["phrase1", "phrase2"]}},
    "cabana":  {{"present": false, "confidence": "high|medium|low", "types": [], "evidence": []}},
    "rooftop": {{"present": true, "confidence": "high", "types": ["bar", "lounge"], "evidence": ["The rooftop bar offers..."]}},
    "spa":     {{...}},
    "beach":   {{...}},
    "food":    {{...}},
    "bar":     {{...}},
    "gym":     {{...}}
  }}
}}

Règles strictes du JSON :
- Toutes les amenities (pool, cabana, rooftop, spa, beach, food, bar, gym) DOIVENT être présentes dans la réponse, même si absent → present=false.
- "evidence" : 1 à 3 phrases courtes (extraits textuels Booking). Vide si present=false.
- "types" : précisions sur le type ("indoor", "outdoor", "rooftop", "infinity") — vide si pas pertinent.
- "confidence" : "high" si plusieurs sources concordantes, "medium" si 1 source claire, "low" si mention ambigüe.
- Pas de markdown. Pas de texte hors JSON."""


# ━━ Scraping Playwright (texte brut de la page Booking) ━━

def _fetch_booking_html(url: str, timeout: int = 30000) -> str | None:
    """Récupère le HTML de la page Booking via Playwright stealth."""
    cleaned = booking_scraper._clean_url(url) if hasattr(booking_scraper, "_clean_url") else url
    try:
        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-GB",
            )
            page = context.new_page()
            try:
                response = page.goto(cleaned, wait_until="domcontentloaded", timeout=timeout)
            except Exception as e:
                browser.close()
                return None
            if not response or response.status >= 400:
                browser.close()
                return None
            page.wait_for_timeout(2500)
            # Wait for property description selector if present
            try:
                page.wait_for_selector(
                    "[data-testid='property-description'], "
                    "#property_description_content, "
                    ".hotel-description",
                    timeout=8000,
                )
            except Exception:
                pass
            html = page.content()
            browser.close()
            return html if html and len(html) > 5000 else None
    except Exception:
        return None


def _extract_text_from_html(html: str, max_chars: int = 15000) -> str:
    """Extrait le texte brut d'une page HTML, tronqué pour rester dans les limites Gemini."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_chars:
        text = text[:max_chars] + " [...]"
    return text


# ━━ Appel Gemini ━━

_GENAI_CLIENT = None


def _get_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante")
        _GENAI_CLIENT = genai.Client(api_key=api_key)
    return _GENAI_CLIENT


def _analyze_with_gemini(text: str, fallback_name: str = "", fallback_city: str = "") -> dict:
    """Envoie le texte Booking à Gemini et parse le JSON retourné."""
    client = _get_client()
    user_prompt = (
        f"Analyse la page BOOKING ci-dessous (texte brut) et retourne le JSON d'amenities + méta.\n\n"
        f"Si l'hôtel n'apparaît pas clairement nommé dans le texte, utilise comme fallback : "
        f"name='{fallback_name}', city='{fallback_city}'.\n\n"
        f"=== TEXTE BOOKING ===\n{text if text else '(page non disponible)'}"
    )
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[SYSTEM_PROMPT, user_prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        return {"error": f"Gemini call failed: {type(e).__name__}: {str(e)[:200]}"}


# ━━ Helper : convert vers le format rp_scraper-compatible ━━

def _to_rp_compatible(gemini_result: dict, booking_url: str) -> dict:
    """Convertit le résultat Gemini vers le même format que rp_scraper.scrape() retourne.

    Drop-in replacement pour que app.py n'ait pas besoin de différencier les sources.
    """
    if "error" in gemini_result:
        return {"error": gemini_result["error"], "booking_url": booking_url}

    meta = gemini_result.get("hotel_meta", {})
    amenities_obj = gemini_result.get("amenities", {})

    # Construit amenities_normalized au format rp_scraper {amenity: bool}
    amenities_normalized = {}
    amenities_details = {}  # pour debug / UI : confidence + evidence
    for key in TARGET_AMENITIES:
        a = amenities_obj.get(key, {})
        present = bool(a.get("present", False))
        amenities_normalized[key] = present
        if present:
            amenities_details[key] = {
                "confidence": a.get("confidence", "medium"),
                "types": a.get("types", []),
                "evidence": a.get("evidence", []),
            }

    return {
        "rp_id": None,  # pas de RP id puisque Booking only
        "rp_url": None,
        "booking_url": booking_url,
        "name": meta.get("name") or "",
        "city": meta.get("city") or "",
        "country": meta.get("country") or "",
        "state": "",
        "star_classification": None,
        "avg_rating": None,
        "vibe_primary": None,  # Suppression vibe : voir doc Vision & Roadmap
        "tags": [],
        "amenities_raw": [k for k, v in amenities_normalized.items() if v],
        "amenities_normalized": amenities_normalized,
        "amenities_details": amenities_details,
        "personas_allowed": ["couples", "small_groups", "families", "solos"],  # liste statique (suppression vibe)
        "image_count": 0,  # rempli par booking_scraper.scrape_booking_photos plus tard
        "image_urls": [],
        "source": "booking",
    }


# ━━ API publique ━━

def extract_hotel_data_from_booking(
    booking_url: str,
    fallback_name: str = "",
    fallback_city: str = "",
) -> dict:
    """Extrait amenities + méta hôtel depuis une URL Booking.

    Args:
        booking_url : URL Booking (https://www.booking.com/hotel/...)
        fallback_name / fallback_city : utilisés si Gemini n'arrive pas à extraire les meta de Booking

    Returns:
        Dict format compatible rp_scraper.scrape() :
        {
            name, city, country, amenities_normalized: {pool: bool, ...},
            amenities_details: {pool: {confidence, types, evidence}, ...},
            personas_allowed (liste statique),
            ...
        }
        Ou {"error": "..."} en cas d'échec.
    """
    if not booking_url or "booking.com/hotel" not in booking_url:
        return {"error": "URL Booking invalide"}

    html = _fetch_booking_html(booking_url)
    if not html:
        return {"error": "Échec scraping Booking (Cloudflare / 403 / contenu vide)"}

    text = _extract_text_from_html(html)
    if not text or len(text) < 500:
        return {"error": "Texte extrait trop court depuis Booking"}

    gemini_result = _analyze_with_gemini(text, fallback_name=fallback_name, fallback_city=fallback_city)
    return _to_rp_compatible(gemini_result, booking_url)


# CLI debug
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python booking_amenities_extractor.py <booking_url>")
        sys.exit(1)
    result = extract_hotel_data_from_booking(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False))
