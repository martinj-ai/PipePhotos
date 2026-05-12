"""Expedia finder — demande à Gemini l'URL de la page hôtel sur expedia.com.

Pattern identique à hotel_site_finder.py mais cible spécifiquement Expedia.
Permet d'enrichir le pipeline avec une 5e source photos (en complément de
official/booking/rp/instagram).

URL Expedia typique :
  https://www.expedia.com/Miami-Hotels-Moxy-Miami-South-Beach.h12572584.Hotel-Information
  https://www.expedia.fr/Paris-Hotels-Hotel-de-Crillon.h12345.Informations-Hotel
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

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# Domaines Expedia connus (variantes par pays). On valide que l'URL retournée
# par Gemini est bien sur l'un d'eux pour éviter les hallucinations.
EXPEDIA_DOMAINS = {
    "expedia.com", "expedia.fr", "expedia.co.uk", "expedia.de",
    "expedia.es", "expedia.it", "expedia.ca", "expedia.com.au",
    "expedia.nl", "expedia.be", "expedia.ch", "expedia.at",
    "expedia.com.br", "expedia.com.mx", "expedia.co.jp", "expedia.com.hk",
    "expedia.com.sg",
}

PROMPT = """Quel est l'URL de la fiche hôtel sur Expedia.com pour l'hôtel suivant ?

Hôtel : {name}
Ville : {city}
Pays : {country}

Règles strictes :
- Retourne UNIQUEMENT un objet JSON avec cette structure : {{"url": "https://...", "confidence": "high|medium|low"}}
- L'URL doit pointer vers la fiche HÔTEL sur expedia.com (ou version locale .fr / .co.uk / .de / .es / .it / .ca / .com.au / .com.br / .co.jp …).
- Format canonique : https://www.expedia.com/{{Ville}}-Hotels-{{Nom-Hotel-Tirets}}.h{{ID}}.Hotel-Information
  où {{ID}} est l'identifiant numérique Expedia interne (8 chiffres environ, ex: h55553829).
  Attention : la partie {{Ville}} est généralement la ville PRINCIPALE (ex: "Miami-Hotels" même pour un hôtel à Miami Beach).
- L'URL doit commencer par https:// et le domaine doit être un domaine Expedia officiel.
- Si tu n'es pas certain de l'ID Expedia (ne JAMAIS inventer un ID au hasard), retourne {{"url": "UNKNOWN", "confidence": "low"}}.
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
    """Demande à Gemini l'URL Expedia. Pattern strict miroir de hotel_site_finder."""
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
    if not _is_expedia_domain(url):
        return None
    return {"url": url, "confidence": data.get("confidence", "medium")}


def _is_expedia_domain(url: str) -> bool:
    """True si l'URL est bien sur un domaine Expedia officiel."""
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in EXPEDIA_DOMAINS)


def find_expedia_url(name: str, city: str | None = None, country: str | None = None) -> dict | None:
    """Trouve l'URL Expedia de l'hôtel.

    Returns:
        {
            "url": str,
            "source": "gemini",
            "confidence": "high|medium|low",
        }
        OU {"url": None, "error": "..."} si rien trouvé.
    """
    if not name:
        return None

    suggestion = _ask_gemini(name, city or "", country or "")
    if not suggestion:
        return {
            "url": None,
            "source": "gemini",
            "error": "Gemini n'a pas trouvé d'URL Expedia fiable pour cet hôtel",
        }

    return {
        "url": suggestion["url"],
        "source": "gemini",
        "confidence": suggestion.get("confidence", "medium"),
    }


# ============= CLI debug =============

def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python expedia_finder.py 'Nom Hôtel' [Ville] [Pays]")
        sys.exit(1)
    name = sys.argv[1]
    city = sys.argv[2] if len(sys.argv) > 2 else ""
    country = sys.argv[3] if len(sys.argv) > 3 else ""
    result = find_expedia_url(name, city, country)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
