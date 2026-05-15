"""Scraper Booking.com via Playwright headless.

Bypasse les protections JS de Booking. Récupère les photos haute résolution depuis
la galerie modale (pattern `cf.bstatic.com/xdata/images/hotel/max1024x768/`).

Usage CLI :
    python booking_scraper.py https://www.booking.com/hotel/us/cabana-miami-beach.en-gb.html

Usage programmatique :
    from booking_scraper import scrape_booking_photos, download_photos_to_dir
    urls = scrape_booking_photos(url)  # → list[str]
    download_photos_to_dir(urls, dest_dir)
"""

from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# On accepte les patterns de la modale galerie : `hotel/maxNNN/{ID}.jpg`
PHOTO_PATTERN = re.compile(r"cf\.bstatic\.com/xdata/images/hotel/max(\d+)(?:x\d+)?/(\d+)\.")

# Taille cible HD : Booking sert toutes les tailles avec la MÊME signature `k=`,
# donc on peut remplacer `max1024x768` → `max3000` sans casser l'auth.
# Empiriquement (12/05/2026) : max3000 → 3000x2000 (taille originale).
# max2048 = 2048x1365, max1600 = 1600x1067 — disponibles si besoin downgrade.
BOOKING_TARGET_SIZE = "max3000"


def _clean_url(url: str) -> str:
    """Nettoie l'URL Booking : on garde juste le path de la fiche hôtel et on ajoute nos
    propres query params (dates).

    Indispensable car Booking reçoit souvent des URLs avec aid=, label=, activeTab=photosGallery,
    sid=, etc. qui changent le comportement de la page (galerie déjà ouverte, mode différent...).
    On jette tout et on reconstruit propre.
    """
    parsed = urllib.parse.urlparse(url)
    # On garde scheme + netloc + path. Pas de query, pas de fragment.
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    # Forcer le suffix .html si l'URL inclut un suffix de langue (.en-gb.html, .fr.html...)
    # Le path est déjà bon dans la majorité des cas.
    return f"{base}?checkin=2026-06-15&checkout=2026-06-16&group_adults=2"


def scrape_booking_photos(url: str, headless: bool = True, max_scrolls: int = 30) -> list[str]:
    """Récupère les URLs de photos haute résolution d'une fiche Booking.

    Args:
        url       : URL fiche hôtel Booking (avec ou sans dates)
        headless  : True en prod, False pour debug visuel
        max_scrolls : nombre de scrolls dans la galerie modale

    Returns:
        Liste d'URLs absolues de photos, dédupliquées par photo ID, meilleure résolution.
    """
    target_url = _clean_url(url)
    photo_urls: set[str] = set()

    with sync_playwright() as p:
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

        # Capture des images en passant via réseau aussi (pour les lazy-loaded)
        def on_response(r):
            if "cf.bstatic.com" in r.url and "/hotel/max" in r.url:
                photo_urls.add(r.url)

        page.on("response", on_response)
        page.goto(target_url, wait_until="networkidle", timeout=30000)

        # Cookies
        try:
            page.click("#onetrust-accept-btn-handler", timeout=2500)
            page.wait_for_timeout(1000)
        except Exception:
            pass

        page.wait_for_timeout(2000)

        # Trouve et clique sur le bouton "+N photos"
        box = page.evaluate("""() => {
            const els = document.querySelectorAll('button, a, div, span');
            for (const el of els) {
                const t = (el.innerText || '').trim();
                if (/^\\+?\\s*\\d+\\s*photos?$/i.test(t) && t.length < 30) {
                    el.scrollIntoView({block: 'center'});
                    const r = el.getBoundingClientRect();
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2, text: t};
                }
            }
            return null;
        }""")

        if box:
            page.mouse.click(box["x"], box["y"])
            page.wait_for_timeout(3000)

        # Scroll vertical dans la modale pour lazy-load toutes les photos
        for _ in range(max_scrolls):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(220)

        page.wait_for_timeout(1500)

        # Capture finale du DOM (au cas où certaines images n'ont pas généré d'event réseau)
        dom_imgs = page.eval_on_selector_all(
            "img",
            "imgs => imgs.map(i => i.src || i.getAttribute('data-src') || '').filter(s => s && s.includes('cf.bstatic.com') && s.includes('/hotel/'))",
        )
        for u in dom_imgs:
            photo_urls.add(u)

        browser.close()

    # Dédup par photo ID, garde la plus haute résolution
    by_id: dict[str, tuple[str, int]] = {}
    for u in photo_urls:
        m = PHOTO_PATTERN.search(u)
        if not m:
            continue
        size = int(m.group(1))
        photo_id = m.group(2)
        # Filtre les thumbnails (max300 et inférieur)
        if size < 600:
            continue
        if photo_id not in by_id or by_id[photo_id][1] < size:
            by_id[photo_id] = (u, size)

    # ━ UPGRADE HD ━
    # Booking expose `maxNNN` dans le path et la signature `k=` est universelle
    # (même HMAC pour toutes les tailles). On remplace systématiquement le
    # segment maxNNN par BOOKING_TARGET_SIZE pour télécharger en pleine résolution.
    # Sans ça on téléchargerait du 1024 alors que les originaux font 3000x2000.
    def _upgrade(u: str) -> str:
        return re.sub(r"max\d+(?:x\d+)?", BOOKING_TARGET_SIZE, u, count=1)

    # Tri stable par photo ID (préserve un ordre cohérent)
    return [_upgrade(u) for u, _ in sorted(by_id.values(), key=lambda x: x[0])]


def download_photos_to_dir(urls: list[str], dest_dir: Path, max_photos: int | None = None) -> list[dict]:
    """Télécharge les photos dans dest_dir. Retourne {filename, size, status}."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if max_photos:
        urls = urls[:max_photos]

    results = []
    for i, url in enumerate(urls, 1):
        m = PHOTO_PATTERN.search(url)
        photo_id = m.group(2) if m else f"unknown_{i}"
        filename = f"booking_{i:03d}_{photo_id}.jpg"
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
        print("Usage: python booking_scraper.py <url_fiche_booking>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"→ Scraping {url}")
    urls = scrape_booking_photos(url)
    print(f"  {len(urls)} photos uniques trouvées")
    for u in urls[:5]:
        m = PHOTO_PATTERN.search(u)
        if m:
            print(f"    [max{m.group(1)}] id={m.group(2)}")


if __name__ == "__main__":
    main()
