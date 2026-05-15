"""Extracteur de photos depuis le site officiel d'un hôtel.

Stratégie en cascade :
  1. Tester patterns URL classiques (/gallery, /photos, /galerie, /explore...)
  2. Si rien : crawler la home et suivre les liens contenant "gallery|photos|galerie"
  3. Sur chaque page candidate : Playwright + scroll + extraction des <img>
  4. Filtrer les pictos / icônes / logos

Usage :
    from hotel_gallery_extractor import extract_gallery_photos
    photos = extract_gallery_photos("https://www.hotelcabane.com/")
"""

from __future__ import annotations

import re
import sys
import tempfile
from urllib.parse import urlparse, urljoin
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# Patchright (undetected Playwright) pour bypass anti-bot des chaînes hôtelières.
# Marriott / Hilton / Hyatt / IHG bloquent Playwright stealth en headless avec
# « Access Denied » (Akamai/Imperva). Patchright + real Chrome + headless=False
# passe ces protections. Fallback automatique si on détecte le blocage.
try:
    from patchright.sync_api import sync_playwright as patchright_sync
    _HAS_PATCHRIGHT = True
except ImportError:
    _HAS_PATCHRIGHT = False
    patchright_sync = None

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

# Patterns d'URLs courants pour les pages galerie/photos + sections d'amenities
GALLERY_PATH_PATTERNS = [
    "/gallery", "/photos", "/galerie", "/explore", "/spaces",
    "/the-resort", "/the-hotel", "/our-resort", "/our-hotel",
    "/photo-gallery", "/visual-tour", "/discover",
    "/gallery/", "/photos/", "/galerie/",
    # Pages amenities qui ont souvent plein de photos
    "/rooms", "/chambres", "/suites",
    "/dining", "/restaurant", "/restaurants", "/bar",
    "/pool", "/piscine", "/cabanas",
    "/spa", "/wellness",
    "/about", "/about-us", "/the-property",
]

# Mots-clés dans les liens / textes pour découvrir des pages galerie / amenities
GALLERY_KEYWORDS = (
    "gallery", "galerie", "photos", "photo-gallery", "explore", "tour", "discover",
    "rooms", "chambres", "suite", "spa", "pool", "piscine", "dining", "restaurant",
    "bar", "amenities", "facilities", "experiences", "the-resort", "the-hotel",
)

# Filtres pour les <img> qu'on ignore
PICTO_URL_KEYWORDS = ("/icon", "/logo", "favicon", "sprite", "/ui/", "play-button",
                      "pixel", "tracker", "analytics", "loader", "/avatar", "/badge",
                      "brand%20device", "brand-device", "brand_device",
                      "/social", "/share", "instagram", "facebook-icon", "twitter-icon",
                      "/flags/", "/cookie", "consent", "spinner")
IGNORED_FORMATS = (".svg", ".gif")
ALLOWED_FORMATS = (".jpg", ".jpeg", ".png", ".webp", ".avif")
MIN_WIDTH = 600
MIN_HEIGHT = 400


def _abs_url(base: str, href: str) -> str:
    return urljoin(base, href)


def _is_picto_url(url: str) -> bool:
    u = url.lower()
    if any(u.endswith(ext) for ext in IGNORED_FORMATS):
        return True
    return any(kw in u for kw in PICTO_URL_KEYWORDS)


