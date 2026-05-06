"""Scrape les photos d'un profil Instagram public via Playwright stealth.

Stratégie :
  1. Goto profile URL avec Playwright stealth (anti-bot Instagram basique)
  2. Décline les cookies / dismisse les popups login si présents
  3. Scroll progressivement pour charger plus de posts (limité pour éviter détection)
  4. Extrait les <img> de la grille de posts (max ~30 photos)
  5. Filtre les URLs (taille min, formats) — Instagram sert du JPEG haute qualité

Limitations :
- Compte privé → impossible (renvoie liste vide)
- Compte avec login wall → on essaie quand même via stealth, ~50% succès
- Vidéos / Reels → on prend la thumbnail (encore une image)

Usage :
    from instagram_scraper import scrape_instagram_photos
    urls = scrape_instagram_photos("https://www.instagram.com/yotelmiami/", max_photos=30)
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

ALLOWED_FORMATS = (".jpg", ".jpeg", ".webp")
MIN_DIM = 400  # pixels


def _is_valid_post_image(url: str) -> bool:
    """Vrai si l'URL est une image de post Instagram exploitable."""
    if not url or not url.startswith("http"):
        return False
    u = url.lower().split("?")[0]
    # Instagram CDN : *.cdninstagram.com ou scontent.cdninstagram.com / fbcdn.net
    if "cdninstagram.com" not in url and "fbcdn.net" not in url:
        return False
    # Skip avatars / profile pics (généralement petits + path /profile/ ou /s150x150)
    if "/s150x150/" in url or "/s320x320/" in url:
        return False
    if "_n.jpg" not in u and "_n.webp" not in u and not any(u.endswith(ext) for ext in ALLOWED_FORMATS):
        # Instagram main post image format = ..._n.jpg ou ..._n.webp
        return False
    return True


def _scroll_and_collect(page, max_scrolls: int = 6) -> list[dict]:
    """Scroll la page Instagram pour charger plus de posts + collecte les <img>."""
    last_height = 0
    for _ in range(max_scrolls):
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height:
            break
        last_height = height
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(800)  # Instagram a besoin de temps pour charger
    page.wait_for_timeout(1500)

    # Collecte les <img> avec leur taille naturelle pour filtrer
    imgs = page.eval_on_selector_all(
        "img",
        """imgs => imgs.map(i => ({
            src: i.currentSrc || i.src || '',
            srcset: i.srcset || '',
            w: i.naturalWidth, h: i.naturalHeight,
            alt: (i.alt || '').substring(0, 100),
        }))"""
    )
    return imgs


def _largest_from_srcset(srcset: str) -> str | None:
    """Parse un srcset Instagram et retourne l'URL la plus large."""
    if not srcset:
        return None
    best = (0, None)
    for entry in srcset.split(","):
        parts = entry.strip().split()
        if not parts:
            continue
        url = parts[0]
        width = 0
        for p in parts[1:]:
            m = re.match(r"(\d+)w", p)
            if m:
                width = int(m.group(1))
        if width > best[0]:
            best = (width, url)
    return best[1]


def scrape_instagram_photos(profile_url: str, max_photos: int = 30) -> dict:
    """Scrape les URLs des photos publiques d'un profil Instagram.

    Args:
        profile_url : ex "https://www.instagram.com/yotelmiami/"
        max_photos : limite

    Returns:
        {
            "photos": [list of URLs],
            "profile_url": str,
            "error": str | None,
        }
    """
    out = {"photos": [], "profile_url": profile_url, "error": None}
    if not profile_url or "instagram.com" not in profile_url:
        out["error"] = "URL Instagram invalide"
        return out

    try:
        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = context.new_page()

            try:
                response = page.goto(profile_url, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                out["error"] = f"goto failed: {type(e).__name__}: {str(e)[:120]}"
                browser.close()
                return out

            if not response or response.status >= 400:
                out["error"] = f"HTTP {response.status if response else 'no response'} (compte privé / banni / Instagram challenge)"
                browser.close()
                return out

            # Décline le banner cookies si présent
            try:
                page.click('button:has-text("Decline optional cookies")', timeout=2000)
            except Exception:
                pass
            try:
                page.click('button:has-text("Allow all cookies")', timeout=1000)
            except Exception:
                pass

            # Dismiss le popup login si présent (parfois "Log in to see more from...")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            except Exception:
                pass

            # ━ Détection du blocage Instagram généralisé (2024+) ━
            # Instagram affiche "Profile isn't available • Instagram" pour TOUS les visiteurs
            # anonymes (que le profile existe ou pas), depuis qu'ils ont durci leur anti-bot.
            # Le seul moyen fiable de scraper est via une API tierce ou un login Insta.
            page_title = (page.title() or "").lower()
            if (
                "page not found" in page_title
                or "isn't available" in page_title
                or "n'est pas disponible" in page_title
                or "sorry" in page_title
            ):
                out["error"] = (
                    "🚫 Instagram bloque le scraping anonyme. "
                    "Sans API tierce (RapidAPI / ScrapingBee Insta) ou login Instagram, "
                    "il est impossible de récupérer les photos. Le compte existe peut-être bien, "
                    "Instagram bloque tous les bots indistinctement."
                )
                browser.close()
                return out

            # Scroll + collecte
            imgs = _scroll_and_collect(page, max_scrolls=6)

            # Filtre + dédup + tri par taille naturelle desc
            seen: set[str] = set()
            collected: list[tuple[int, str]] = []  # (size_score, url)
            for img in imgs:
                src = img.get("src") or ""
                # Préfère l'URL la plus large depuis srcset
                srcset_best = _largest_from_srcset(img.get("srcset") or "")
                if srcset_best:
                    src = srcset_best

                if not _is_valid_post_image(src):
                    continue
                clean = src.split("?")[0]
                if clean in seen:
                    continue
                seen.add(clean)

                # Score = max(natural_size, srcset_width)
                w = img.get("w") or 0
                size_score = w * (img.get("h") or 0)
                if w < MIN_DIM or (img.get("h") or 0) < MIN_DIM:
                    # Skip les avatars / icônes
                    if size_score < (MIN_DIM * MIN_DIM):
                        continue

                collected.append((size_score, src))

            collected.sort(key=lambda t: -t[0])
            out["photos"] = [url for _, url in collected[:max_photos]]
            browser.close()

    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    return out


# CLI
if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) < 2:
        print("Usage: python instagram_scraper.py <profile_url>")
        sys.exit(1)
    result = scrape_instagram_photos(sys.argv[1])
    print(f"Profile : {result['profile_url']}")
    print(f"Photos trouvées : {len(result['photos'])}")
    if result.get("error"):
        print(f"Erreur : {result['error']}")
    for p in result["photos"][:8]:
        print(f"  {p[:130]}")
