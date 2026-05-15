"""Scraper Expedia.com via Patchright (Playwright undetected fork) + real Chrome.

Récupère les photos haute résolution depuis la galerie d'une fiche hôtel Expedia.

⚠️ BOT WALL BYPASS — résumé empirique (Martin, 12/05/2026)
=============================================================
Expedia utilise DataDome/PerimeterX. Tests :
- Playwright standard headless+stealth : 🛡️ bloqué (page « Bot or Not? »)
- Patchright headless+chromium : 🛡️ bloqué
- Patchright headless=False + channel='chrome' (real Chrome) : ✅ bypass

Configuration gagnante :
- `patchright` (fork undetected de Playwright)
- `channel='chrome'` (utilise le binaire Chrome système installé)
- `headless=False` (DataDome détecte le mode headless)
- `launch_persistent_context` (vrais profil + storage)

Conséquence : une fenêtre Chrome apparaît brièvement sur le bureau pendant
le scrape (~30s). Acceptable pour un outil local POC.

URL Expedia typique :
  https://www.expedia.com/Miami-Hotels-Aloft-Miami-Airport.h16223760.Hotel-Information

Pattern photos CDN Trivago/Expedia :
  https://images.trvl-media.com/lodging/{NNNN}/{NNNN}/...{hash}.jpg
"""

from __future__ import annotations

import re
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from patchright.sync_api import sync_playwright
    _HAS_PATCHRIGHT = True
except ImportError:
    _HAS_PATCHRIGHT = False
    # Fallback to vanilla playwright (will get blocked by Expedia, but at least
    # won't crash for unit tests / dev environments without patchright)
    from playwright.sync_api import sync_playwright


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

EXPEDIA_PHOTO_PATTERN = re.compile(
    r"https?://[a-z0-9.\-]*?trvl-media\.com/[^\s\"'<>]+\.(?:jpg|jpeg|png|webp)",
    re.IGNORECASE,
)


class ExpediaBotBlocked(Exception):
    """Levée quand Expedia détecte le scraper et affiche la page 'Bot or Not?'.

    Avec patchright + chrome + headless=False, cette exception ne devrait plus
    JAMAIS être levée. Si elle l'est, c'est qu'Expedia a renforcé son anti-bot
    et il faudrait passer à un proxy résidentiel ou ScraperAPI.
    """
    pass


