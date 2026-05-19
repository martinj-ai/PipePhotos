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
from playwright_stealth import Stealth


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


def booking_url_to_slug(url: str) -> str:
    """Extrait un slug filesystem-safe depuis une URL Booking.

    Exemples :
        https://www.booking.com/hotel/us/the-sagamore.fr.html?aid=...&gclid=Cj...
          → "the-sagamore"
        https://www.booking.com/hotel/us/the-gates-hotel-south-beach.html
          → "the-gates-hotel-south-beach"
        https://www.booking.com/hotel/fr/leeu-collection.en-gb.html
          → "leeu-collection"

    Sans nettoyage, l'ancien code utilisait `parsed_path[-1].replace(".html", "")`
    qui laissait les query params dans le slug → erreur OSError "File name too long"
    sur le filesystem ext4 (limite 255 chars), bug Sagamore Hotel Martin 19/05/2026.
    """
    parsed = urllib.parse.urlparse(url)
    # On parse uniquement le path : strip query, fragment, etc.
    parts = [p for p in parsed.path.rstrip("/").split("/") if p]
    last = parts[-1] if parts else "hotel"
    # Strip .html + suffix langue éventuels (.fr, .en-gb, .es, .de, ...)
    last = last.replace(".html", "")
    # Strip les codes langue : .fr / .en / .es / .de / .it / .nl / .pt / .ru / .ja /
    # .zh / .ko / .ar / .he / .pl / .cs / .uk / .tr + variants régionaux (.en-gb, .pt-br, etc.)
    last = re.sub(r"\.(fr|en|es|de|it|nl|pt|ru|ja|zh|ko|ar|he|pl|cs|uk|tr|sv|no|da|fi|el)(-[a-z]{2})?$", "", last)
    # Sanitize : keep alphanumeric + - _
    last = re.sub(r"[^a-zA-Z0-9_-]", "-", last)
    # Collapse multiple dashes + trim
    last = re.sub(r"-+", "-", last).strip("-")
    # Cap à 80 chars (large marge vs 255 ext4)
    last = last[:80] if last else "hotel"
    return last.lower()


