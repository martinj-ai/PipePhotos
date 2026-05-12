"""RP finder — trouve l'URL ResortPass d'un hôtel via DuckDuckGo + Gemini cascade.

Pattern identique à expedia_finder.py / hotel_site_finder.py.
URL RP typique : https://www.resortpass.com/hotels/moxy-miami-south-beach
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

RP_DOMAIN = "resortpass.com"

PROMPT = """Quel est l'URL de la fiche hôtel sur ResortPass.com pour l'hôtel suivant ?

Hôtel : {name}
Ville : {city}
Pays : {country}

Règles strictes :
- Retourne UNIQUEMENT un objet JSON avec cette structure : {{"url": "https://...", "confidence": "high|medium|low"}}
- L'URL doit pointer vers la fiche HÔTEL sur resortpass.com
- Format canonique : https://www.resortpass.com/hotels/{{slug-de-l-hotel}}
- L'URL doit commencer par https:// et le domaine doit être resortpass.com
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


def _is_rp_domain(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return False
    return host == RP_DOMAIN or host.endswith("." + RP_DOMAIN)


def _ddg_request(query: str) -> str:
    """Fait UNE requête DDG HTML et retourne le HTML brut."""
    ddg_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    req = urllib.request.Request(
        ddg_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://duckduckgo.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", errors="ignore")


def _search_duckduckgo(name: str, city: str) -> str | None:
    """Recherche DDG pour trouver l'URL RP. Retry une fois sur rate limit."""
    import time
    query = f"{name} {city} resortpass"
    rp_pattern = re.compile(
        r"https?://(?:www\.)?resortpass\.com/hotels/[a-z0-9\-]+",
        re.IGNORECASE,
    )
    uddg_pattern = re.compile(r"uddg=([^&\"']+)")

    for attempt in range(2):
        try:
            html = _ddg_request(query)
        except Exception:
            html = ""

        for match in uddg_pattern.findall(html):
            decoded = urllib.parse.unquote(match)
            m = rp_pattern.search(decoded)
            if m:
                # Nettoie : retire les éventuels query params / fragments
                url = m.group(0)
                return url

        direct = rp_pattern.search(html)
        if direct:
            return direct.group(0)

        if attempt == 0:
            time.sleep(3)

    return None


def _ask_gemini(name: str, city: str, country: str) -> dict | None:
    """Demande à Gemini l'URL RP. Fallback si DDG vide."""
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
    except Exception:
        return None

    url = (data.get("url") or "").strip()
    if not url or url == "UNKNOWN" or not url.startswith("https://"):
        return None
    if not _is_rp_domain(url):
        return None
    return {"url": url, "confidence": data.get("confidence", "medium")}


def find_rp_url(name: str, city: str | None = None, country: str | None = None) -> dict | None:
    """Trouve l'URL ResortPass de l'hôtel (cascade DDG → Gemini).

    Returns:
        {"url": str, "source": "duckduckgo" | "gemini", "confidence": str}
        OU {"url": None, "error": "..."} si rien trouvé.
    """
    if not name:
        return None

    ddg_url = _search_duckduckgo(name, city or "")
    if ddg_url and _is_rp_domain(ddg_url):
        return {"url": ddg_url, "source": "duckduckgo", "confidence": "high"}

    suggestion = _ask_gemini(name, city or "", country or "")
    if not suggestion:
        return {
            "url": None,
            "source": "duckduckgo+gemini",
            "error": "Aucune URL ResortPass trouvée",
        }

    return {
        "url": suggestion["url"],
        "source": "gemini",
        "confidence": suggestion.get("confidence", "medium"),
    }


def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python rp_finder.py 'Nom Hôtel' [Ville] [Pays]")
        sys.exit(1)
    name = sys.argv[1]
    city = sys.argv[2] if len(sys.argv) > 2 else ""
    country = sys.argv[3] if len(sys.argv) > 3 else ""
    print(json.dumps(find_rp_url(name, city, country), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