def scrape_expedia_photos(url: str, headless: bool = False, max_arrow_presses: int = 50) -> list[str]:
    """Récupère les URLs photos depuis une fiche hôtel Expedia.

    Args:
        url       : URL fiche hôtel Expedia (xxx.h{ID}.Hotel-Information)
        headless  : par défaut FALSE car requis pour bypass DataDome (cf. docstring module).
        max_arrow_presses : nombre de pressions ArrowRight dans la galerie modale.
                           Chaque arrow charge 1 photo. 50 ≈ couvre la plupart des hôtels.

    Returns:
        Liste d'URLs absolues photos, dédupliquées et triées par path stem.

    Raises:
        ExpediaBotBlocked: si Expedia affiche sa page de bot detection.
    """
    photo_urls: set[str] = set()
    bot_blocked = {"flag": False}

    def _launch_and_scrape(p):
        # Patchright + persistent context + real Chrome = bypass DataDome.
        # Le user_data_dir est jetable (tempfile) → pas d'effet de bord.
        # IMPORTANT : on NE passe PAS de user_agent ni d'args custom — patchright
        # gère lui-même les fingerprints. Override → DataDome détecte mismatch
        # entre UA déclaré et navigator fingerprint → bot wall.
        user_data_dir = tempfile.mkdtemp(prefix="patchright_expedia_")
        kwargs = {
            "user_data_dir": user_data_dir,
            "headless": headless,
            "no_viewport": True,
        }
        if _HAS_PATCHRIGHT:
            kwargs["channel"] = "chrome"
        try:
            ctx = p.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            print(f"  [expedia] launch chrome channel échoué : {e}", file=sys.stderr)
            kwargs.pop("channel", None)
            ctx = p.chromium.launch_persistent_context(**kwargs)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Capture toutes les réponses image du CDN Expedia/Trivago
        def on_response(r):
            if "trvl-media.com" in r.url and re.search(r"\.(jpg|jpeg|png|webp)", r.url, re.IGNORECASE):
                photo_urls.add(r.url)

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"  [expedia] navigation échouée : {e}", file=sys.stderr)
            ctx.close()
            return

        page.wait_for_timeout(3500)

        # ━ Détection bot wall ━
        # Avec patchright+chrome+headless=False, ne devrait plus arriver.
        try:
            title = (page.title() or "").lower()
            if "bot or not" in title or "robot ou pas" in title:
                print(f"  [expedia] BOT WALL détectée (title='{page.title()}')", file=sys.stderr)
                bot_blocked["flag"] = True
                ctx.close()
                return
        except Exception:
            pass

        # Cookies / consent (Expedia OneTrust)
        for selector in ("#onetrust-accept-btn-handler", "button:has-text('Accept all')",
                         "button:has-text('Tout accepter')"):
            try:
                page.click(selector, timeout=1500)
                page.wait_for_timeout(600)
                break
            except Exception:
                continue

        # ━ Ouvre la galerie modale ━
        # Empiriquement (12/05/2026) le bouton trigger principal est
        # `button.uitk-image-link` (composant Expedia UITK). On essaye aussi
        # quelques sélecteurs historiques en fallback.
        opened = False
        for selector in (
            "button.uitk-image-link",  # ← le bon en 2026
            "[data-stid='property-gallery-photo-button']",
            "[data-stid='property-gallery-photo']",
            "button:has-text('View all photos')",
            "button:has-text('All photos')",
        ):
            try:
                el = page.query_selector(selector)
                if el:
                    el.scroll_into_view_if_needed()
                    page.wait_for_timeout(400)
                    el.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    opened = True
                    print(f"  [expedia] galerie ouverte via {selector}", file=sys.stderr)
                    break
            except Exception:
                continue

        if not opened:
            print("  [expedia] bouton galerie non trouvé — on prend les photos de la page", file=sys.stderr)

        # ━ Scroll DANS le modal galerie ━
        # Le modal Expedia (uitk-sheet-content) est un conteneur scrollable
        # avec 30-50 images. On scrolle JavaScript-side dans cet élément
        # spécifique (page.mouse.wheel scrolle juste la fenêtre, pas le modal).
        prev_count = -1
        stale = 0
        for i in range(30):
            try:
                page.evaluate("""() => {
                    const sheet = document.querySelector('.uitk-sheet-content');
                    if (sheet) {
                        sheet.scrollBy({top: 1500, behavior: 'auto'});
                    } else {
                        window.scrollBy(0, 1500);
                    }
                }""")
            except Exception:
                break
            page.wait_for_timeout(400)
            if len(photo_urls) == prev_count:
                stale += 1
                if stale >= 5:
                    break
            else:
                stale = 0
            prev_count = len(photo_urls)

        # ArrowRight aussi en fallback (certains modaux ne scrollent pas, ils
        # naviguent via clavier) — utile pour les hôtels avec layout slideshow.
        for _ in range(max_arrow_presses):
            try:
                page.keyboard.press("ArrowRight")
            except Exception:
                break
            page.wait_for_timeout(180)

        page.wait_for_timeout(1500)

        # ━ Scrape DOM final ━
        # Au cas où des photos sont dans le DOM mais pas captées via network
        # (préchargées dans srcset, sprite, etc.)
        try:
            dom_imgs = page.eval_on_selector_all(
                "img",
                "imgs => imgs.map(i => i.src || i.getAttribute('data-src') || '').filter(s => s && s.includes('trvl-media.com'))",
            )
            for u in dom_imgs:
                photo_urls.add(u)
        except Exception:
            pass

        # Tente d'élargir avec srcset (Expedia met souvent les HD là)
        try:
            srcsets = page.eval_on_selector_all(
                "img[srcset]",
                "imgs => imgs.flatMap(i => (i.getAttribute('srcset') || '').split(',').map(s => s.trim().split(' ')[0]))",
            )
            for u in srcsets:
                if "trvl-media.com" in u:
                    photo_urls.add(u)
        except Exception:
            pass

        ctx.close()

    with sync_playwright() as p:
        _launch_and_scrape(p)

    if bot_blocked["flag"]:
        raise ExpediaBotBlocked(
            "Expedia a détecté Playwright/Patchright et sert sa page 'Bot or Not?'. "
            "Avec patchright + chrome + headless=False ça ne devrait plus arriver — "
            "vérifie que Chrome est installé et que patchright est à jour."
        )

    # ━ Dédup intelligente ━
    # Expedia sert la même image en plusieurs résolutions (rw=200, rw=598, rw=1200…)
    # via query params. On regroupe par path et on garde la version la plus large.
    by_stem: dict[str, tuple[str, int]] = {}
    for u in photo_urls:
        if not EXPEDIA_PHOTO_PATTERN.search(u):
            continue
        if any(k in u.lower() for k in ("avatar", "sprite", "icon", "logo", "/badges/")):
            continue
        parsed = urllib.parse.urlparse(u)
        stem = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            width = int(qs.get("rw", ["0"])[0])
        except (ValueError, IndexError):
            width = 0
        if width == 0:
            m = re.search(r"/(\d{3,4})_", u)
            if m:
                width = int(m.group(1))
        # Filtre thumbnails (<400px = mini ou icône)
        if width and width < 400:
            continue
        if stem not in by_stem or by_stem[stem][1] < width:
            by_stem[stem] = (u, width)

    # ━ UPGRADE HD ━
    # Empiriquement (12/05/2026) : l'URL Expedia/Trivago SANS aucun query param
    # sert l'image originale en 3840x2560 (4K natif). Avec impolicy+rw=1200
    # on était plafonnés à 1200px alors que l'original est x3 plus large.
    # On strip donc TOUS les query params pour avoir l'original full-res.
    def _strip_to_original(u: str) -> str:
        parsed = urllib.parse.urlparse(u)
        return urllib.parse.urlunparse(parsed._replace(query="", fragment=""))

    return [_strip_to_original(u) for u, _ in sorted(by_stem.values(), key=lambda x: x[0])]