def _scroll_and_collect_images(page, max_scrolls: int = 25) -> list[dict]:
    """Scroll la page pour déclencher lazy-load + collecte 3 sources :
      1) <img> classiques (avec data-src lazy)
      2) background-image CSS (Grand Beach style)
      3) <picture><source srcset> (Yotel/Drupal/Marriott style — souvent oublié)

    Triple collecte : sans elle on rate les sites modernes qui utilisent <picture> responsive
    avec lazy-load, où le <img> reste vide jusqu'au scroll-to-view.
    """
    # Scroll plus agressif — certains sites lazy-loadent vraiment au-delà du viewport
    last_height = 0
    stable_count = 0
    for _ in range(max_scrolls):
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height:
            stable_count += 1
            if stable_count >= 2:
                break
        else:
            stable_count = 0
        last_height = height
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(400)
    # Petit scroll-up + scroll-down pour redéclencher les observers manqués
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(800)

    # 1) <img> classiques
    imgs_tags = page.eval_on_selector_all(
        "img",
        """imgs => imgs.map(i => ({
            src: i.currentSrc || i.src || i.getAttribute('data-src') || i.getAttribute('data-lazy-src') || '',
            srcset: i.srcset || '',
            w: i.naturalWidth, h: i.naturalHeight,
            display_w: i.width, display_h: i.height,
            alt: (i.alt || '').substring(0, 100),
            type: 'img',
        }))"""
    )

    # 2) background-image CSS (très fréquent pour galeries de sites hôteliers modernes)
    bg_imgs = page.evaluate("""() => {
        const seen = new Set();
        const results = [];
        const els = document.querySelectorAll('*');
        for (const el of els) {
            const bg = window.getComputedStyle(el).backgroundImage;
            if (!bg || !bg.includes('url') || bg.includes('gradient')) continue;
            const match = bg.match(/url\\(["']?([^"')]+)["']?\\)/);
            if (!match) continue;
            const url = match[1];
            if (seen.has(url)) continue;
            seen.add(url);
            const r = el.getBoundingClientRect();
            results.push({
                src: url,
                srcset: '',
                w: 0, h: 0,
                display_w: Math.round(r.width),
                display_h: Math.round(r.height),
                alt: '',
                type: 'bg',
            });
        }
        return results;
    }""")

    # 3) <picture><source srcset> — Drupal/Yotel et beaucoup de sites responsive moderne.
    #    On collecte chaque <source> en remontant au <picture> parent pour avoir un display size.
    source_imgs = page.evaluate("""() => {
        const out = [];
        // Sources dans <picture>
        const pictures = document.querySelectorAll('picture');
        for (const pic of pictures) {
            const sources = pic.querySelectorAll('source');
            const r = pic.getBoundingClientRect();
            for (const s of sources) {
                const ss = s.getAttribute('srcset') || s.srcset || '';
                const single = s.getAttribute('src') || '';
                if (!ss && !single) continue;
                out.push({
                    src: single,
                    srcset: ss,
                    w: 0, h: 0,
                    display_w: Math.round(r.width),
                    display_h: Math.round(r.height),
                    alt: '',
                    type: 'source',
                });
            }
        }
        // Sources standalone (hors <picture>) — moins courant mais possible (<video poster>, etc.)
        const allSources = document.querySelectorAll('source[srcset]');
        for (const s of allSources) {
            if (s.parentElement && s.parentElement.tagName === 'PICTURE') continue;
            const ss = s.getAttribute('srcset') || '';
            if (!ss) continue;
            out.push({
                src: '',
                srcset: ss,
                w: 0, h: 0,
                display_w: 1200,  // assumed
                display_h: 800,
                alt: '',
                type: 'source',
            });
        }
        return out;
    }""")

    return imgs_tags + bg_imgs + source_imgs


def _largest_from_srcset(srcset: str) -> str | None:
    """Parse un srcset et retourne l'URL la plus large (max width).

    Supporte 3 formats :
      - "url1 1280w, url2 800w" → renvoie url1 (largest width)
      - "url1 2x, url2 1x" → renvoie url1 (highest density)
      - "url1" (descripteur absent, cas Drupal Yotel) → renvoie url1 (fallback)
    """
    if not srcset:
        return None
    best_w = (0, None)
    best_x = (0.0, None)
    fallback_first: str | None = None
    for entry in srcset.split(","):
        parts = entry.strip().split()
        if not parts:
            continue
        url = parts[0]
        if fallback_first is None:
            fallback_first = url
        # Width descriptor (1280w)
        for p in parts[1:]:
            m = re.match(r"(\d+)w$", p)
            if m and int(m.group(1)) > best_w[0]:
                best_w = (int(m.group(1)), url)
                continue
            # Density descriptor (2x, 1.5x)
            mx = re.match(r"([\d.]+)x$", p)
            if mx:
                try:
                    d = float(mx.group(1))
                    if d > best_x[0]:
                        best_x = (d, url)
                except ValueError:
                    pass
    if best_w[1]:
        return best_w[1]
    if best_x[1]:
        return best_x[1]
    return fallback_first  # srcset = juste une URL sans descripteur


