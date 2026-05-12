"""PDF Export — génère un deck "avant/après" branded Dayuse depuis un run pipeline.

Stratégie technique : on rend un HTML/CSS branded (charte Dayuse) via Jinja2, puis
on convertit en PDF avec Playwright (déjà installé pour le scraping). Avantage vs
WeasyPrint : support CSS3 complet (gradients, flex, grid), pas de dépendances
système supplémentaires (cairo/pango).

Structure du PDF :
- Page 1 : cover (hôtel, date, stats globales du run)
- Pages 2+ : 1 page par photo enhanced — avant/après côte à côte + métadonnées
"""

from __future__ import annotations

import base64
import datetime as _dt
from pathlib import Path
from typing import Iterable

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).parent
TEMPLATES_DIR = ROOT / "templates"


def _img_to_data_url(path: Path) -> str:
    """Convertit une image locale en data URL base64 pour embedding dans le HTML.

    Pour Playwright + file:// les images locales peuvent poser problème de
    cross-origin. Le data URL contourne ça et garantit la portabilité du HTML
    rendu (utile pour debug : on peut ouvrir le HTML dans un navigateur).
    """
    try:
        data = path.read_bytes()
    except Exception:
        return ""
    suffix = path.suffix.lower().lstrip(".") or "jpg"
    mime = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _action_label(action: str) -> str:
    """Label humain compact pour les badges d'action IA."""
    return {
        "ai_lighting": "Lumière IA",
        "ai_add_character": "Personnage ajouté",
        "ai_add_pool_float": "Bouée ajoutée",
        "ai_remove_clutter": "Nettoyage IA",
        "ai_remove_people": "Foule retirée",
        "ai_recompose": "Recadrage IA",
        "local_smart_crop": "Recadrage local",
        "local_warm_boost": "Warm boost",
        "cached": "Cache",
    }.get(action, action)