def download_photos_to_dir(urls: list[str], dest_dir: Path, max_photos: int | None = None) -> list[dict]:
    """Télécharge les photos dans dest_dir. Retourne {filename, size, status}.

    Les URLs reçues sont déjà nettoyées de leurs query params par scrape_expedia_photos()
    → on télécharge directement l'original 3840x2560.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if max_photos:
        urls = urls[:max_photos]

    results = []
    for i, url in enumerate(urls, 1):
        hd_url = url  # déjà strippée par scrape_expedia_photos()
        parsed = urllib.parse.urlparse(url)
        basename = Path(parsed.path).stem
        photo_id = basename[-8:] if len(basename) >= 8 else basename or f"unknown_{i}"
        filename = f"expedia_{i:03d}_{photo_id}.jpg"
        dest = dest_dir / filename
        try:
            req = urllib.request.Request(hd_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            results.append({"filename": filename, "url": hd_url, "size": len(data), "status": "ok"})
        except Exception as e:
            results.append({"filename": filename, "url": hd_url, "size": 0, "status": f"error: {e}"})
    return results


# ============= CLI =============

def main():
    if len(sys.argv) < 2:
        print("Usage: python expedia_scraper.py <url_fiche_expedia>")
        sys.exit(1)
    url = sys.argv[1]
    print(f"→ Scraping {url}")
    urls = scrape_expedia_photos(url, headless=False)
    print(f"  {len(urls)} photos uniques trouvées")
    for u in urls[:5]:
        print(f"    {u[:140]}")


if __name__ == "__main__":
    main()