def _drupal_size_from_url(url: str) -> int:
    """Extrait la largeur d'un style Drupal (`/styles/1280w/`, `/styles/2000w_focal_point/`).
    0 si pas de pattern Drupal détecté."""
    m = re.search(r"/styles/(\d+)w", url)
    return int(m.group(1)) if m else 0


def _normalize_base_url(url: str) -> str:
    """Retire la partie style Drupal pour obtenir une 'identité' photo stable.
    `https://x/sites/default/files/styles/1280w/public/2026-02/MIA_N577.jpg?itok=...`
    →  `https://x/sites/default/files/2026-02/MIA_N577.jpg`
    Permet de dédup les variants responsive d'une même photo.
    """
    no_qs = url.split("?")[0]
    return re.sub(r"/styles/[^/]+/public/", "/", no_qs)


def _filter_images(images: list[dict], base_url: str) -> list[str]:
    """Applique les filtres (taille, format, URL pattern) et retourne les URLs absolues uniques.

    Gère 3 types : <img> (avec naturalWidth), background-image CSS, <picture><source>.
    Dédup intra-photo : si plusieurs variants Drupal `/styles/{size}w/` pour la même photo,
    on garde uniquement la plus grande résolution.
    """
    seen: set[str] = set()
    out: list[str] = []
    for img in images:
        src = img.get("src") or ""
        # Préfère l'URL la plus haute résolution depuis srcset si dispo
        srcset_best = _largest_from_srcset(img.get("srcset") or "")
        if srcset_best:
            src = srcset_best
        if not src or src.startswith("data:"):
            continue
        if not src.startswith("http"):
            src = _abs_url(base_url, src)
        if _is_picto_url(src):
            continue
        clean = src.split("?")[0].lower()
        if not any(clean.endswith(ext) for ext in ALLOWED_FORMATS):
            continue

        img_type = img.get("type") or "img"
        is_bg_or_source = img_type in ("bg", "source")
        nw, nh = img.get("w") or 0, img.get("h") or 0
        dw, dh = img.get("display_w") or 0, img.get("display_h") or 0

        if is_bg_or_source:
            # Pour bg/source : pas de naturalWidth, on se base sur display_w/h du conteneur.
            # Pour les <source> dans <picture>, le display_w correspond à la box du <picture>
            # — généralement la taille d'affichage. Si trop petit (carrousel thumb), on skip.
            if dw < MIN_WIDTH or dh < MIN_HEIGHT:
                # Mais on accepte quand même si on a un srcset qui propose des grandes tailles
                # (cas Yotel : <picture> rendu petit dans une grille mais srcset propose 2000w)
                if not srcset_best:
                    continue
        else:
            # <img> classique : on filtre sur naturalWidth si dispo, sinon display
            if nw > 0 and nh > 0:
                if nw < MIN_WIDTH or nh < MIN_HEIGHT:
                    continue
                # Carré et petit = picto
                if 0.95 <= nw / max(nh, 1) <= 1.05 and nw < 1000:
                    continue
            elif dw > 0 and dh > 0:
                # Lazy-loaded sans nw chargé : on accepte si display > min
                if dw < MIN_WIDTH or dh < MIN_HEIGHT:
                    continue

        if src in seen:
            continue
        seen.add(src)
        out.append(src)

    # Dédup intra-photo : si plusieurs variants `/styles/{N}w/` pour la même base, garder le plus grand
    by_base: dict[str, tuple[int, str]] = {}
    for u in out:
        base = _normalize_base_url(u)
        size = _drupal_size_from_url(u)
        # 0 = original sans style → considéré comme largest
        if size == 0:
            size = 99999
        if base not in by_base or size > by_base[base][0]:
            by_base[base] = (size, u)
    return [v[1] for v in by_base.values()]


def _try_paths_and_collect(page, base_url: str, paths: list[str]) -> list[str]:
    """Visite chaque path candidat et collecte les images."""
    all_imgs: list[str] = []
    seen: set[str] = set()
    for path in paths:
        url = base_url.rstrip("/") + path
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if not response or response.status != 200:
                continue
            page.wait_for_timeout(1200)
            imgs = _scroll_and_collect_images(page)
            for u in _filter_images(imgs, url):
                if u not in seen:
                    seen.add(u)
                    all_imgs.append(u)
        except Exception:
            continue
    return all_imgs


