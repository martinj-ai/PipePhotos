"""Scraper Expedia.com via Playwright + stealth.

Récupère les photos haute résolution depuis la galerie d'une fiche hôtel Expedia.
Pattern photo Expedia : `images.trvl-media.com/lodging/{NNNN}/{NNNN}/...{size}.jpg`
(CDN Trivago, propriétaire d'Expedia).

Usage CLI :
    python expedia_scraper.py https://www.expedia.com/Miami-Hotels-...h12345.Hotel-Information

Usage programmatique :
    from expedia_scraper import scrape_expedia_photos, download_photos_to_dir
    urls = scrape_expedia_photos(url)
    download_photos_to_dir(urls, dest_dir)
"""

from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import Stealth
    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# Patterns photos Expedia (CDN Trivago = trvl-media.com).
# On accepte plusieurs résolutions, on filtrera plus bas pour garder la max.
# Exemple URL : https://images.trvl-media.com/lodging/12000000/11650000/11647500/11647427/{hash}.jpg?impolicy=resizecrop&rw=1200&ra=fit
EXPEDIA_PHOTO_PATTERN = re.compile(
    r"https?://[a-z0-9.\-]*?trvl-media\.com/[^\s\"'<>]+\.(?:jpg|jpeg|png|webp)",
    re.IGNORECASE,
)


def scrape_expedia_photos(url: str, headless: bool = True, max_scrolls: int = 30) -> list[str]:
    """Récupère les URLs photos depuis une fiche hôtel Expedia.

    Args:
        url       : URL fiche hôtel Expedia (xxx.h{ID}.Hotel-Information)
        headless  : True en prod, False pour debug visuel (voir le browser tourner)
        max_scrolls : nombre de scrolls dans la galerie modale

    Returns:
        Liste d'URLs absolues photos, dédupliquées et triées par taille (max d'abord).
    """
    photo_urls: set[str] = set()

    def _launch_and_scrape(p):
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = context.new_page()

        # Capture toutes les réponses image du CDN Expedia/Trivago
        def on_response(r):
            if "trvl-media.com" in r.url and re.search(r"\.(jpg|jpeg|png|webp)", r.url, re.IGNORECASE):
                photo_urls.add(r.url)

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception as e:
            print(f"  [expedia] navigation échouée : {e}", file=sys.stderr)
            browser.close()
            return

        # Cookies / consent (Expedia OneTrust)
        for selector in ("#onetrust-accept-btn-handler", "button:has-text('Accept all')",
                         "button:has-text('Tout accepter')"):
            try:
                page.click(selector, timeout=2000)
                page.wait_for_timeout(800)
                break
            except Exception:
                continue

        page.wait_for_timeout(2000)

        # Ouvre la galerie : Expedia a généralement un bouton "View all photos" ou
        # un overlay clickable sur la photo principale.
        opened = False
        for selector in (
            "button:has-text('View all photos')",
            "button:has-text('All photos')",
            "button:has-text('Voir toutes les photos')",
            "button:has-text('Toutes les photos')",
            "[data-stid='property-gallery-photo-button']",
            "[data-stid='property-gallery-photo']",  # tile gallery
        ):
            try:
                el = page.query_selector(selector)
                if el:
                    el.scroll_into_view_if_needed()
                    page.wait_for_timeout(500)
                    el.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    opened = True
                    break
            except Exception:
                continue

        # Si pas de bouton, on tente quand même : la page hôtel charge déjà ~10-20 photos
        if not opened:
            print("  [expedia] bouton galerie non trouvé, on prend les photos de la page", file=sys.stderr)

        # Scroll dans la galerie pour lazy-loader toutes les photos
        for _ in range(max_scrolls):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(250)

        page.wait_for_timeout(1500)

        # Capture finale des src d'images dans le DOM (au cas où des photos
        # ne génèrent pas d'event réseau, ex: img déjà cachées)
        dom_imgs = page.eval_on_selector_all(
            "img",
            "imgs => imgs.map(i => i.src || i.getAttribute('data-src') || '').filter(s => s && s.includes('trvl-media.com'))",
        )
        for u in dom_imgs:
            photo_urls.add(u)

        browser.close()

    # Lance avec stealth si dispo (recommandé contre les bot detectors Expedia).
    # Stealth.use_sync() wraps sync_playwright() — on ne double-wrap pas.
    if _HAS_STEALTH:
        with Stealth().use_sync(sync_playwright()) as p:
            _launch_and_scrape(p)
    else:
        with sync_playwright() as p:
            _launch_and_scrape(p)

    # Dédup intelligente : Expedia produit des URLs variantes avec query params
    # (rw=200 vs rw=1200, impolicy, etc.) pour la MÊME image. On regroupe par
    # "path stem" (la partie significative avant les params) et on garde
    # l'URL avec la plus grande largeur.
    by_stem: dict[str, tuple[str, int]] = {}
    for u in photo_urls:
        if not EXPEDIA_PHOTO_PATTERN.search(u):
            continue
        # Filtre les avatars / icônes / sprites
        if any(k in u.lower() for k in ("avatar", "sprite", "icon", "logo")):
            continue
        # Extrait le path sans query (= identifiant unique de l'image)
        parsed = urllib.parse.urlparse(u)
        stem = parsed.path
        # Estimation de la largeur depuis le query param `rw` ou `ra`
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            width = int(qs.get("rw", ["0"])[0])
        except (ValueError, IndexError):
            width = 0
        # Si pas de rw, on regarde dans le path (parfois ".../1200_low/...")
        if width == 0:
            m = re.search(r"/(\d{3,4})_", u)
            if m:
                width = int(m.group(1))
        # Filtre thumbnails (< 600px)
        if width and width < 600:
            continue
        if stem not in by_stem or by_stem[stem][1] < width:
            by_stem[stem] = (u, width)

    # Tri stable par stem path (préserve un ordre cohérent)
    return [u for u, _ in sorted(by_stem.values(), key=lambda x: x[0])]


def download_photos_to_dir(urls: list[str], dest_dir: Path, max_photos: int | None = None) -> list[dict]:
    """Télécharge les photos dans dest_dir. Retourne {filename, size, status}.

    Filename : `expedia_{i:03d}_{hash}.jpg` (hash = 8 derniers chars du basename).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if max_photos:
        urls = urls[:max_photos]

    results = []
    for i, url in enumerate(urls, 1):
        # Génère un identifiant court depuis l'URL (8 derniers chars du basename)
        parsed = urllib.parse.urlparse(url)
        basename = Path(parsed.path).stem  # sans extension
        photo_id = basename[-8:] if len(basename) >= 8 else basename or f"unknown_{i}"
        filename = f"expedia_{i:03d}_{photo_id}.jpg"
        dest = dest_dir / filename

        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            results.append({"filename": filename, "url": url, "size": len(data), "status": "ok"})
        except Exception as e:
            results.append({"filename": filename, "url": url, "size": 0, "status": f"error: {e}"})
    return results


# ============= CLI =============

def main():
    if len(sys.argv) < 2:
        print("Usage: python expedia_scraper.py <url_fiche_expedia>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"→ Scraping {url}")
    urls = scrape_expedia_photos(url)
    print(f"  {len(urls)} photos uniques trouvées")
    for u in urls[:5]:
        print(f"    {u[:120]}")


if __name__ == "__main__":
    main()