def scrape_booking_photos(url: str, headless: bool = True, max_scrolls: int = 30) -> list[str]:
    """Récupère les URLs de photos haute résolution d'une fiche Booking.

    Args:
        url       : URL fiche hôtel Booking (avec ou sans dates)
        headless  : True en prod, False pour debug visuel
        max_scrolls : nombre de scrolls dans la galerie modale

    Returns:
        Liste d'URLs absolues de photos, dédupliquées par photo ID, meilleure résolution.
    """
    # Normalise l'URL : strip les suffixes langue (.fr, .en-gb, etc.) pour avoir
    # la version EN canonique. Booking redirige automatiquement selon Accept-Language
    # si besoin, et la version sans suffixe a un DOM plus prévisible.
    url = re.sub(r"\.(fr|en|es|de|it|nl|pt|ru|ja|zh|ko|ar|he|pl|cs|uk|tr|sv|no|da|fi|el)(-[a-z]{2})?\.html",
                 ".html", url, count=1)
    target_url = _clean_url(url)
    photo_urls: set[str] = set()
    debug = lambda msg: print(f"[booking_scraper] {msg}", file=sys.stderr, flush=True)

    from playwright_helpers import chromium_launch_args
    # Stealth wrapper (Martin 19/05/2026, fix DataDome bug Sagamore prod) :
    # Booking utilise DataDome qui détecte Chromium headless via les flags
    # navigator.webdriver, navigator.plugins, WebGL fingerprint, etc.
    # playwright_stealth patche tout ça automatiquement → bypass DataDome de
    # base. Mêmes use_sync que les autres scrapers du pipe (hotel_site_finder,
    # booking_amenities_extractor, etc.).
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=headless, args=chromium_launch_args())
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
        debug(f"goto {target_url}")
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            debug(f"goto error : {e}")

        # ━ ATTENTE JS RENDER (Martin 19/05/2026, fix Sagamore) ━━━━━━━━━━━━━━━
        # Booking renvoie une page initiale quasi-vide (challenge DataDome qui
        # pose chal_t=... + force_referer=...) puis le JS render le contenu en
        # ~3-5s. Si on check title()/content() trop tôt, on a `title=''` et le
        # bouton "+N photos" n'est pas encore dans le DOM.
        # Solution : attendre le selector body qui contient les photos OU
        # timeout 6s avec fallback.
        try:
            # Attend que la galerie principale render — selectors courants Booking
            page.wait_for_selector(
                'a[data-testid*="gallery"], button[data-testid*="photos"], '
                'a[href*="#tab-photos"], div[id*="photo"], img[src*="bstatic.com"]',
                timeout=8000,
            )
            debug("gallery DOM ready")
        except Exception:
            debug("gallery DOM not detected after 8s, fallback timeout")
        page.wait_for_timeout(2000)  # cushion supplémentaire pour finir le render

        title = (page.title() or "")[:80]
        debug(f"page loaded · title='{title}' · URL={page.url[:120]}")

        # Détection blocage anti-bot stricte (DataDome challenge page sans contenu)
        if not title or len(page.content() or "") < 50000:
            debug(f"⚠️ Page suspecte (title='{title[:40]}', html_len={len(page.content() or '')}) — challenge ?")

        # Cookies (multi-language) — APRÈS le wait JS render (sinon les buttons ne sont pas là)
        for sel in ("#onetrust-accept-btn-handler", "button[aria-label*='Accept']",
                    "button[aria-label*='Accepter']", "button:has-text('Accepter')",
                    "button:has-text('Accept all')"):
            try:
                page.click(sel, timeout=1500)
                debug(f"cookies accepted via {sel}")
                page.wait_for_timeout(800)
                break
            except Exception:
                pass

        # ━ Stratégie 1 : scroll la PAGE PRINCIPALE pour déclencher le lazy-load
        # des thumbnails (Booking lazy-load les photos hôtel à mesure qu'on scroll).
        debug("scrolling main page to trigger lazy-load thumbnails")
        for i in range(15):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(180)
        page.wait_for_timeout(1500)
        debug(f"after page scroll : {len(photo_urls)} photo URLs captured")

        # ━ Stratégie 2 : clic sur le bouton "+N photos" (galerie modale)
        # Élargissement du regex pour matcher : "+98 photos", "98 photos", "+98 photo",
        # "Voir les 98 photos", "Toutes les photos", "View all photos", "98 fotos", etc.
        box = page.evaluate(r"""() => {
            const els = document.querySelectorAll('button, a, div, span');
            // Patterns de buttons photos (FR/EN/ES typiques sur Booking)
            const patterns = [
                /^\+?\s*\d+\s*photos?$/i,
                /^\+?\s*\d+\s*fotos?$/i,
                /voir.*\d+.*photos?/i,
                /(view|see).*\d+.*photos?/i,
                /ver.*\d+.*fotos?/i,
                /toutes? les photos/i,
                /all photos/i,
                /todas las fotos/i,
            ];
            for (const el of els) {
                const t = (el.innerText || '').trim();
                if (!t || t.length > 80) continue;
                if (patterns.some(p => p.test(t))) {
                    el.scrollIntoView({block: 'center'});
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        return {x: r.x + r.width / 2, y: r.y + r.height / 2, text: t};
                    }
                }
            }
            return null;
        }""")

        if box:
            debug(f"found photos button : '{box['text'][:50]}' → clicking")
            try:
                page.mouse.click(box["x"], box["y"])
                page.wait_for_timeout(3000)
            except Exception as e:
                debug(f"click error : {e}")

            # Scroll vertical dans la modale pour lazy-load toutes les photos
            debug(f"scrolling modal ({max_scrolls} times)")
            for _ in range(max_scrolls):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(220)
            page.wait_for_timeout(1500)
            debug(f"after modal scroll : {len(photo_urls)} photo URLs captured")
        else:
            debug("⚠️ NO photos button found — falling back on main page scan only")

        # Capture finale du DOM (au cas où certaines images n'ont pas généré d'event réseau)
        try:
            dom_imgs = page.eval_on_selector_all(
                "img",
                "imgs => imgs.map(i => i.src || i.getAttribute('data-src') || '').filter(s => s && s.includes('cf.bstatic.com') && s.includes('/hotel/'))",
            )
            for u in dom_imgs:
                photo_urls.add(u)
            debug(f"DOM scan : +{len(dom_imgs)} candidates · total URLs : {len(photo_urls)}")
        except Exception as e:
            debug(f"DOM scan error : {e}")

        browser.close()
    debug(f"final : {len(photo_urls)} raw URLs before dedup/filter")

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