def _find_gallery_links_from_home(page, base_url: str) -> list[str]:
    """Cherche dans la home les liens vers des pages galerie potentielles."""
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
    except Exception:
        return []

    links = page.eval_on_selector_all(
        "a",
        """a => a.map(el => ({
            href: el.href,
            text: (el.innerText || '').substring(0, 80),
        }))"""
    )

    candidates: list[str] = []
    seen: set[str] = set()
    base_host = urlparse(base_url).netloc.lower()
    for link in links:
        href = (link.get("href") or "").strip()
        text = (link.get("text") or "").strip().lower()
        if not href or href.startswith("javascript:") or href.startswith("#"):
            continue
        # Reste sur le même domaine
        try:
            if urlparse(href).netloc and urlparse(href).netloc.lower() != base_host:
                continue
        except Exception:
            continue
        # Match keyword dans href ou text
        haystack = (href + " " + text).lower()
        if any(kw in haystack for kw in GALLERY_KEYWORDS):
            if href not in seen:
                seen.add(href)
                candidates.append(href)
    return candidates[:5]  # max 5 pages candidates


def _detect_blocking(page) -> str | None:
    """Retourne un libellé du blocage détecté, ou None si la page semble OK.

    Signaux empiriques (Marriott, Hilton, Hyatt observés 12/05/2026) :
      - Title contient « Access Denied », « Just a moment », « Blocked »
      - HTML extrêmement court (< 5KB = page d'erreur générique)
      - Body uniquement « Access Denied » ou similaire
    """
    try:
        title = (page.title() or "").lower()
        if any(s in title for s in ("access denied", "blocked", "just a moment",
                                     "attention required", "bot or not", "robot ou pas")):
            return f"blocked_title:{page.title()[:50]}"
        html_len = page.evaluate("document.documentElement.outerHTML.length")
        if html_len and html_len < 5000:
            body_text = page.evaluate("(document.body && document.body.innerText || '').slice(0,200).toLowerCase()")
            if any(s in body_text for s in ("access denied", "blocked", "cloudflare",
                                             "akamai", "verify you are human")):
                return f"blocked_small_html:{html_len}b"
    except Exception:
        pass
    return None