def _build_html(slug: str, run_data: dict) -> str:
    """Rend le template Jinja2 avec les données du run + images embeddées en base64."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["action_label"] = _action_label

    hotel = run_data.get("hotel") or {}
    enhanced_results = run_data.get("enhanced") or []

    # Embed images en data URL pour rendu Playwright fiable
    photos_for_template = []
    uploads_dir = ROOT / "data" / "uploads" / slug
    enhanced_dir = ROOT / "data" / "output" / slug / "enhanced"
    for entry in enhanced_results:
        filename = entry.get("filename")
        if not filename:
            continue
        before_path = uploads_dir / filename
        after_path = enhanced_dir / filename
        if not after_path.exists():
            continue
        photos_for_template.append({
            "filename": filename,
            "final_order_pos": entry.get("final_order_pos"),
            "action": entry.get("action"),
            "action_label": _action_label(entry.get("action", "")),
            "reason": entry.get("reason") or "",
            "justification": entry.get("justification") or {},
            "transformations": entry.get("transformations") or {},
            "is_fully_generated": bool(entry.get("is_fully_generated")),
            "is_bonus": bool(entry.get("is_bonus")),
            "persona_used": entry.get("persona_used"),
            "before_data_url": _img_to_data_url(before_path) if before_path.exists() else "",
            "after_data_url": _img_to_data_url(after_path),
        })

    # Stats globales du run
    n_total = len(photos_for_template)
    n_ai_retouched = sum(1 for p in photos_for_template if p["action"].startswith("ai_"))
    n_human_added = sum(
        1 for p in photos_for_template
        if p["transformations"].get("character_added") or p["transformations"].get("bonus_lifestyle")
    )
    n_clutter = sum(1 for p in photos_for_template if p["transformations"].get("clutter_removed"))
    n_lighting = sum(1 for p in photos_for_template if p["transformations"].get("ai_lighting"))

    cost_usd_total = float(run_data.get("cost_usd_total") or 0)
    pipeline_duration_s = float(run_data.get("pipeline_duration_s") or 0)

    # ━━ Page "Preview Dayuse" : mock fidèle de la page hôtel sur dayuse.fr ━━
    # Layout hero = 1 grande photo gauche + 2 petites empilées droite (matche le
    # vrai design dayuse.fr). On prend les 3 premières photos finalistes du pack.
    # Adresse + avis fictifs (le but est de visualiser le rendu en prod, pas de
    # remplacer la vraie page).
    hero_photos = photos_for_template[:3]
    preview_data = {
        "enabled": len(hero_photos) >= 3,
        "hero_main": hero_photos[0] if len(hero_photos) >= 1 else None,
        "hero_top": hero_photos[1] if len(hero_photos) >= 2 else None,
        "hero_bottom": hero_photos[2] if len(hero_photos) >= 3 else None,
        "address_fake": _make_fake_address(hotel.get("city", "")),
        "rating": "4.5",
        "rating_label": "Excellent",
        "n_reviews": 9,
        "review_quote": "Belle piscine, accueil chaleureux, vibe vraiment Dayuse. On reviendra.",
        "review_author": "Robert",
        # Breadcrumb façon dayuse.fr : États-Unis › Florida › Miami › Miami beach › South Beach
        "breadcrumb": _make_breadcrumb(hotel.get("city", "")),
    }

    context = {
        "slug": slug,
        "hotel_name": hotel.get("name") or slug,
        "hotel_city": hotel.get("city") or "",
        "hotel_stars": hotel.get("stars") or "",
        "generated_at": _dt.datetime.now().strftime("%d %B %Y"),
        "photos": photos_for_template,
        "preview": preview_data,
        "stats": {
            "n_total": n_total,
            "n_ai_retouched": n_ai_retouched,
            "n_human_added": n_human_added,
            "n_clutter": n_clutter,
            "n_lighting": n_lighting,
            "cost_usd": cost_usd_total,
            "duration_s": pipeline_duration_s,
            "duration_human": _format_duration(pipeline_duration_s),
        },
    }
    template = env.get_template("pdf_export.html")
    return template.render(**context)


def _make_fake_address(city: str) -> str:
    """Adresse fictive plausible pour la ville. Pas besoin d'être réelle —
    le PDF sert à visualiser un mock, pas à donner une vraie adresse."""
    if not city:
        return "915 Washington Ave, USA"
    return f"915 Washington Ave, {city}, USA"


def _make_breadcrumb(city: str) -> list[str]:
    """Reconstruit un breadcrumb façon dayuse.fr selon la ville détectée."""
    if not city:
        return ["États-Unis"]
    city_lower = city.lower()
    if "miami" in city_lower:
        return ["États-Unis", "Florida", "Miami", "Miami beach"]
    if "new york" in city_lower or "nyc" in city_lower:
        return ["États-Unis", "New York", "Manhattan"]
    if "paris" in city_lower:
        return ["France", "Île-de-France", "Paris"]
    if "los angeles" in city_lower or " la " in city_lower:
        return ["États-Unis", "California", "Los Angeles"]
    # Fallback : on garde juste la ville
    return ["États-Unis", city]


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}min{s:02d}"


def generate_pdf(slug: str, run_data: dict) -> bytes:
    """Pipeline : run_data → HTML rendu → PDF binary via Playwright Chromium.

    Args:
        slug : hôtel slug (utilisé pour résoudre les chemins photos)
        run_data : structure JSON retournée par /api/run

    Returns:
        bytes du PDF prêt à servir.
    """
    html = _build_html(slug, run_data)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1240, "height": 1754})  # A4 portrait @150dpi
        page = context.new_page()
        # data: URL pour charger le HTML sans avoir à écrire un fichier temporaire
        page.set_content(html, wait_until="domcontentloaded")
        # Petit délai pour s'assurer que la font Google Manrope soit chargée
        page.wait_for_timeout(800)
        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
            prefer_css_page_size=False,
        )
        browser.close()
    return pdf_bytes
