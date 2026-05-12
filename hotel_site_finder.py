"""Trouve l'URL du site officiel d'un hôtel via Gemini, vérifie qu'elle répond.

Stratégie :
  1. Demande à Gemini : URL officielle de cet hôtel ? (knowledge embedded + recherche)
  2. Vérifie HTTP 200 (suit redirects)
  3. Vérifie que le <title> de la page contient au moins un mot du nom hôtel
  → Si tout passe : return {url, source: "gemini", confidence}
  → Sinon : return None (le pipeline tombe sur fallback Booking/RP)
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from urllib.parse import urlparse
import urllib3
from google import genai
from google.genai import types
from dotenv import load_dotenv

urllib3.disable_warnings()  # certains sites hôteliers ont des certs douteux

load_dotenv()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# Domaines connus à exclure (ce sont des plateformes, pas le site officiel de l'hôtel)
BLACKLIST_DOMAINS = {
    "booking.com", "expedia.com", "tripadvisor.com", "hotels.com", "agoda.com",
    "kayak.com", "trivago.com", "priceline.com", "orbitz.com", "travelocity.com",
    "resortpass.com", "dayuse.com", "hotwire.com", "skyscanner.com",
    "google.com", "facebook.com", "instagram.com", "twitter.com", "linkedin.com",
    "yelp.com", "wikipedia.org",
}

# Domaines de chaînes hôtelières connues protégées par Cloudflare Enterprise.
# Pour ces domaines, on FAIT CONFIANCE à l'URL Gemini (pas de check 200 — Cloudflare
# bloquera systématiquement notre Playwright). L'extraction tentera quand même, et si
# elle échoue on retombe sur Booking/RP via le orchestrateur.
TRUSTED_CHAIN_DOMAINS = {
    "hyatt.com",        "marriott.com",      "hilton.com",       "ihg.com",
    "accor.com",        "accorhotels.com",   "sofitel.com",      "novotel.com",
    "ibis.com",         "mercure.com",       "pullman.com",      "raffles.com",
    "fairmont.com",     "swissotel.com",     "mgallery.com",
    "westin.com",       "sheraton.com",      "wal­dorfastoria.com", "stregis.com",
    "ritzcarlton.com",  "wynnhotels.com",    "fourseasons.com",  "shangri-la.com",
    "rosewoodhotels.com","mandarinoriental.com","aman.com",      "soho-house.com",
    "kimptonhotels.com","cromwell.com",      "intercontinental.com","crowneplaza.com",
    "holidayinn.com",   "candlewoodsuites.com","staybridge.com", "regenthotels.com",
    "loewshotels.com",  "omnihotels.com",    "wyndhamhotels.com","choicehotels.com",
}

PROMPT = """Quel est le site web officiel de l'hôtel suivant ?

Hôtel : {name}
Ville : {city}
Pays : {country}

Règles strictes :
- Retourne UNIQUEMENT un objet JSON avec cette structure : {{"url": "https://...", "confidence": "high|medium|low"}}
- L'URL doit être le site OFFICIEL de l'hôtel (chaîne directe ou site dédié), pas une plateforme de réservation (Booking, Expedia, TripAdvisor, ResortPass, Hotels.com, etc.)
- Si l'hôtel fait partie d'une chaîne (Hyatt, Hilton, Marriott, IHG, Accor, etc.), donne la page DÉDIÉE de cet hôtel sur le site de la chaîne (ex: hyatt.com/.../coconut-point) — pas la home générique de la chaîne.
- L'URL doit commencer par https://
- Si tu n'es pas certain, retourne {{"url": "UNKNOWN", "confidence": "low"}}.
- Pas de markdown, pas de texte hors JSON.
"""


_CLIENT = None


def _get_client():
    global _CLIENT
    if _CLIENT is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante")
        _CLIENT = genai.Client(api_key=api_key)
    return _CLIENT


def _ask_gemini(name: str, city: str, country: str) -> dict | None:
    """Demande à Gemini l'URL officielle. Retourne {url, confidence} ou None."""
    client = _get_client()
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[PROMPT.format(name=name, city=city or "?", country=country or "?")],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        data = json.loads(response.text)
    except Exception as e:
        return None

    url = (data.get("url") or "").strip()
    if not url or url == "UNKNOWN" or not url.startswith("https://"):
        return None
    return {"url": url, "confidence": data.get("confidence", "medium")}


def _is_blacklisted(url: str) -> bool:
    """True si l'URL pointe vers une plateforme connue (pas un site officiel d'hôtel)."""
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return True
    return any(host == d or host.endswith("." + d) for d in BLACKLIST_DOMAINS)