def _run_extract_with_engine(site_url: str, max_total_photos: int, use_patchright: bool, headless: bool) -> dict:
    """Lance l'extraction galerie avec un moteur Playwright donné.

    Args:
        use_patchright : si True, utilise patchright + channel='chrome' + headless=False
                         (bypass DataDome/Akamai mais ouvre fenêtre Chrome visible).
                         Sinon Playwright stealth + headless (rapide, invisible).
        headless       : forcé False si use_patchright (sinon DataDome détecte).

    Returns: même structure que extract_gallery_photos (photos, pages_visited, error,
             + 'blocked_signal' si on a détecté un blocage sur la première page).
    """
    out = {"photos": [], "pages_visited": [], "error": None, "blocked_signal": None,
           "engine": "patchright" if use_patchright else "playwright_stealth"}
    parsed = urlparse(site_url)
    site_prefix = site_url.rstrip("/")

    def _run(p, ctx_or_browser, page):
        photos: list[str] = []
        seen: set[str] = set()

        def _absorb(urls):
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    photos.append(u)

        # 1) Page hôtel direct
        try:
            page.goto(site_url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(2000)
            # ━ Détection blocage côté chaîne hôtelière (Marriott/Hilton/Hyatt) ━
            block = _detect_blocking(page)
            if block:
                out["blocked_signal"] = block
                print(f"  [hotel_site] blocage détecté ({block}) sur {site_url[:80]}",
                      file=sys.stderr)
                return photos
            imgs = _scroll_and_collect_images(page)
            _absorb(_filter_images(imgs, site_url))
            out["pages_visited"].append(site_url)
        except Exception:
            pass

        # 2) Liens internes (gallery/rooms/spa/dining/explore)
        if len(photos) < max_total_photos:
            try:
                gallery_links = _find_gallery_links_from_home(page, site_url)
            except Exception:
                gallery_links = []
            for link in gallery_links[:5]:
                if len(photos) >= max_total_photos:
                    break
                try:
                    response = page.goto(link, wait_until="domcontentloaded", timeout=20000)
                    if not response or response.status != 200:
                        continue
                    out["pages_visited"].append(link)
                    page.wait_for_timeout(1200)
                    imgs = _scroll_and_collect_images(page)
                    _absorb(_filter_images(imgs, link))
                except Exception:
                    continue

        # 3) Patterns relatifs au préfixe hôtel
        if len(photos) < 30:
            more = _try_paths_and_collect(page, site_prefix, GALLERY_PATH_PATTERNS)
            out["pages_visited"].extend([site_prefix + pth for pth in GALLERY_PATH_PATTERNS])
            _absorb(more)

        return photos

    try:
        if use_patchright:
            if not _HAS_PATCHRIGHT:
                out["error"] = "patchright non installé (pip install patchright + patchright install chromium)"
                return out
            with patchright_sync() as p:
                user_data_dir = tempfile.mkdtemp(prefix="patchright_hotel_")
                # IMPORTANT : pas de UA override / args custom → patchright gère.
                kwargs = {"user_data_dir": user_data_dir, "headless": False, "no_viewport": True}
                try:
                    ctx = p.chromium.launch_persistent_context(channel="chrome", **kwargs)
                except Exception as e:
                    print(f"  [hotel_site] chrome channel KO ({e}) → fallback chromium",
                          file=sys.stderr)
                    ctx = p.chromium.launch_persistent_context(**kwargs)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                photos = _run(p, ctx, page)
                ctx.close()
        else:
            with Stealth().use_sync(sync_playwright()) as p:
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
                photos = _run(p, context, page)
                browser.close()
        out["photos"] = photos[:max_total_photos]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def extract_gallery_photos(site_url: str, max_total_photos: int = 200) -> dict:
    """Extrait les photos de galerie depuis le site officiel.

    Stratégie en cascade (Martin 12/05/2026) :
      A. Playwright stealth + headless (rapide, invisible) ← tente d'abord.
      B. Si bloqué (Access Denied / Just a moment / 0 photo après crawl complet)
         ET patchright dispo → retry avec patchright + Chrome réel + headless=False.
         ⚠️ Conséquence : fenêtre Chrome brièvement visible (~30s) sur le bureau.

    Sites typiquement résolus uniquement par patchright :
      Marriott, Hilton, Hyatt, IHG (Akamai/Imperva anti-bot).

    Returns: {photos, pages_visited, error, engine}
    """
    # ━ Tentative A : moteur rapide (Playwright stealth headless) ━
    result_a = _run_extract_with_engine(site_url, max_total_photos,
                                          use_patchright=False, headless=True)

    # ━ Heuristique de retry ━
    # Retry si :
    #   - blocage explicite détecté (Access Denied / Just a moment / etc.)
    #   - OU 0 photo extraite après visite des pages (= probable blocage subtil)
    should_retry = (
        result_a.get("blocked_signal") is not None
        or (len(result_a.get("photos") or []) == 0 and not result_a.get("error"))
    )

    if should_retry and _HAS_PATCHRIGHT:
        print(f"  [hotel_site] retry avec patchright (signal={result_a.get('blocked_signal')}, "
              f"photos={len(result_a.get('photos') or [])})", file=sys.stderr)
        result_b = _run_extract_with_engine(site_url, max_total_photos,
                                             use_patchright=True, headless=False)
        # On garde le résultat avec le plus de photos (sauf si B a planté)
        if not result_b.get("error") and len(result_b.get("photos") or []) > len(result_a.get("photos") or []):
            result_b["fallback_from"] = result_a.get("blocked_signal") or "0_photos"
            return result_b

    return result_a


# CLI
if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("Usage: python hotel_gallery_extractor.py <url>")
        sys.exit(1)
    result = extract_gallery_photos(sys.argv[1])
    print(f"Pages visitées : {len(result['pages_visited'])}")
    print(f"Photos trouvées : {len(result['photos'])}")
    if result.get("error"):
        print(f"Erreur : {result['error']}")
    for p in result["photos"][:8]:
        print(f"  {p[:130]}")
