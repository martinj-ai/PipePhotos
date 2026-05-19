"""Trouve l'URL du compte Instagram officiel d'un hôtel via Gemini.

Stratégie :
  1. Demande à Gemini : compte Instagram officiel de cet hôtel ? (knowledge embedded)
  2. Vérifie que l'URL est bien instagram.com/<handle>
  3. Optionnel : check léger du profile via Playwright (mais Instagram bloque souvent les bots
     pour la requête de check, donc on est tolérant — l'extraction réelle se fait dans
     instagram_scraper)

Returns dict {url, handle, source, confidence, error?} ou None.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()


PROMPT = """Quel est le compte Instagram OFFICIEL de l'hôtel suivant ?

Hôtel : {name}
Ville : {city}
Pays : {country}

🚨 ANTI-HALLUCINATION (CRITIQUE) :
Tu NE DOIS PAS deviner / inventer / construire un handle plausible. Tu dois UNIQUEMENT retourner un handle que tu CONNAIS RÉELLEMENT comme existant et vérifié.
Si tu n'as pas une connaissance directe et fiable du compte (>85% certain qu'il existe vraiment et qu'il appartient à cet hôtel), retourne UNKNOWN.
Beaucoup d'hôtels n'ont PAS de compte Insta dédié (les chaînes utilisent souvent un compte régional générique). Dans ce cas → UNKNOWN.

