"""ResortPass scraper — extrait les données structurées d'une fiche hôtel RP.

Source : Next.js SSR, données dans <script id="__NEXT_DATA__">{...}</script>.

Usage :
    python rp_scraper.py https://www.resortpass.com/hotels/hilton-cabana-miami-beach
    python rp_scraper.py https://www.resortpass.com/hotels/xxx --out data/rp/xxx.json

Sortie : un JSON propre avec amenities, vibes, personas autorisés, photos.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = ROOT / "config" / "vibes_personas.json"
DEFAULT_OUT_DIR = ROOT / "data" / "rp"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Mapping RP amenities → catégories de photos qu'on cherche.
# Plusieurs entrées RP peuvent mapper vers la même catégorie (ex: pool + outdoor-pool).
AMENITY_MAP = {
    "pool": "pool",
    "outdoor-pool": "pool",
    "indoor-pool": "pool_indoor",
    "cabana": "cabana",
    "food": "food",
    "drink": "bar",
    "spa": "spa",       # vrai salon spa uniquement
    "sauna": "spa",
    "hammam": "spa",
    "hottub": "hottub", # hot tub = catégorie distincte, pas une photo de "salon spa" attendue
    "beach": "beach",
    "rooftop": "rooftop",
    "gym": "gym",
    "fitness": "gym",
}


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def extract_next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        raise RuntimeError("__NEXT_DATA__ introuvable. RP a peut-être changé son layout.")
    return json.loads(m.group(1))


def extract_image_urls(images: list[dict]) -> list[str]:
    """RP fournit chaque image en plusieurs résolutions. On garde l'URL principale."""
    out = []
    for img in images:
        pic = img.get("picture") or {}
        url = pic.get("url")
        if url:
            out.append(url)
    return out


def normalize_amenities(rp_amenities: list[dict]) -> dict[str, bool]:
    """Mappe les amenities RP vers nos catégories normalisées."""
    raw_names = {a.get("name") for a in rp_amenities if isinstance(a, dict)}
    normalized = {}
    for rp_name, our_cat in AMENITY_MAP.items():
        if rp_name in raw_names:
            normalized[our_cat] = True
    # On veut explicitement les False aussi pour la "shopping list"
    for cat in set(AMENITY_MAP.values()):
        normalized.setdefault(cat, False)
    return normalized


def _normalize_vibe(vibe: str) -> str:
    """Family-Friendly == family friendly == Family Friendly."""
    return vibe.lower().replace("-", " ").replace("_", " ").strip()


def derive_personas(vibe: str | None, config_path: Path = CONFIG) -> list[str]:
    """Lit la config personas et retourne la liste pour la vibe donnée (matching robuste)."""
    with open(config_path) as f:
        cfg = json.load(f)
    # construit une map normalisée
    normalized = {_normalize_vibe(k): v for k, v in cfg.items() if not k.startswith("_")}
    if vibe and _normalize_vibe(vibe) in normalized:
        return normalized[_normalize_vibe(vibe)]
    return cfg.get("_default", ["couples", "solos"])


def scrape(url: str) -> dict:
    html = fetch_html(url)
    data = extract_next_data(html)
    hd = data.get("props", {}).get("pageProps", {}).get("hotelDetails")
    if not hd:
        raise RuntimeError("hotelDetails manquant dans le JSON. Mauvaise URL ?")

    rp_amenities = hd.get("amenities", [])
    vibe_primary = (hd.get("vibes") or {}).get("primary")
    images = hd.get("image", [])

    return {
        "rp_id": hd.get("id"),
        "rp_url": url,
        "name": hd.get("name"),
        "city": hd.get("city_name"),
        "country": hd.get("country_name"),
        "state": hd.get("state_name"),
        "star_classification": hd.get("star_classification"),
        "avg_rating": hd.get("avg_rating"),
        "vibe_primary": vibe_primary,
        "tags": [t.get("name") for t in (hd.get("tags") or []) if isinstance(t, dict)],
        "amenities_raw": [a.get("name") for a in rp_amenities if isinstance(a, dict)],
        "amenities_normalized": normalize_amenities(rp_amenities),
        "personas_allowed": derive_personas(vibe_primary),
        "image_count": len(images),
        "image_urls": extract_image_urls(images),
    }


def download_photos(image_urls: list[str], dest_dir: Path, max_photos: int | None = None) -> list[dict]:
    """Télécharge les photos RP dans dest_dir. Retourne la liste {filename, url, size, status}."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if max_photos:
        image_urls = image_urls[:max_photos]

    results = []
    for i, url in enumerate(image_urls, 1):
        # Génère un nom de fichier propre : on garde le hash + slug court
        # URL type : .../uploads/image/picture/2021/cabananewnew.jpg
        original_name = url.rstrip("/").split("/")[-1]
        # Préfixe avec un index pour ordre déterministe
        ext = original_name.split(".")[-1].lower() if "." in original_name else "jpg"
        filename = f"rp_{i:02d}_{original_name}"
        dest = dest_dir / filename

        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            results.append({
                "filename": filename,
                "url": url,
                "size": len(data),
                "status": "ok",
            })
        except Exception as e:
            results.append({
                "filename": filename,
                "url": url,
                "size": 0,
                "status": f"error: {e}",
            })
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="URL fiche hôtel RP (ex: https://www.resortpass.com/hotels/...)")
    parser.add_argument("--out", help="Chemin JSON de sortie (par défaut data/rp/{slug}.json)")
    args = parser.parse_args()

    print(f"→ {args.url}")
    result = scrape(args.url)

    # Output
    if args.out:
        out_path = Path(args.out)
    else:
        slug = args.url.rstrip("/").split("/")[-1]
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DEFAULT_OUT_DIR / f"{slug}.json"

    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # Récap console
    print(f"  {result['name']} ({result['star_classification']}★, {result['city']})")
    print(f"  Vibe: {result['vibe_primary']} → personas autorisés : {result['personas_allowed']}")
    actives = [k for k, v in result["amenities_normalized"].items() if v]
    print(f"  Amenities détectées : {', '.join(actives)}")
    print(f"  Photos : {result['image_count']}")
    print(f"→ {out_path}")


if __name__ == "__main__":
    main()