def _check_url_alive(url: str, hotel_name: str | None = None) -> dict:
    """Vérifie HTTP 200 + match du nom dans le titre, via Playwright + stealth (bypass anti-bot Cloudflare)."""
    out = {"alive": False, "status": None, "title": None, "name_match": False, "error": None}
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    try:
        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
            if not response:
                out["error"] = "no response"
                browser.close()
                return out
            out["status"] = response.status
            if response.status != 200:
                out["error"] = f"HTTP {response.status}"
                browser.close()
                return out
            page.wait_for_timeout(1500)  # laisse le JS se setup
            title = (page.title() or "").strip()[:200]
            out["alive"] = True
            out["title"] = title
            if hotel_name and title:
                title_lc = title.lower()
                stop = {"hotel", "resort", "the", "and", "spa", "suites", "inn"}
                tokens = [t.lower() for t in re.findall(r"[A-Za-zÀ-ÿ]{4,}", hotel_name) if t.lower() not in stop]
                out["name_match"] = any(t in title_lc for t in tokens) if tokens else True
            browser.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def find_hotel_site(name: str, city: str | None = None, country: str | None = None) -> dict | None:
    """Trouve l'URL du site officiel de l'hôtel.

    Returns:
        {
            "url": str,
            "source": "gemini",
            "confidence": "high|medium|low",
            "title": str (titre de la page validée),
            "name_match": bool,
        }
        OU None si rien trouvé / non vérifiable.
    """
    if not name:
        return None

    suggestion = _ask_gemini(name, city or "", country or "")
    if not suggestion:
        return {"url": None, "source": "gemini", "error": "Gemini n'a pas trouvé d'URL fiable"}

    url = suggestion["url"]
    if _is_blacklisted(url):
        return {"url": None, "source": "gemini", "error": f"URL Gemini = plateforme exclue ({urlparse(url).netloc})"}

    # ━━ Trusted chain domains : on FAIT CONFIANCE à Gemini sans tester l'URL ━━
    # Bug observé Martin (12/05/2026) sur Moxy Miami South Beach (Marriott) : Cloudflare
    # Enterprise renvoie HTTP 403 à notre Playwright stealth pour les chaînes (Marriott,
    # Hilton, Hyatt, Accor...). _check_url_alive échouait → erreur "URL Gemini ne répond
    # pas : HTTP 403" → photos officielles perdues, alors que ces sites HÉBERGENT bien
    # une galerie hôtel valide. TRUSTED_CHAIN_DOMAINS était déjà défini mais jamais
    # utilisé. Fix : si l'URL appartient à un domaine trusted, on skip le check d'aliveness
    # → l'extracteur (hotel_gallery_extractor) tentera Playwright sur l'URL. Si lui aussi
    # échoue, on tombe naturellement sur Booking via le orchestrateur.
    host = urlparse(url).netloc.lower().lstrip("www.")
    is_trusted_chain = any(host == d or host.endswith("." + d) for d in TRUSTED_CHAIN_DOMAINS)

    if is_trusted_chain:
        return {
            "url": url,
            "source": "gemini",
            "confidence": suggestion.get("confidence", "medium"),
            "title": None,
            "name_match": None,
            "trusted_chain_skip_check": True,
        }

    check = _check_url_alive(url, hotel_name=name)
    if not check["alive"]:
        return {
            "url": None,
            "source": "gemini",
            "error": f"URL Gemini ne répond pas : {check.get('error') or 'status=' + str(check.get('status'))}",
            "url_attempted": url,
        }

    # Si le titre ne match pas du tout le nom de l'hôtel, c'est probablement une mauvaise URL
    if check["title"] and not check["name_match"]:
        return {
            "url": None,
            "source": "gemini",
            "error": f"URL Gemini répond mais titre '{check['title']}' ne contient pas le nom de l'hôtel",
        }

    return {
        "url": url,
        "source": "gemini",
        "confidence": suggestion.get("confidence", "medium"),
        "title": check["title"],
        "name_match": check["name_match"],
    }


# CLI pour test
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python hotel_site_finder.py 'Nom Hôtel' 'Ville' 'Pays'")
        sys.exit(1)
    name = sys.argv[1]
    city = sys.argv[2] if len(sys.argv) > 2 else ""
    country = sys.argv[3] if len(sys.argv) > 3 else ""
    result = find_hotel_site(name, city, country)
    print(json.dumps(result, indent=2, ensure_ascii=False))