Format JSON strict :
- {{"url": "https://www.instagram.com/<handle>/", "handle": "<handle>", "confidence": "high|medium|low"}}
- L'URL doit être le compte INSTAGRAM OFFICIEL de l'hôtel (de préférence vérifié bleu).
- Pour les chaînes : donne le compte SPÉCIFIQUE à cet hôtel UNIQUEMENT si tu le connais réellement (ex: instagram.com/hyatt_centric_miami_beach). Sinon → UNKNOWN. Ne donne JAMAIS un compte générique de chaîne.
- L'URL doit commencer par https://www.instagram.com/
- handle = nom du compte (sans le @, sans le slash final)
- Si tu n'es pas certain à >85% : retourne {{"url": "UNKNOWN", "handle": "UNKNOWN", "confidence": "low"}}
- Préfère TOUJOURS retourner UNKNOWN plutôt qu'un handle douteux. Mieux vaut admettre l'ignorance que faire perdre du temps avec une URL fictive.
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


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def _check_profile_exists(profile_url: str) -> dict:
    """Quick check Playwright stealth : le profile Instagram existe-t-il vraiment ?

    Instagram retourne :
    - HTTP 200 + page profile valide → {exists: True, title}
    - HTTP 200 + page "Sorry, this page isn't available." → {exists: False, error}
    - HTTP 404 / 5xx → {exists: False, error: HTTP X}
    - Login wall agressif → on est tolérant, on assume exists=True (le scraping plus tard décidera)

    Coût : ~3-5 sec de Playwright. Vaut largement le coup vs scraper un compte fictif.
    """
    out = {"exists": False, "error": None, "title": None}
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        # Si playwright pas dispo, on skip le check (tolérant, on fera confiance à Gemini)
        out["exists"] = True
        out["error"] = "playwright not installed, skipping check"
        return out

    try:
        from playwright_helpers import chromium_launch_args
        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(headless=True, args=chromium_launch_args())
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = context.new_page()
            try:
                resp = page.goto(profile_url, wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                out["error"] = f"goto failed: {type(e).__name__}: {str(e)[:120]}"
                browser.close()
                return out
            if not resp:
                out["error"] = "no response"
                browser.close()
                return out
            page.wait_for_timeout(1500)
            status = resp.status
            title = (page.title() or "").strip()
            out["title"] = title
            title_lc = title.lower()

            # HTTP 4xx / 5xx → erreur direct
            if status >= 400:
                out["error"] = f"HTTP {status}"
                browser.close()
                return out

            # Title-based detection : "Page Not Found • Instagram" (en anglais) / equivalent FR
            if "page not found" in title_lc or "page introuvable" in title_lc:
                out["error"] = "Profile inexistant (page not found)"
                browser.close()
                return out

            # Body-based : Instagram affiche "Sorry, this page isn't available."
            try:
                body_text = page.evaluate(
                    "document.body ? document.body.innerText.substring(0, 1000).toLowerCase() : ''"
                )
            except Exception:
                body_text = ""

            INVALID_MARKERS = (
                "sorry, this page isn't available",
                "désolé, cette page n'est pas disponible",
                "the link you followed may be broken",
                "le lien que vous avez suivi est peut-être rompu",
                "page not found",
                "user not found",
            )
            if any(marker in body_text for marker in INVALID_MARKERS):
                out["error"] = "Profile inexistant (page Instagram 'not available')"
                browser.close()
                return out

            # Sinon on assume que le profile existe (même si login wall agressif)
            out["exists"] = True
            browser.close()
    except Exception as e:
        out["error"] = f"check error: {type(e).__name__}: {str(e)[:120]}"

    return out


def _normalize_url(url: str) -> tuple[str | None, str | None]:
    """Normalise une URL Instagram et extrait le handle. Retourne (url, handle) ou (None, None)."""
    if not url or not url.startswith("https://"):
        return None, None
    try:
        parsed = urlparse(url)
        if "instagram.com" not in parsed.netloc:
            return None, None
        # Extrait le handle du path
        path = parsed.path.strip("/")
        if not path:
            return None, None
        handle = path.split("/")[0]
        # Validation handle Instagram (alphanumeric + . _ , 1-30 chars)
        if not re.match(r"^[A-Za-z0-9._]{1,30}$", handle):
            return None, None
        normalized_url = f"https://www.instagram.com/{handle}/"
        return normalized_url, handle
    except Exception:
        return None, None


def find_hotel_instagram(name: str, city: str | None = None, country: str | None = None) -> dict | None:
    """Trouve le compte Instagram officiel de l'hôtel via Gemini.

    Returns:
        {
            "url": str,
            "handle": str (sans @),
            "source": "gemini",
            "confidence": "high|medium|low",
        }
        OU {"url": None, "error": str} si Gemini ne trouve pas / URL invalide.
    """
    if not name:
        return None

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
        return {"url": None, "source": "gemini", "error": f"Gemini API error: {str(e)[:200]}"}

    raw_url = (data.get("url") or "").strip()
    handle_claim = (data.get("handle") or "").strip().lstrip("@")
    confidence = data.get("confidence", "medium")

    if not raw_url or raw_url == "UNKNOWN":
        return {"url": None, "source": "gemini", "error": "Gemini pas certain du compte Instagram", "confidence": confidence}

    url, handle = _normalize_url(raw_url)
    if not url:
        return {"url": None, "source": "gemini", "error": f"URL Instagram invalide : {raw_url[:120]}", "confidence": confidence}

    # ⚠️ Note : on NE PEUT PAS check si le profile existe vraiment via Playwright stealth.
    # Instagram bloque tous les bots anonymes depuis 2024 et affiche "Profile isn't available"
    # même pour les profils existants. Le seul moyen fiable serait une API tierce
    # (RapidAPI Instagram Scraper, ScrapingBee Insta endpoint).
    # On fait donc confiance à Gemini (avec prompt anti-hallucination renforcé) et l'erreur
    # remontera naturellement au niveau du scraper (0 photos retournées) si le compte est faux.
    return {
        "url": url,
        "handle": handle or handle_claim,
        "source": "gemini",
        "confidence": confidence,
        "warning": "⚠️ Instagram ne permet plus de vérifier l'existence des profils sans login. Si le compte n'existe pas, le scraping retournera simplement 0 photos.",
    }


# CLI
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python instagram_finder.py 'Nom Hôtel' 'Ville' 'Pays'")
        sys.exit(1)
    name = sys.argv[1]
    city = sys.argv[2] if len(sys.argv) > 2 else ""
    country = sys.argv[3] if len(sys.argv) > 3 else ""
    result = find_hotel_instagram(name, city, country)
    print(json.dumps(result, indent=2, ensure_ascii=False))
