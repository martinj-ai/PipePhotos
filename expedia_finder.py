"""Expedia finder — trouve l'URL de la fiche hôtel sur expedia.com.

Stratégie :
  1. Recherche DuckDuckGo HTML (gratuit, sans API key) → première URL Expedia
     dans les résultats. Fiable car DDG indexe les pages Expedia avec leurs IDs
     réels (testé : Moxy Miami South Beach → h55553829 récupéré direct).
  2. Fallback Gemini si DDG ne retourne rien (cas hôtel très obscur).

URL Expedia typique :
  https://www.expedia.com/Miami-Hotels-Moxy-Miami-South-Beach.h55553829.Hotel-Information
  https://www.expedia.fr/Paris-Hotels-Hotel-de-Crillon.h12345.Informations-Hotel
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


def _looks_hallucinated(url: str) -> bool:
    """Détecte les IDs Expedia probablement hallucinés par Gemini.

    Patterns observés empiriquement quand Gemini invente un ID :
      - h67900000, h65900000, h12345678 (placeholders ronds avec beaucoup de zéros)
      - h11111111 (chiffres identiques)
      - h12345678 (séquence)

    Les vrais IDs Expedia (h55553829, h18107, h16223760) ont une distribution
    de chiffres "aléatoire" sans pattern évident.
    """
    m = re.search(r"\.h(\d+)\.", url)
    if not m:
        return False
    digits = m.group(1)
    # 4+ zéros consécutifs en fin → suspect (Gemini ajoute des 0 pour padding)
    if re.search(r"0{4,}$", digits):
        return True
    # 4+ zéros consécutifs au milieu → suspect (h12000000, h67900000…)
    if re.search(r"0{4,}", digits):
        return True
    # Chiffres tous identiques ou très peu de variété
    if len(set(digits)) <= 2:
        return True
    return False


def _ddg_request(query: str) -> str:
    """Fait UNE requête DDG HTML et retourne le HTML brut. Lève sur erreur."""
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
    """Recherche DuckDuckGo HTML pour trouver l'URL Expedia.

    Si la 1ère requête retourne 0 uddg (souvent un rate limit transient sur l'IP
    qui vient de faire plusieurs DDG calls), on attend 3s et retry une fois.

    Returns:
        L'URL Expedia trouvée (la première match), ou None si rien après retry.
    """
    import time
    query = f"{name} {city} expedia hotel-information"
    expedia_pattern = re.compile(
        r"https?://(?:www\.)?expedia\.[a-z.]+/[^\s\"'<>]+?\.h\d+\.(?:Hotel-Information|Informations-Hotel|Hotel-Informationen|Informacion-Hotel|Informazioni-Hotel)",
        re.IGNORECASE,
    )
    uddg_pattern = re.compile(r"uddg=([^&\"']+)")

    for attempt in range(2):  # 1 essai + 1 retry après 3s
        try:
            html = _ddg_request(query)
        except Exception:
            html = ""

        # Extrait depuis les redirects uddg=
        for match in uddg_pattern.findall(html):
            decoded = urllib.parse.unquote(match)
            m = expedia_pattern.search(decoded)
            if m:
                return m.group(0)

        # Fallback : cherche directement (au cas où DDG affiche les URLs nues)
        direct = expedia_pattern.search(html)
        if direct:
            return direct.group(0)

        # 0 résultat → rate limit probable → wait + retry une fois
        if attempt == 0:
            time.sleep(3)

    return None


def find_expedia_url(name: str, city: str | None = None, country: str | None = None) -> dict | None:
    """Trouve l'URL Expedia de l'hôtel.

    Stratégie en cascade :
      1. DuckDuckGo HTML search (gratuit, fiable car DDG indexe les vrais IDs)
      2. Gemini en fallback si DDG ne retourne rien (hôtel très obscur)

    Returns:
        {
            "url": str,
            "source": "duckduckgo" | "gemini",
            "confidence": "high|medium|low",
        }
        OU {"url": None, "error": "..."} si rien trouvé.
    """
    if not name:
        return None

    # ━ Étape 1 : DuckDuckGo (priorité) ━
    ddg_url = _search_duckduckgo(name, city or "")
    if ddg_url and _is_expedia_domain(ddg_url):
        return {
            "url": ddg_url,
            "source": "duckduckgo",
            "confidence": "high",
        }

    # ━ Étape 2 : Gemini fallback (hôtel non indexé par DDG) ━
    suggestion = _ask_gemini(name, city or "", country or "")
    if not suggestion:
        return {
            "url": None,
            "source": "duckduckgo+gemini",
            "error": "Aucune URL Expedia trouvée (ni via DuckDuckGo ni via Gemini)",
        }

    # ━ Garde anti-hallucination : si l'ID Gemini paraît inventé (h67900000,
    # h12345678, h11111111…), on préfère retourner UNKNOWN plutôt qu'une URL
    # qui va donner 404 / 0 photos. Évite les faux positifs côté UI.
    if _looks_hallucinated(suggestion["url"]):
        return {
            "url": None,
            "source": "gemini",
            "error": (
                f"URL Gemini suspecte (ID hallucinable) : {suggestion['url']}. "
                "DuckDuckGo n'a pas pu confirmer l'URL réelle (rate-limited ?)."
            ),
            "url_attempted": suggestion["url"],
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
