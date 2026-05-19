"""Front Flask pour l'outil DayAccess.

Routes :
    GET  /                : page principale (form URL RP + upload photos)
    POST /api/scrape      : scrape une URL RP, retourne JSON
    POST /api/upload      : upload d'un dossier de photos pour un hôtel
    POST /api/run         : lance le pipeline complet (analyse + sélection)
                            → pour V0 : retourne RP + liste photos uploadées

Usage :
    source .venv/bin/activate
    python app.py
    → http://localhost:5050
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_from_directory, Response

import rp_scraper
import booking_scraper
import hotel_site_finder
import hotel_gallery_extractor
import analyze
import audit_db  # Martin 15/05/2026 : persist validation + tagging humain
import dedup_angles
import coverage as coverage_mod
import enhance
import ordering
import photo_generator
import progress
import spend
import dedup_vlm
import amenity_verifier
import photo_journey as photo_journey_mod
import booking_amenities_extractor
import slowmo_higgsfield
import pdf_export
import expedia_finder
import expedia_scraper
import rp_finder

ROOT = Path(__file__).parent
UPLOADS_DIR = ROOT / "data" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB par batch

# ━━ JSON encoder numpy-safe (Martin 19/05/2026, fix prod Sagamore) ━━━━━━━━━
# Le pipeline produit des scores via ImageHash / numpy / scipy → certaines
# valeurs sont des numpy.int64 / numpy.float64 / numpy.ndarray que Flask
# ne sait pas serialiser → crash "Object of type int64 is not JSON serializable"
# AU MOMENT de retourner la réponse finale, après que tout le pipeline ait
# tourné (= analyses + photos déjà sauvées sur disk + DB, juste la réponse
# HTTP qui foire → "Unexpected token '<'" côté front car page erreur HTML).
# Fix : custom JSON provider qui convertit auto les types numpy.
try:
    import numpy as _np_for_json
    from flask.json.provider import DefaultJSONProvider as _DefaultJSONProvider

    class _NumpySafeJSONProvider(_DefaultJSONProvider):
        def default(self, obj):
            if isinstance(obj, _np_for_json.integer):
                return int(obj)
            if isinstance(obj, _np_for_json.floating):
                return float(obj)
            if isinstance(obj, _np_for_json.ndarray):
                return obj.tolist()
            if isinstance(obj, (_np_for_json.bool_,)):
                return bool(obj)
            # Path, datetime, etc. — fallback str()
            return super().default(obj)

    app.json = _NumpySafeJSONProvider(app)
except ImportError:
    pass  # numpy pas installé → on garde le provider Flask par défaut

# ━━ Proxy fix pour Railway (Martin 19/05/2026) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Railway/Heroku/Render mettent l'app derrière un reverse proxy HTTPS qui forward
# en HTTP interne. Sans ce middleware, Flask url_for(_external=True) génère des
# URLs en http://... au lieu de https://..., ce qui casse OAuth (redirect_uri
# mismatch côté Google car Google attend https://).
# x_proto=1 : lit X-Forwarded-Proto (https/http)
# x_host=1  : lit X-Forwarded-Host (le vrai host public, ex: photodaypass-production.up.railway.app)
# x_for=1   : lit X-Forwarded-For (IP client réelle)
if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("BEHIND_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)
    app.config["PREFERRED_URL_SCHEME"] = "https"

# ━━ SSO Google OAuth (Martin 19/05/2026) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Protection globale : toutes les routes exigent un login Google avec email
# se terminant par @dayuse.com. Routes publiques (login, callback, healthcheck,
# static) bypass auto via le middleware before_request.
# Configurer via env vars : GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
#                            FLASK_SECRET_KEY, ALLOWED_EMAIL_DOMAIN.
import auth as auth_module
auth_module.init_oauth(app)
app.register_blueprint(auth_module.auth_bp)
auth_module.require_login_globally(app)


@app.route("/")
def index():
    return render_template("index.html", current_user=auth_module.current_user())


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    """Reçoit {url}, scrape RP, sauvegarde dans data/rp/, retourne le JSON."""
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url.startswith("https://www.resortpass.com/hotels/"):
        return jsonify({"error": "URL doit être une fiche hôtel ResortPass."}), 400

    try:
        result = rp_scraper.scrape(url)
    except Exception as e:
        return jsonify({"error": f"Échec scraping : {e}"}), 500

    # Sauvegarde
    slug = url.rstrip("/").split("/")[-1]
    rp_dir = ROOT / "data" / "rp"
    rp_dir.mkdir(parents=True, exist_ok=True)
    out_path = rp_dir / f"{slug}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return jsonify({"slug": slug, "data": result})


@app.route("/api/scrape-booking", methods=["POST"])
def api_scrape_booking():
    """Mode Booking-only : on récupère amenities + meta hôtel depuis Booking (sans RP).

    Body : {url: "https://www.booking.com/hotel/..."}
    Output : compatible avec /api/scrape (rp data structure équivalente).
    Le slug est dérivé du chemin Booking (ex: booking-yotel-miami).
    """
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url.startswith("https://www.booking.com/hotel/"):
        return jsonify({"error": "URL doit être une fiche hôtel Booking."}), 400

    progress.init("booking_scrape", total=2, step="extracting")
    progress.update("booking_scrape", message="Scraping page Booking + analyse Gemini…")
    try:
        data = booking_amenities_extractor.extract_hotel_data_from_booking(url)
    except Exception as e:
        progress.finish("booking_scrape", message=f"Erreur : {str(e)[:200]}")
        return jsonify({"error": f"Échec extraction Booking : {str(e)[:200]}"}), 500

    if data.get("error"):
        progress.finish("booking_scrape", message=f"Erreur : {data['error']}")
        return jsonify({"error": data["error"]}), 500

    # Génère un slug à partir du chemin Booking (ex: yotel-miami) ou du nom hôtel
    slug = f"booking-{booking_scraper.booking_url_to_slug(url)}"

    # Sauvegarde au même endroit que les scrapes RP (pour réutiliser le pipeline en aval)
    rp_dir = ROOT / "data" / "rp"
    rp_dir.mkdir(parents=True, exist_ok=True)
    out_path = rp_dir / f"{slug}.json"
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    progress.finish(f"booking_scrape", message="OK")
    return jsonify({"slug": slug, "data": data})


@app.route("/api/identify-hotel", methods=["POST"])
def api_identify_hotel():
    """Étape 1 unifiée (Martin 12/05/2026) : Booking est la clé d'entrée, on
    enrichit en parallèle avec les URLs des autres sources (RP, Expedia,
    Site officiel) via DDG/Gemini cascade.

    Body : {url: "https://www.booking.com/hotel/..."}
    Output : {
        slug, data (= meta hôtel comme /api/scrape-booking),
        urls: {rp_url, expedia_url, official_url},
        urls_meta: {rp: {source, error?}, expedia: {...}, ...}
    }

    Permet à l'UI de pré-remplir les champs URL en Étape 2 → l'utilisateur
    n'a qu'à éditer ce qui manque ou est faux.
    """
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url.startswith("https://www.booking.com/hotel/"):
        return jsonify({"error": "URL doit être une fiche hôtel Booking."}), 400

    progress.init("booking_scrape", total=3, step="extracting")
    progress.update("booking_scrape", current=1, total=3,
                    message="📥 Scraping fiche Booking + analyse Gemini…")

    # ━ Scrape Booking pour récupérer nom/ville/amenities ━
    # (Martin 18/05/2026) Le pipeline auto-find DDG+Gemini des URLs RP/Expedia/
    # Officiel a été retiré : le user saisit lui-même les URLs qu'il veut utiliser
    # via les champs du form. Cet endpoint ne fait QUE le scrape Booking pour
    # récupérer les métadonnées hôtel (nom, ville, amenities, vibe). Pour
    # auto-find ponctuel d'une URL, utiliser /api/auto-find-url.
    try:
        data = booking_amenities_extractor.extract_hotel_data_from_booking(url)
    except Exception as e:
        progress.finish("booking_scrape", message=f"Erreur : {str(e)[:200]}")
        return jsonify({"error": f"Échec extraction Booking : {str(e)[:200]}"}), 500
    if data.get("error"):
        progress.finish("booking_scrape", message=f"Erreur : {data['error']}")
        return jsonify({"error": data["error"]}), 500

    # Slug + sauvegarde du rp_data au même endroit que /api/scrape-booking
    slug = f"booking-{booking_scraper.booking_url_to_slug(url)}"
    rp_dir = ROOT / "data" / "rp"
    rp_dir.mkdir(parents=True, exist_ok=True)
    progress.update("booking_scrape", current=2, total=3,
                    message=f"💾 Booking parsée : {(data.get('name') or '?')[:40]} — sauvegarde…")
    with open(rp_dir / f"{slug}.json", "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # ━ Pas d'auto-find des URLs : le user les a saisies manuellement côté front ━
    urls = {
        "booking_url": url,
        "rp_url": None,
        "expedia_url": None,
        "official_url": None,
    }
    urls_meta = {
        "rp": {"url": None, "source": "manual"},
        "expedia": {"url": None, "source": "manual"},
        "official": {"url": None, "source": "manual"},
    }

    progress.finish("booking_scrape", message="Identification terminée")
    return jsonify({
        "slug": slug,
        "data": data,
        "urls": urls,
        "urls_meta": urls_meta,
    })


@app.route("/api/auto-find-url", methods=["POST"])
def api_auto_find_url():
    """Trouve UNE URL (RP / Expedia / Site officiel) à partir de l'URL Booking.

    Endpoint léger pour les boutons "🔎 Auto-find" du formulaire — appelé à la
    demande quand le user n'a pas l'URL et veut déléguer la recherche à
    DuckDuckGo + Gemini fallback.

    Body : {type: "rp"|"expedia"|"official", booking_url: str}
    Output : {url: str|None, source: "duckduckgo"|"gemini"|null, confidence: str|None, error?: str}
    """
    payload = request.get_json(silent=True) or {}
    url_type = (payload.get("type") or "").strip().lower()
    booking_url = (payload.get("booking_url") or "").strip()

    if url_type not in ("rp", "expedia", "official"):
        return jsonify({"error": "type doit être 'rp', 'expedia' ou 'official'"}), 400
    if not booking_url.startswith("https://www.booking.com/hotel/"):
        return jsonify({"error": "URL Booking valide requise"}), 400

    # ━ Scrape Booking (depuis cache si possible) pour récupérer nom/ville/pays ━
    slug = f"booking-{booking_scraper.booking_url_to_slug(booking_url)}"
    rp_path = ROOT / "data" / "rp" / f"{slug}.json"

    if rp_path.exists():
        with open(rp_path) as f:
            data = json.load(f)
    else:
        try:
            data = booking_amenities_extractor.extract_hotel_data_from_booking(booking_url)
            if data.get("error"):
                return jsonify({"error": f"Booking scrape échoué : {data['error']}"}), 500
            rp_path.parent.mkdir(parents=True, exist_ok=True)
            with open(rp_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            return jsonify({"error": f"Booking scrape crash : {str(e)[:200]}"}), 500

    name = data.get("name") or ""
    city = data.get("city") or ""
    country = data.get("country") or ""

    # ━ Appel finder approprié avec timeout 20s ━
    import concurrent.futures as _cf
    def _find():
        try:
            if url_type == "rp":
                return rp_finder.find_rp_url(name, city, country)
            if url_type == "expedia":
                return expedia_finder.find_expedia_url(name, city, country)
            if url_type == "official":
                return hotel_site_finder.find_hotel_site(name, city, country)
        except Exception as e:
            return {"url": None, "error": f"crash: {str(e)[:200]}"}

    with _cf.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_find)
        try:
            result = future.result(timeout=20)
        except _cf.TimeoutError:
            future.cancel()
            return jsonify({
                "url": None,
                "source": None,
                "error": "timeout 20s (le finder n'a pas répondu)",
            })

    if not result:
        return jsonify({"url": None, "source": None, "error": "Aucun résultat"})

    return jsonify({
        "url": result.get("url"),
        "source": result.get("source"),
        "confidence": result.get("confidence"),
        "error": result.get("error"),
    })


@app.route("/api/fetch-rp-photos", methods=["POST"])
def api_fetch_rp_photos():
    """Télécharge directement les photos haute résolution depuis RP dans data/uploads/{slug}/."""
    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "slug manquant"}), 400

    rp_path = ROOT / "data" / "rp" / f"{slug}.json"
    if not rp_path.exists():
        return jsonify({"error": "Hôtel non scrapé. Scrape RP d'abord."}), 400

    with open(rp_path) as f:
        rp_data = json.load(f)

    image_urls = rp_data.get("image_urls", [])
    if not image_urls:
        return jsonify({"error": "Aucune URL d'image dans le scrape RP."}), 400

    hotel_dir = UPLOADS_DIR / slug
    if hotel_dir.exists():
        shutil.rmtree(hotel_dir)
    hotel_dir.mkdir(parents=True)

    # Init progress
    progress.init(f"{slug}_fetch", total=len(image_urls), step="downloading")

    results = []
    for i, url in enumerate(image_urls, 1):
        downloaded = rp_scraper.download_photos([url], hotel_dir, max_photos=1)
        results.extend(downloaded)
        progress.increment(f"{slug}_fetch", current=i, message=f"Photo {i}/{len(image_urls)}")

    progress.finish(f"{slug}_fetch", message="Téléchargement terminé")
    ok_count = sum(1 for r in results if r["status"] == "ok")

    return jsonify({
        "slug": slug,
        "downloaded": ok_count,
        "total": len(image_urls),
        "files": [
            {"name": r["filename"], "size": r["size"], "status": r["status"]}
            for r in results
        ],
    })


@app.route("/api/fetch-all-sources", methods=["POST"])
def api_fetch_all_sources():
    """Orchestre la récupération depuis les sources cochées (officiel/booking/rp/
    expedia). Dédup pHash inter-sources. Stocke tout dans data/uploads/{slug}/.

    Body : {
        slug,
        booking_url?,        # URL Booking (saisie en Étape 1)
        official_url?,       # URL site officiel pré-saisie (skip DDG/Gemini si fournie)
        expedia_url?,        # idem Expedia
        rp_url?,             # idem ResortPass (sinon utilise rp_data.image_urls)
        sources: {official, booking, rp, expedia}
    }
    """
    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or "").strip()
    booking_url = (payload.get("booking_url") or "").strip()
    official_url_override = (payload.get("official_url") or "").strip() or None
    expedia_url_override = (payload.get("expedia_url") or "").strip() or None
    rp_url_override = (payload.get("rp_url") or "").strip() or None
    sources = payload.get("sources") or {"official": True, "booking": True, "rp": True, "expedia": True}

    if not slug:
        return jsonify({"error": "slug manquant (scrape RP d'abord)"}), 400

    rp_path = ROOT / "data" / "rp" / f"{slug}.json"
    if not rp_path.exists():
        return jsonify({"error": "Hôtel RP non scrapé. Étape 1 d'abord."}), 400
    with open(rp_path) as f:
        rp_data = json.load(f)

    progress.init(f"{slug}_fetch_all", total=100, step="starting")

    hotel_dir = UPLOADS_DIR / slug
    if hotel_dir.exists():
        shutil.rmtree(hotel_dir)
    hotel_dir.mkdir(parents=True)

    sources_summary = {}  # par source : {url, photos_downloaded, error?}
    all_files = []  # liste cumulative des fichiers téléchargés (avec source taggée)

    # ━━━ SOURCE 1 : Site officiel ━━━
    if sources.get("official"):
        # Si URL fournie en Étape 1, on l'utilise direct (skip DDG/Gemini)
        if official_url_override:
            progress.update(f"{slug}_fetch_all", step="official_finding", current=5, total=100,
                            message=f"Site officiel : URL pré-saisie {official_url_override[:60]}…")
            site_result = {"url": official_url_override, "source": "manual", "confidence": "high"}
        else:
            progress.update(f"{slug}_fetch_all", step="official_finding", current=5, total=100,
                            message="Recherche site officiel via DuckDuckGo + Gemini…")
            site_result = hotel_site_finder.find_hotel_site(
                name=rp_data.get("name", ""),
                city=rp_data.get("city", ""),
                country=rp_data.get("country", ""),
            )
        if site_result and site_result.get("url"):
            site_url = site_result["url"]
            progress.update(f"{slug}_fetch_all", step="official_extracting", current=15, total=100,
                            message=f"Extraction galerie depuis {site_url}…")
            extract = hotel_gallery_extractor.extract_gallery_photos(site_url, max_total_photos=80)
            urls = extract.get("photos") or []
            results = booking_scraper.download_photos_to_dir(urls, hotel_dir / "_official_temp", max_photos=80) if urls else []
            ok = [r for r in results if r["status"] == "ok"]
            # Renomme avec préfixe official_
            for i, r in enumerate(ok, 1):
                old = hotel_dir / "_official_temp" / r["filename"]
                new_name = f"official_{i:03d}_{r['filename'].split('_', 2)[-1] if '_' in r['filename'] else r['filename']}"
                new_path = hotel_dir / new_name
                if old.exists():
                    old.rename(new_path)
                    all_files.append({"name": new_name, "size": r["size"], "source": "official"})
            (hotel_dir / "_official_temp").rmdir() if (hotel_dir / "_official_temp").exists() and not list((hotel_dir / "_official_temp").iterdir()) else None
            sources_summary["official"] = {
                "url": site_url,
                "title": site_result.get("title"),
                "photos_downloaded": len(ok),
                "photos_found": len(urls),
                "pages_visited": len(extract.get("pages_visited") or []),
                "engine": extract.get("engine"),  # "playwright_stealth" ou "patchright"
                "fallback_from": extract.get("fallback_from"),
                "blocked_signal": extract.get("blocked_signal"),
            }
            if not urls and extract.get("blocked_signal"):
                sources_summary["official"]["error"] = (
                    f"🛡️ Site bloqué ({extract['blocked_signal']}). "
                    "Même patchright n'a pas pu extraire de photo."
                )
        else:
            sources_summary["official"] = {
                "url": None,
                "error": (site_result or {}).get("error", "site officiel introuvable"),
                "url_attempted": (site_result or {}).get("url_attempted"),
                "photos_downloaded": 0,
            }

    # ━━━ SOURCE 2 : Booking ━━━
    if sources.get("booking") and booking_url:
        progress.update(f"{slug}_fetch_all", step="booking", current=45, total=100,
                        message="Scraping Booking (Playwright stealth)…")
        try:
            urls = booking_scraper.scrape_booking_photos(booking_url)
            results = booking_scraper.download_photos_to_dir(urls, hotel_dir / "_booking_temp", max_photos=120)
            ok = [r for r in results if r["status"] == "ok"]
            for i, r in enumerate(ok, 1):
                old = hotel_dir / "_booking_temp" / r["filename"]
                new_name = f"booking_{i:03d}_{r['filename'].split('_', 2)[-1] if '_' in r['filename'] else r['filename']}"
                new_path = hotel_dir / new_name
                if old.exists():
                    old.rename(new_path)
                    all_files.append({"name": new_name, "size": r["size"], "source": "booking"})
            (hotel_dir / "_booking_temp").rmdir() if (hotel_dir / "_booking_temp").exists() and not list((hotel_dir / "_booking_temp").iterdir()) else None
            sources_summary["booking"] = {"url": booking_url, "photos_downloaded": len(ok), "photos_found": len(urls)}
        except Exception as e:
            sources_summary["booking"] = {"url": booking_url, "error": str(e)[:200], "photos_downloaded": 0}

    # ━━━ SOURCE 3 : RP photos ━━━
    # Deux cas :
    #   (a) Workflow Booking-first : rp_data vient de Booking et n'a PAS d'image_urls.
    #       → On scrape l'URL RP fournie (rp_url_override) avec rp_scraper.scrape().
    #   (b) Workflow RP-first (legacy) : rp_data vient de /api/scrape (RP direct)
    #       et contient déjà image_urls. → On les utilise direct.
    if sources.get("rp"):
        progress.update(f"{slug}_fetch_all", step="rp", current=75, total=100,
                        message="Téléchargement photos ResortPass…")
        urls = []
        rp_used_url = None
        if rp_url_override and rp_url_override.startswith("https://www.resortpass.com/hotels/"):
            # Workflow Booking-first : on doit aller chercher les image_urls
            # depuis la fiche RP, parce que rp_data (= Booking) ne les a pas.
            progress.update(f"{slug}_fetch_all", step="rp_scraping", current=72, total=100,
                            message=f"Scrape fiche ResortPass {rp_url_override[:60]}…")
            try:
                rp_scrape = rp_scraper.scrape(rp_url_override)
                urls = rp_scrape.get("image_urls") or []
                rp_used_url = rp_url_override
            except Exception as e:
                sources_summary["rp"] = {
                    "url": rp_url_override,
                    "error": f"Scrape RP échoué : {str(e)[:150]}",
                    "photos_downloaded": 0,
                }
                urls = []
        if not urls and rp_data.get("image_urls"):
            # Fallback workflow RP-first : on utilise les image_urls de rp_data
            urls = rp_data.get("image_urls") or []
            rp_used_url = rp_data.get("rp_url")

        if urls:
            results = rp_scraper.download_photos(urls, hotel_dir / "_rp_temp")
            ok = [r for r in results if r["status"] == "ok"]
            for i, r in enumerate(ok, 1):
                old = hotel_dir / "_rp_temp" / r["filename"]
                new_name = f"rp_{i:03d}_{r['filename'].split('_', 2)[-1] if '_' in r['filename'] else r['filename']}"
                new_path = hotel_dir / new_name
                if old.exists():
                    old.rename(new_path)
                    all_files.append({"name": new_name, "size": r["size"], "source": "rp"})
            tmp = hotel_dir / "_rp_temp"
            if tmp.exists() and not list(tmp.iterdir()):
                tmp.rmdir()
            sources_summary["rp"] = {
                "url": rp_used_url,
                "photos_downloaded": len(ok),
                "photos_found": len(urls),
            }
            # ━━ Persiste les URLs RP dans rp_data.json (Martin 14/05/2026) ━━
            # Sans ça, /api/run lit rp_data.image_urls=[] et le front affiche
            # "URL ResortPass non trouvée" alors qu'on a bien scrapé les photos.
            # Workflow Booking-first : rp_data vient de Booking et n'a PAS ces champs.
            try:
                rp_data["image_urls"] = urls
                rp_data["rp_url"] = rp_used_url
                rp_data["image_count"] = len(urls)
                with open(rp_path, "w") as f:
                    json.dump(rp_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[rp persist] failed: {e}")
        elif "rp" not in sources_summary:
            sources_summary["rp"] = {
                "url": None,
                "error": "Aucune URL RP fournie et rp_data sans image_urls",
                "photos_downloaded": 0,
            }

    # ━━━ SOURCE 4 : Expedia ━━━
    # URL trouvée automatiquement par DuckDuckGo + Gemini fallback (comme site officiel).
    # Risques connus :
    #   1. Gemini peut halluciner l'ID numérique (h{ID}) → URL inexistante.
    #   2. Expedia détecte Playwright → page "Bot or Not?" → 0 photo malgré URL valide.
    # Dans les deux cas on log proprement, le pipeline continue.
    if sources.get("expedia"):
        if expedia_url_override:
            progress.update(f"{slug}_fetch_all", step="expedia_finding", current=70, total=100,
                            message=f"Expedia : URL pré-saisie {expedia_url_override[:60]}…")
            expedia_result = {"url": expedia_url_override, "source": "manual", "confidence": "high"}
        else:
            progress.update(f"{slug}_fetch_all", step="expedia_finding", current=70, total=100,
                            message="Recherche URL Expedia via DuckDuckGo + Gemini…")
            expedia_result = expedia_finder.find_expedia_url(
                name=rp_data.get("name", ""),
                city=rp_data.get("city", ""),
                country=rp_data.get("country", ""),
            )
        if expedia_result and expedia_result.get("url"):
            expedia_url = expedia_result["url"]
            progress.update(f"{slug}_fetch_all", step="expedia_scraping", current=73, total=100,
                            message=f"Scraping Expedia (Playwright stealth)…")
            try:
                urls = expedia_scraper.scrape_expedia_photos(expedia_url)
                results = expedia_scraper.download_photos_to_dir(urls, hotel_dir / "_expedia_temp", max_photos=80)
                ok = [r for r in results if r["status"] == "ok"]
                for i, r in enumerate(ok, 1):
                    old = hotel_dir / "_expedia_temp" / r["filename"]
                    base = r["filename"].split("_", 2)[-1] if "_" in r["filename"] else r["filename"]
                    new_name = f"expedia_{i:03d}_{base}"
                    new_path = hotel_dir / new_name
                    if old.exists():
                        old.rename(new_path)
                        all_files.append({"name": new_name, "size": r["size"], "source": "expedia"})
                tmp = hotel_dir / "_expedia_temp"
                if tmp.exists() and not list(tmp.iterdir()):
                    tmp.rmdir()
                sources_summary["expedia"] = {
                    "url": expedia_url,
                    "confidence": expedia_result.get("confidence"),
                    "photos_downloaded": len(ok),
                    "photos_found": len(urls),
                }
                if len(urls) == 0:
                    sources_summary["expedia"]["error"] = (
                        "0 photo extraite — DOM Expedia probablement changé ou "
                        "URL invalide (galerie non détectée)"
                    )
            except expedia_scraper.ExpediaBotBlocked as e:
                sources_summary["expedia"] = {
                    "url": expedia_url,
                    "error": (
                        "🛡️ Expedia bloque le scraping anonyme (page 'Bot or Not?'). "
                        "URL OK mais inaccessible sans proxy résidentiel ou API tierce."
                    ),
                    "bot_blocked": True,
                    "photos_downloaded": 0,
                }
            except Exception as e:
                sources_summary["expedia"] = {
                    "url": expedia_url,
                    "error": str(e)[:200],
                    "photos_downloaded": 0,
                }
        else:
            sources_summary["expedia"] = {
                "url": None,
                "error": (expedia_result or {}).get("error", "URL Expedia introuvable via Gemini"),
                "photos_downloaded": 0,
            }

    # ━━━ Upscale Lanczos pour photos trop petites POST-DOWNLOAD ━━━
    # N1_ingestion d'analyze.py rejette tout ce qui est < 500×320. Plutôt que de
    # supprimer les thumbnails (ex: hotel_gallery_extractor qui chope du 448×336
    # via un srcset dégénéré), on les upscale Lanczos jusqu'à passer le seuil.
    # Cf. Martin 13/05/2026 : "ne supprime pas, agrandis comme avec Lanczos x2".
    # La photo upscalée n'a pas plus de détail réel mais peut être analysée par
    # Gemini Vision et utilisée comme source.
    progress.update(f"{slug}_fetch_all", step="upscale_min_size", current=90, total=100,
                    message="Upscale Lanczos photos trop petites (< 500×320)…")
    MIN_W, MIN_H = 500, 320
    from PIL import Image as _PILImage
    upscaled_count = 0
    corrupted_count = 0
    for f in all_files:
        p = hotel_dir / f["name"]
        if not p.exists():
            continue
        try:
            with _PILImage.open(p) as _im:
                w, h = _im.size
                if w < MIN_W or h < MIN_H:
                    # Calcule le facteur d'upscale minimal pour passer les 2 seuils
                    # (puis arrondi vers le haut + marge confort 1.2x).
                    factor_w = MIN_W / w if w < MIN_W else 1.0
                    factor_h = MIN_H / h if h < MIN_H else 1.0
                    factor = max(factor_w, factor_h) * 1.2  # marge confort
                    new_w, new_h = int(w * factor), int(h * factor)
                    upscaled = _im.convert("RGB").resize(
                        (new_w, new_h), _PILImage.Resampling.LANCZOS
                    )
                    f["original_size"] = [w, h]
                    f["upscaled_size"] = [new_w, new_h]
                    f["upscaled_from_thumbnail"] = True
                    # Sauve avec quality 92 (sweet spot poids/qualité)
                    upscaled.save(p, "JPEG", quality=92, optimize=True)
                    upscaled_count += 1
        except Exception as e:
            # Image corrompue → supprime
            try:
                p.unlink()
            except Exception:
                pass
            corrupted_count += 1
    if upscaled_count > 0:
        print(f"[upscale_min_size] {upscaled_count} photos upscalées Lanczos (étaient < {MIN_W}×{MIN_H})")
    if corrupted_count > 0:
        print(f"[upscale_min_size] {corrupted_count} photos corrompues supprimées")
        # Retire les corrompues de all_files
        all_files = [f for f in all_files if (hotel_dir / f["name"]).exists()]

    # ━━━ Dédup pHash inter-sources ━━━
    progress.update(f"{slug}_fetch_all", step="dedup", current=92, total=100,
                    message="Dédup pHash inter-sources…")
    paths = sorted([Path(hotel_dir / f["name"]) for f in all_files])
    if len(paths) > 1:
        clusters = dedup_angles.find_duplicate_clusters(paths, threshold=16)
        kept, dropped = dedup_angles.select_best_per_cluster(clusters)
        kept_set = {p.resolve() for p in kept}
        # Supprime les doublons (priorité : official > booking > expedia > rp)
        priority = {"official": 4, "booking": 3, "expedia": 2, "rp": 1}
        files_by_path = {(hotel_dir / f["name"]).resolve(): f for f in all_files}
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            # Prend celui de plus haute priorité comme "winner"
            cluster_files = [files_by_path[p.resolve()] for p in cluster if p.resolve() in files_by_path]
            cluster_files.sort(key=lambda f: priority.get(f["source"], 0), reverse=True)
            winner = cluster_files[0]
            for loser in cluster_files[1:]:
                p = (hotel_dir / loser["name"]).resolve()
                try:
                    p.unlink()
                except Exception:
                    pass
        # Recompose la liste finale après suppression
        all_files = [f for f in all_files if (hotel_dir / f["name"]).exists()]

    progress.finish(f"{slug}_fetch_all",
                    message=f"{len(all_files)} photos prêtes (sources: {sum(1 for s in sources_summary.values() if s.get('photos_downloaded', 0) > 0)})")

    return jsonify({
        "slug": slug,
        "downloaded": len(all_files),
        "sources_summary": sources_summary,
        "files": all_files,
    })


@app.route("/api/fetch-booking-photos", methods=["POST"])
def api_fetch_booking_photos():
    """Scrape les photos haute résolution depuis Booking via Playwright headless.
    Stocke dans data/uploads/{slug}/ (slug de RP). Demande RP scrapé d'abord."""
    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or "").strip()
    booking_url = (payload.get("booking_url") or "").strip()

    if not slug:
        return jsonify({"error": "slug manquant (scrape RP d'abord)"}), 400
    if not booking_url.startswith("https://www.booking.com/hotel/"):
        return jsonify({"error": "URL Booking invalide (doit commencer par https://www.booking.com/hotel/)"}), 400

    # Étape 1 : scrape (~15-30s avec Playwright)
    progress.init(f"{slug}_fetch_booking", total=100, step="scraping_booking")
    progress.update(f"{slug}_fetch_booking", message="Lance Playwright + ouvre la galerie Booking...")

    try:
        photo_urls = booking_scraper.scrape_booking_photos(booking_url)
    except Exception as e:
        progress.finish(f"{slug}_fetch_booking", message=f"Erreur scraping: {e}")
        return jsonify({"error": f"Échec scraping Booking : {e}"}), 500

    if not photo_urls:
        progress.finish(f"{slug}_fetch_booking", message="Aucune photo trouvée")
        return jsonify({"error": "Aucune photo trouvée sur la fiche Booking. URL correcte ?"}), 400

    # Étape 2 : download dans data/uploads/{slug}/
    progress.update(f"{slug}_fetch_booking", step="downloading", current=0, total=len(photo_urls), message=f"Téléchargement de {len(photo_urls)} photos...")

    hotel_dir = UPLOADS_DIR / slug
    if hotel_dir.exists():
        shutil.rmtree(hotel_dir)
    hotel_dir.mkdir(parents=True)

    results = []
    for i, url in enumerate(photo_urls, 1):
        downloaded = booking_scraper.download_photos_to_dir([url], hotel_dir)
        results.extend(downloaded)
        progress.increment(f"{slug}_fetch_booking", current=i, total=len(photo_urls),
                           message=f"Photo {i}/{len(photo_urls)}")

    progress.finish(f"{slug}_fetch_booking", message=f"{len([r for r in results if r['status'] == 'ok'])} photos prêtes")
    ok_count = sum(1 for r in results if r["status"] == "ok")

    return jsonify({
        "slug": slug,
        "downloaded": ok_count,
        "total": len(photo_urls),
        "source": "booking",
        "files": [
            {"name": r["filename"], "size": r["size"], "status": r["status"]}
            for r in results
        ],
    })


@app.route("/api/spend")
def api_spend():
    """Cumul des dépenses Gemini + Nano Banana sur tous les runs."""
    return jsonify(spend.read_summary())


@app.route("/api/progress")
def api_progress():
    """Lit l'état d'une opération en cours (poll par le front).
    Toujours 200 même si pas de state ou erreur transitoire — laisse le client retry."""
    slug = request.args.get("slug", "").strip()
    if not slug:
        return jsonify({"error": "slug manquant"}), 400
    try:
        state = progress.read(slug)
    except Exception as e:
        # Filet de sécurité ultime — on ne crashe jamais le poll côté serveur
        return jsonify({"step": "transient_error", "current": 0, "total": 0, "done": False, "_err": str(e)[:200]}), 200
    if not state:
        return jsonify({"step": "none", "current": 0, "total": 0, "done": False}), 200
    return jsonify(state)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Upload de photos pour un hôtel. Form-data : slug + files[]."""
    slug = request.form.get("slug", "").strip()
    if not slug:
        return jsonify({"error": "slug manquant"}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "aucun fichier"}), 400

    hotel_dir = UPLOADS_DIR / slug
    # On nettoie pour ne pas mélanger des batchs précédents
    if hotel_dir.exists():
        shutil.rmtree(hotel_dir)
    hotel_dir.mkdir(parents=True)

    saved = []
    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        dest = hotel_dir / f.filename
        f.save(dest)
        saved.append({"name": f.filename, "size": dest.stat().st_size})

    return jsonify({"slug": slug, "uploaded": len(saved), "files": saved})


@app.route("/api/run", methods=["POST"])
def api_run():
    """Pipeline analyse complet :
    1. Charge le RP scrapé
    2. Liste les photos uploadées
    3. Analyse Gemini en parallèle (5 workers)
    4. Dédup d'angles (pHash)
    5. Coverage vs shopping list RP
    6. Renvoie tout pour affichage front
    """
    pipeline_started_at = time.time()
    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "slug manquant"}), 400

    rp_path = ROOT / "data" / "rp" / f"{slug}.json"
    if not rp_path.exists():
        return jsonify({"error": "Hôtel non scrapé. Scrape RP d'abord."}), 400

    with open(rp_path) as f:
        rp_data = json.load(f)

    hotel_dir = UPLOADS_DIR / slug
    if not hotel_dir.exists():
        return jsonify({"error": "Aucune photo uploadée pour cet hôtel."}), 400

    photo_paths_all = sorted(p for p in hotel_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if not photo_paths_all:
        return jsonify({"error": "Dossier upload vide."}), 400

    # Filtrage manuel : photos désélectionnées par l'utilisateur dans la grille
    deselected = set((payload or {}).get("deselected") or [])
    photo_paths = [p for p in photo_paths_all if p.name not in deselected]
    if not photo_paths:
        return jsonify({"error": "Toutes les photos désélectionnées. Coche au moins une photo."}), 400

    # === Mode reprise (économise du temps sur tests itératifs) ===
    # resume_from ∈ {"scrape" (défaut, tout refaire), "selection" (skip Gemini), "postprocess" (skip enhance)}
    resume_from = (payload or {}).get("resume_from") or "scrape"
    use_analysis_cache = resume_from in ("selection", "postprocess")
    use_enhance_cache = resume_from == "postprocess"

    # === Étape 1 : Analyse Gemini en parallèle ===
    try:
        model = analyze.get_model()
    except Exception as e:
        return jsonify({"error": f"Gemini non configuré : {e}"}), 500

    progress.init(f"{slug}_analyze", total=len(photo_paths), step="analyzing")

    # ━━ Mode postprocess (Martin 14/05/2026) : signale immédiatement que
    # l'analyse est "en cours / depuis cache" pour que le front voie l'étape
    # même si elle se termine en <500ms (cache hit instantané).
    if use_analysis_cache:
        progress.update(
            f"{slug}_analyze", step="analyzing",
            current=0, total=len(photo_paths),
            message=f"📂 Chargement {len(photo_paths)} analyses depuis cache…",
        )

    def _progress_cb(done, total, last_filename):
        # Récupère le payload du dernier traité pour cumuler le coût
        # Note : on n'a pas accès direct au payload ici, on cumule a posteriori après le batch
        msg = f"Photo {done}/{total} : {last_filename}"
        if use_analysis_cache:
            msg = f"📂 Cache {done}/{total} : {last_filename}"
        progress.increment(f"{slug}_analyze", current=done, message=msg)

    analyses_dir = ROOT / "data" / "analyses" / slug
    # parallel=8 : Gemini Vision Flash a un quota tier paid 1 généreux (~2000 RPM).
    # Avec ~100 photos, on passe d'~3min séquentiel (3 workers) à <1min.
    # Override possible via env ANALYZE_WORKERS pour debug rate-limit.
    analyze_workers = int(os.environ.get("ANALYZE_WORKERS", "8"))
    analyses = analyze.analyze_batch(
        photo_paths, model, parallel=analyze_workers,
        output_dir=analyses_dir,
        progress_callback=_progress_cb,
        use_cache=use_analysis_cache,
    )

    # Cumul tokens & coût
    # Important : on n'incrémente PAS pour les analyses chargées du cache
    # (sinon on facture des tokens qui n'ont pas été consommés ce run-ci).
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd = 0.0
    analyses_from_cache = 0
    failed_analyses = []
    for a in analyses:
        if a.get("_from_cache"):
            analyses_from_cache += 1
            continue
        trace = a.get("trace", [])
        n1 = next((t for t in trace if t.get("node") == "N1_ingestion"), None)
        n2 = next((t for t in trace if t.get("node") == "N2_analyze_gemini"), None)
        if n2 and n2.get("result") == "pass":
            u = n2.get("usage", {})
            total_input_tokens += u.get("input_tokens", 0)
            total_output_tokens += u.get("output_tokens", 0)
            total_cost_usd += u.get("cost_usd", 0)
        else:
            # ━━ Diagnostic d'erreur robuste ━━
            # Cas 1 : N2 a un champ error → erreur d'analyse Gemini (clé, quota, modèle)
            # Cas 2 : N2 absent + N1 a une error → l'image n'a pas pu être chargée
            # Cas 3 : N1 reject (image trop petite/corrompue) → ingestion_reason
            # Cas 4 : aucun des deux → bug structurel (analyze a planté avant tracing)
            err = "unknown"
            stage = "?"
            if n2 and n2.get("error"):
                err = n2["error"]
                stage = "N2_gemini"
            elif n1 and n1.get("error"):
                err = n1["error"]
                stage = "N1_ingestion"
            elif n1 and n1.get("result") == "reject":
                err = "image rejetée à l'ingestion (trop petite, corrompue, ou format non supporté)"
                stage = "N1_reject"
            elif not trace:
                err = "trace vide — analyze a probablement crashé avant le tracing"
                stage = "no_trace"
            failed_analyses.append({
                "filename": a.get("input", {}).get("filename"),
                "stage": stage,
                "error": err,
            })

    # ━━ Garde : si trop d'échecs Gemini, on abort avec message explicite ━━
    # Cas typique : projet en spend cap dépassé → 100% des photos échouent. Sans
    # cette garde, on continuait dans dedup/coverage/ordering avec des analyses
    # vides et le pipeline crashait plus loin sans contexte clair.
    fresh_attempted = len(analyses) - analyses_from_cache
    if fresh_attempted > 0:
        fail_rate = len(failed_analyses) / fresh_attempted
        if fail_rate >= 0.3:
            sample_err = failed_analyses[0]["error"] if failed_analyses else "?"
            # Détection du spend cap → message ultra-clair pour Martin
            is_spend_cap = "spend" in sample_err.lower() or "spending" in sample_err.lower() or "billing" in sample_err.lower()
            if is_spend_cap:
                user_msg = (
                    f"❌ Spend cap Gemini API dépassé ({len(failed_analyses)}/{fresh_attempted} photos en erreur). "
                    f"Va sur https://ai.studio/spend pour lever le plafond, puis relance."
                )
            else:
                sample_stage = failed_analyses[0].get("stage", "?")
                user_msg = (
                    f"❌ {len(failed_analyses)}/{fresh_attempted} analyses Gemini en erreur "
                    f"(étape : {sample_stage}). Première erreur : {sample_err[:200]}"
                )
            progress.update(
                f"{slug}_analyze",
                step="error",
                done=True,
                message=user_msg,
            )
            return jsonify({
                "error": user_msg,
                "failed_count": len(failed_analyses),
                "fresh_attempted": fresh_attempted,
                "sample_errors": [f["error"][:200] for f in failed_analyses[:3]],
            }), 502

    progress.update(
        f"{slug}_analyze",
        cost_usd_cumulated=round(total_cost_usd, 6),
        input_tokens_cumulated=total_input_tokens,
        output_tokens_cumulated=total_output_tokens,
        step="dedup",
    )

    # === Étape 2 : Dédup d'angles ===
    # 2a) pHash strict (seuil 12) → clusters certains
    clusters = dedup_angles.find_duplicate_clusters(photo_paths, threshold=12)

    # 2b) Calcul des distances pHash pour TOUTES les paires en zone grise (13-28)
    #     → ces paires seront vérifiées par Gemini Vision (sémantique)
    import imagehash as _ih
    from PIL import Image as _Image
    phash_distances: dict[tuple[str, str], int] = {}
    hashes_by_name = {}
    for p in photo_paths:
        try:
            with _Image.open(p) as im:
                hashes_by_name[p.name] = _ih.phash(im)
        except Exception:
            continue
    names = list(hashes_by_name.keys())
    for i, n1 in enumerate(names):
        for n2 in names[i + 1:]:
            d = hashes_by_name[n1] - hashes_by_name[n2]
            if dedup_vlm.GREY_ZONE_MIN <= d <= dedup_vlm.GREY_ZONE_MAX:
                phash_distances[(n1, n2)] = d
    # Score par photo (pour choisir la meilleure de chaque cluster)
    scores = {}
    by_path = {Path(a["input"]["path_absolute"]): a for a in analyses}
    for path, a in by_path.items():
        analysis = a.get("analysis") or {}
        ps = (analysis.get("emotional") or {}).get("pillar_scores") or {}
        scores[path] = ps.get("freedom", 0) + ps.get("wellness", 0) + ps.get("experience", 0)

    kept, dropped = dedup_angles.select_best_per_cluster(clusters, scores)
    kept_set = {p.resolve() for p in kept}

    # 2c) Dédup sémantique VLM sur les paires en zone grise (Gemini Vision)
    # On a maintenant les analyses Gemini → on peut pré-filtrer puis appeler VLM
    # ━━ Mode postprocess : on skip car le pack final est déjà figé (enhanced/*.jpg)
    #    et on ne ré-affecte pas la sélection. C'est juste un appel Gemini onéreux pour rien.
    analyses_by_filename = {a["input"]["filename"]: a.get("analysis") for a in analyses}
    vlm_dedup_results = []
    if phash_distances and not use_enhance_cache:
        # Indique au front qu'on entre dans la phase dedup_vlm (sinon il reste figé sur "100% analyze")
        progress.update(
            f"{slug}_analyze",
            step="dedup_vlm",
            current=0,
            total=min(len(phash_distances), dedup_vlm.MAX_PAIRS_TO_CHECK),
            message=f"Dédup sémantique VLM ({min(len(phash_distances), dedup_vlm.MAX_PAIRS_TO_CHECK)} paires à vérifier)…",
        )

        def _vlm_cb(done, total, label):
            progress.update(
                f"{slug}_analyze",
                step="dedup_vlm",
                current=done,
                total=total,
                message=f"VLM dedup {done}/{total} : {label}",
            )

        try:
            vlm_dedup_results = dedup_vlm.find_semantic_duplicates(
                photo_paths, analyses_by_filename, phash_distances,
                progress_callback=_vlm_cb,
            )
            # Pour chaque paire same_scene=True : on garde la meilleure (best score), l'autre est duplicate
            for pair in vlm_dedup_results:
                if not pair.get("same_scene"):
                    continue
                f1, f2 = pair["a"], pair["b"]
                p1 = next((p for p in photo_paths if p.name == f1), None)
                p2 = next((p for p in photo_paths if p.name == f2), None)
                if not (p1 and p2):
                    continue
                # Score : on garde celle qui a déjà été marquée kept par pHash, sinon best score
                p1_kept = p1.resolve() in kept_set
                p2_kept = p2.resolve() in kept_set
                if p1_kept and not p2_kept:
                    kept_set.discard(p2.resolve())
                elif p2_kept and not p1_kept:
                    kept_set.discard(p1.resolve())
                else:
                    # Les 2 sont kept (ou les 2 sont dropped) — on choisit par score
                    s1 = scores.get(p1, 0)
                    s2 = scores.get(p2, 0)
                    if s1 >= s2:
                        kept_set.discard(p2.resolve())
                    else:
                        kept_set.discard(p1.resolve())
        except Exception as e:
            # Si VLM dedup échoue, on continue avec juste pHash
            print(f"VLM dedup error (non-blocking): {e}")

    # Tag chaque analyse avec son statut dédup final
    for a in analyses:
        ap = Path(a["input"]["path_absolute"])
        a["dedup_status"] = "kept" if ap in kept_set else "duplicate_dropped"

    # === Étape 3 : Coverage vs shopping list RP ===
    # On utilise SEULEMENT les photos kept (post-dédup) pour le coverage
    kept_analyses = [a for a in analyses if a["dedup_status"] == "kept"]
    cov_initial = coverage_mod.compute_coverage(rp_data, kept_analyses)

    # ━━ 3b) Re-vérification amenity_dominance via 2e passe Gemini Vision (Q4 Martin) ━━
    # On re-vérifie les top-3 candidats de chaque bucket amenity. Si Gemini constate qu'une
    # photo est en réalité un close-up lifestyle (pas l'amenity comme sujet), on plombe
    # sa dominance → la photo est rétrogradée dans le tri suivant.
    # ━━ Mode postprocess : on skip (le pack est déjà figé sur disque, c'est juste un appel
    #    Gemini onéreux qui ne change rien à la sortie multi-format/slowmo).
    verifier_results = []
    verifier_cost_usd = 0.0
    if not use_enhance_cache:
        progress.update(f"{slug}_analyze", step="amenity_verify",
                        message="Vérification amenity (2e passe Gemini sur toutes les photos amenity)…")
        try:
            # n_per_bucket=None : on vérifie TOUTES les photos d'un bucket amenity, pas juste top-3
            # (Gemini hallucine régulièrement → on doit tout vérifier pour ne pas laisser passer
            # un closeup bikini scoré 240/300 par Gemini).
            verifier_workers = int(os.environ.get("AMENITY_VERIFIER_WORKERS", "8"))
            verifier_results = amenity_verifier.verify_top_candidates(
                kept_analyses, cov_initial, n_per_bucket=None, parallel=verifier_workers,
            )
            verifier_cost_usd = round(sum(r.get("cost_usd", 0) for r in verifier_results), 6)
            print(f"[amenity_verifier] {len(verifier_results)} photos vérifiées, "
                  f"{sum(1 for r in verifier_results if not r.get('is_focused'))} rétrogradées, "
                  f"coût ${verifier_cost_usd}")
        except Exception as e:
            verifier_results = []
            verifier_cost_usd = 0.0
            import traceback
            print(f"[amenity_verifier] ERROR (non-blocking): {e}")
            traceback.print_exc()
    else:
        print("[postprocess] skip amenity_verifier + VLM dedup (use_enhance_cache=True)")

    # Re-compute coverage avec les dominances corrigées
    cov = coverage_mod.compute_coverage(rp_data, kept_analyses)

    # === Étape 4 : Sélection top-N + Ordering + Retouche ===
    by_filename = {a["input"]["filename"]: a for a in analyses}

    # 4.a Collecte des photos retenues par le coverage (kept_estimate=True dans by_category)
    selected_filenames = set()
    selection_meta = {}  # filename → {category, rank, score}
    photo_targets = {}   # filename → list[target_category] (multi-tagging)
    for cat, info in cov["by_category"].items():
        for rank, ph in enumerate(info["photos"], 1):
            if ph.get("selected_in_top"):
                selected_filenames.add(ph["filename"])
                # Une photo peut être sélectionnée dans plusieurs catégories (multi-tagging) ;
                # on enregistre la 1ère catégorie atteinte, et on accumule les tags.
                if ph["filename"] not in selection_meta:
                    selection_meta[ph["filename"]] = {
                        "category": cat,
                        "rank": rank,
                        "score_brand_total": ph.get("score_brand_total"),
                        "score_components": ph.get("score_components"),
                    }
                photo_targets.setdefault(ph["filename"], []).append(cat)

    # 4.a-ter : Photos BONUS lifestyle (focus humains qualifiés) — selection_meta synthétique
    # pour qu'elles aient une justification dans l'UI ("bonus <amenity>")
    for b in (cov.get("bonus_lifestyle") or []):
        fname = b["filename"]
        if fname in selection_meta:
            continue  # déjà placée dans le pack normal, pas besoin de doubler
        # Le bonus_lifestyle_entries dans cov contient l'entry complet — on prend le score_components live
        bonus_entry = next((e for e in (cov.get("_bonus_lifestyle_entries") or []) if e["entry"]["input"]["filename"] == fname), None)
        sc = coverage_mod.compute_score_components(bonus_entry["entry"]) if bonus_entry else None
        selection_meta[fname] = {
            "category": f"bonus_{b['amenity']}",
            "rank": 0,  # 0 = bonus, pas un rang dans bucket normal
            "score_brand_total": b.get("score"),
            "score_components": sc,
            "is_bonus": True,
            "bonus_amenity": b["amenity"],
        }
        photo_targets.setdefault(fname, []).append(f"bonus_{b['amenity']}")

    # 4.a-bis : Génération full IA pour amenities manquantes (spa, bar — règle métier stricte)
    # En mode postprocess : on skip (le pack est déjà figé, pas de raison de générer une nouvelle photo full-IA)
    generated_photos = []  # liste des photos générées full IA, à injecter dans le pack
    for cat, info in cov["by_category"].items():
        if use_enhance_cache:
            break
        if info["status"] != "missing":
            continue
        if not photo_generator.can_generate(cat):
            continue  # food, pool, cabana etc. : pas de génération autorisée
        # ━ Pas de génération IA pour les buckets activés depuis les photos seulement ━
        # Martin 19/05/2026 : depuis la policy "trust photos > fiche", un bucket peut être
        # activé même sans mention dans la fiche RP. Dans ce cas, on a déjà ≥1 photo réelle
        # pour cette amenity → on s'en contente plutôt que de générer un faux IA, parce que :
        # 1) le risque hallucination Gemini sur l'amenity reste (cabanas vs lounges)
        # 2) si on génère un faux et qu'en vrai l'hôtel n'a pas l'amenity → contenu trompeur
        # On préfère un bucket "missing" honnête à un faux IA contestable.
        if info.get("from_photos_only") or info.get("auto_activated_by_photos"):
            continue
        # On a un manque ET la catégorie peut être générée ET Booking confirme l'amenity
        gen_dir = ROOT / "data" / "uploads" / slug
        gen_path = gen_dir / f"_generated_{cat}.png"
        gen_result = photo_generator.generate_photo_for_amenity(cat, gen_path)
        if gen_result.get("output_path"):
            generated_photos.append({
                "category": cat,
                "filename": gen_path.name,
                "cost_usd": gen_result["cost_usd"],
            })
            # On l'ajoute à la liste des photos analysées avec une analyse minimale
            fake_analysis = {
                "factual": {"category": cat, "human_count": 0, "subjects": [f"{cat} (généré IA)"], "time_of_day": "jour"},
                "technical_hints": {"ambiance": "lumineux-chaud", "palette_alignment": "aligned-warm"},
                "emotional": {"sensations": ["sérénité", "bien-être"], "pillar_scores": {"freedom": 70, "wellness": 90, "experience": 70}},
                "hero_quality": {"score": 60, "is_slot1_worthy": False},
                "issues": [],
                "is_fully_generated": True,
            }
            fake_entry = {
                "input": {
                    "filename": gen_path.name,
                    "path_absolute": str(gen_path.resolve()),
                    "width": 1024, "height": 1024,
                    "file_url": f"file://{gen_path.resolve()}",
                },
                "trace": [{"node": "N0_full_generation", "result": "pass", "duration_ms": gen_result["duration_ms"], "cost_usd": gen_result["cost_usd"]}],
                "analysis": fake_analysis,
                "dedup_status": "kept",
                "is_fully_generated": True,
            }
            analyses.append(fake_entry)
            by_filename[gen_path.name] = fake_entry
            # On l'ajoute dans le bucket coverage approprié
            info["photos"].insert(0, {
                "filename": gen_path.name,
                "score_brand_total": 230,
                "selected_in_top": True,
            })
            info["found"] = info.get("found", 0) + 1
            info["kept_estimate"] = info.get("kept_estimate", 0) + 1
            info["status"] = "ok"
            info["message"] = f"1 photo {cat} générée IA (catégorie initialement manquante)"
            selected_filenames.add(gen_path.name)
            selection_meta[gen_path.name] = {"category": cat, "rank": 1, "score_brand_total": 230}
            photo_targets.setdefault(gen_path.name, []).append(cat)

    # 4.b Ordering : règles brand (slot 1 = meilleure amenity hors intérieur, alternance gens/sans, round-robin)
    selected_analyses_objs = [by_filename[f] for f in selected_filenames if f in by_filename and by_filename[f].get("analysis")]
    # Récupère les entries bonus lifestyle (clé interne du coverage_mod.compute_coverage)
    bonus_entries = cov.get("_bonus_lifestyle_entries") or []
    ordered_pack = ordering.order_final_pack(
        selected_analyses_objs,
        photo_targets,
        target_count_min=12,
        target_count_max=18,
        bonus_lifestyle_entries=bonus_entries,
    )
    final_order = {entry["input"]["filename"]: idx + 1 for idx, entry in enumerate(ordered_pack)}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Étape 4.c — Pass 2 RICH : complète les 4 sections lourdes (safe_zones_for_humans,
    # crop_safe_zones, recommended_crop, slowmo_potential) UNIQUEMENT sur les
    # ~12-18 photos finalistes (ordered_pack). Économise les tokens vs générer
    # ces champs sur les 25 photos initiales (~60% des sections rich tombent à
    # la poubelle si on les met dans la pass1 et qu'elles concernent des photos
    # rejetées par dedup/coverage/ordering).
    #
    # Skip si :
    #   - mode use_analysis_cache : les rich sont déjà dans les JSON cache
    #   - mode use_enhance_cache : pas la peine, on ne re-retouche pas
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Skip pass2 UNIQUEMENT en mode postprocess (use_enhance_cache=True) : enhance et
    # multi_format ne re-tournent pas, les rich ne servent à rien. En mode "selection"
    # (use_analysis_cache=True mais use_enhance_cache=False), on doit produire les rich
    # pour les finalistes qui n'en ont pas encore (sélection user a pu changer).
    rich_paths = []
    if not use_enhance_cache:
        for entry in ordered_pack:
            p_str = entry.get("input", {}).get("path_absolute")
            if not p_str:
                continue
            p = Path(p_str)
            if not p.exists():
                continue
            a = entry.get("analysis") or {}
            # Si une analyse rich a déjà été produite (run précédent partiel), skip
            if a.get("safe_zones_for_humans") and a.get("crop_safe_zones"):
                continue
            rich_paths.append(p)

    if rich_paths:
        progress.update(
            f"{slug}_analyze",
            step="analyzing_rich",
            current=0,
            total=len(rich_paths),
            message=f"Détails IA finalistes (0/{len(rich_paths)})",
        )

        # threading n'est importé que plus bas dans la fonction (bloc enhance) — on
        # l'importe ici localement pour le lock de cumul cost/tokens (rich_cost).
        import threading as _threading
        rich_cost = {"usd": 0.0, "in_tokens": 0, "out_tokens": 0}
        rich_cost_lock = _threading.Lock()

        def _rich_progress_cb(done, total, last_filename, usage=None, error=None, **kw):
            if usage:
                with rich_cost_lock:
                    rich_cost["usd"] += usage.get("cost_usd", 0)
                    rich_cost["in_tokens"] += usage.get("input_tokens", 0)
                    rich_cost["out_tokens"] += usage.get("output_tokens", 0)
            msg = f"Détails IA finalistes {done}/{total} : {last_filename}"
            if error:
                msg = f"⚠️ {last_filename} : {str(error)[:80]}"
            progress.increment(f"{slug}_analyze", current=done, message=msg)

        print(f"🔍 Pass2 RICH sur {len(rich_paths)} photos finalistes (parallel={analyze_workers})")
        rich_results = analyze.analyze_rich_batch(
            rich_paths, model,
            parallel=analyze_workers,
            output_dir=analyses_dir,
            progress_callback=_rich_progress_cb,
        )

        # Cumul tokens/coût dans la même tirelire que pass1
        progress.update(
            f"{slug}_analyze",
            cost_usd_cumulated=round(total_cost_usd + rich_cost["usd"], 6),
            input_tokens_cumulated=total_input_tokens + rich_cost["in_tokens"],
            output_tokens_cumulated=total_output_tokens + rich_cost["out_tokens"],
        )
        total_cost_usd += rich_cost["usd"]
        total_input_tokens += rich_cost["in_tokens"]
        total_output_tokens += rich_cost["out_tokens"]

        # Merge des champs RICH dans by_filename + ordered_pack (les consommateurs
        # — enhance, multi_format, slowmo — lisent dans entry["analysis"] ou
        # by_filename[...].get("analysis"))
        for entry in ordered_pack:
            fname = entry["input"]["filename"]
            stem = Path(fname).stem
            rich_a = rich_results.get(stem)
            if not rich_a:
                continue
            existing = entry.get("analysis") or {}
            for k in ("safe_zones_for_humans", "recommended_crop", "crop_safe_zones", "slowmo_potential"):
                if k in rich_a:
                    existing[k] = rich_a[k]
            entry["analysis"] = existing
            if fname in by_filename:
                by_filename[fname]["analysis"] = existing
        print(f"  ✓ Pass2 mergée dans {len(rich_results)}/{len(rich_paths)} analyses")

    # ━━ Message d'enhance adapté selon le mode (Martin 14/05/2026) ━━
    # En postprocess (cache hit), on signale immédiatement que les retouches sont chargées
    # depuis le cache pour que le front voie l'étape même si elle se termine en <500ms.
    if use_enhance_cache:
        enhance_msg = f"📂 Chargement {len(selected_filenames)} retouches depuis cache…"
    else:
        enhance_msg = f"Retouches IA — 0/{len(selected_filenames)}"
    progress.update(
        f"{slug}_analyze", step="enhancing",
        current=0, total=len(selected_filenames),
        message=enhance_msg,
    )

    enhanced_dir = ROOT / "data" / "output" / slug / "enhanced"
    # ━━ Cleanup enhanced/ au début d'un run COMPLET (resume_from=scrape) ━━
    # Sans ce wipe, des photos enhanced d'anciens runs traînent sur disque et
    # polluent le mode "reprendre depuis postprocess" (on chargerait des
    # photos qui ne sont plus dans la sélection actuelle).
    # On NE supprime PAS en mode replay : le but du replay c'est justement
    # de re-utiliser ces fichiers.
    if not use_analysis_cache and not use_enhance_cache and enhanced_dir.exists():
        import shutil as _shutil
        _shutil.rmtree(enhanced_dir)

    enhanced_results = []
    enhancement_cost_usd = 0.0
    enhancement_input_tokens = 0
    enhancement_output_tokens = 0

    # === Logique alternance STRICTE par position : slot pair = humain forcé ===
    # On force chaque slot pair (#2, #4, #6...) du pack ordonné à avoir un humain.
    # Si la photo en slot pair n'a pas d'humain natif ET est candidate (cabana/transat vide,
    # rooftop, piscine zoomée…), on déclenche un ajout personnage IA.
    # Le slot 1 garde sa règle "meilleure amenity peu importe humain".
    personas_allowed = rp_data.get("personas_allowed") or []
    vibe = rp_data.get("vibe_primary")

    # ━━ Alternance humain STRICTE + persona contextuel par photo ━━
    # Vibe RP supprimée → personas autorisés = liste statique [couples, small_groups, families, solos].
    # Détection contextuelle par photo via le champ Gemini family_friendly_indicators :
    #   - Si la photo contient toboggan / aire de jeux / kids pool / kid menu → on PEUT mettre families
    #   - Si la photo est intimiste (bar adulte, lounge feutré) → on PRÉFÈRE couples / solos
    #   - Sinon défaut neutre → couples (ou solos par rotation)
    # Approche NON CONTRAIGNANTE : si pas de match clair, on tombe sur couples/solos sans risque.
    DEFAULT_PERSONAS = ["couples", "small_groups", "solos"]  # rotation par défaut
    add_character_filenames = set()
    persona_per_filename = {}

    # Construction du pool de personas dispo : on union avec personas_allowed (rétrocompat) si fourni
    pool_personas = list(DEFAULT_PERSONAS)
    if personas_allowed:
        for p in personas_allowed:
            if p not in pool_personas:
                pool_personas.append(p)

    def _pick_contextual_persona(entry: dict, rotation_idx: int) -> str:
        """Choisit le persona pour CETTE photo en regardant ses indicateurs contextuels.

        Règle non-contraignante : si pas d'indicateur clair → fallback sur la rotation par défaut.
        """
        analysis = entry.get("analysis") or {}
        ff = analysis.get("family_friendly_indicators") or {}
        is_family = bool(ff.get("is_family_friendly"))
        # Si la photo est family-friendly ET 'families' est dispo → priorité families
        if is_family and "families" in pool_personas:
            return "families"
        # Sinon rotation par défaut sur DEFAULT_PERSONAS (couples > small_groups > solos)
        order = [p for p in DEFAULT_PERSONAS if p in pool_personas]
        if not order:
            order = pool_personas
        return order[rotation_idx % len(order)] if order else "couples"

    # ━━ Alternance humains : SOUPLE (Martin 12/05/2026 — "donner du mou") ━━
    # Règles actuelles :
    #   1. Slot 1 forcé avec humain SI is_candidate (mais ordering.py garantit en amont
    #      qu'un slot 1 sans humain possible n'est PAS choisi → la photo slot 1 est
    #      toujours human-ready)
    #   2. Slots N>1 : alternance 1/2 — on ajoute SI is_candidate ET slot précédent
    #      n'avait pas d'humain.
    #   3. PAS de garde-fou cascade : si 3+ slots de suite sont non-candidats (ex: 2
    #      aerial + 1 f_and_b), on accepte la cascade plutôt que de forcer un humain
    #      mal placé. Retour Martin : "mieux avoir 0 humain bien fait qu'un humain
    #      mal proportionné".
    #
    # Garanties :
    #   - Slot 1 = TOUJOURS un humain (natif ou ajouté), via ordering.py qui exclut
    #     les candidats non human-ready (aerial, cat exclue, wide non-prominent)
    #   - Slots suivants : alternance opportuniste, pas de force
    prev_will_have_human = False
    persona_idx = 0
    for idx, entry in enumerate(ordered_pack, 1):
        if entry.get("is_bonus_lifestyle"):
            prev_will_have_human = True
            continue
        has_human_native = enhance.has_narrative_human(entry["analysis"])
        is_candidate = enhance.is_add_character_candidate(entry["analysis"])

        will_add = False
        if idx == 1 and not has_human_native and is_candidate:
            will_add = True
        elif not has_human_native and not prev_will_have_human and is_candidate:
            will_add = True

        if will_add:
            fname = entry["input"]["filename"]
            add_character_filenames.add(fname)
            persona_per_filename[fname] = _pick_contextual_persona(entry, persona_idx)
            persona_idx += 1

        prev_will_have_human = has_human_native or will_add

    # === Boucle de retouche : on itère dans l'ORDRE FINAL du pack (slot 1, 2, ...) ===
    # ━━ Parallélisation : 3 workers ThreadPool (I/O-bound — chaque enhance fait des
    #    appels Gemini Image qui dorment pendant l'attente réseau). On préserve
    #    l'ordre final via slot index dans le résultat ; le worker pool peut
    #    retourner dans n'importe quel ordre. Un lock protège le progress + les
    #    cumuls de cost/tokens.
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ENHANCE_WORKERS = int(os.environ.get("ENHANCE_WORKERS", "3"))
    enhance_lock = threading.Lock()
    enhance_progress = {"done": 0}
    enhanced_by_slot: dict[int, dict] = {}

    ordered_filenames = [e["input"]["filename"] for e in ordered_pack]

    def _enhance_one_job(slot: int, filename: str) -> tuple[int, str, dict] | None:
        a = by_filename.get(filename)
        if not a or not a.get("analysis"):
            return None
        input_path = Path(a["input"]["path_absolute"])
        strategy = enhance.pick_strategy(
            a["analysis"],
            personas_allowed=personas_allowed,
            vibe=vibe,
            add_character=(filename in add_character_filenames),
            persona_override=persona_per_filename.get(filename),
            photo_filename=filename,
            image_path=input_path,
        )
        existing_enhanced = enhanced_dir / filename
        if use_enhance_cache and existing_enhanced.exists():
            result = {
                "input_path": str(input_path),
                "output_path": str(existing_enhanced),
                "action": strategy.get("action", "cached"),
                "reason": "📂 enhanced existant chargé du cache (resume_from=postprocess)",
                "method": "cached",
                "steps": [{"action": strategy.get("action", "cached"), "reason": "cache"}],
                "brand_lut_applied": True,
                "cost_usd": 0,
                "duration_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "from_cache": True,
                "framing_changed": False,
                "framing_warning": None,
                "ai_validation": {"ok": True, "from_cache": True},
                "retry_attempted": False,
                "fallback_to_original": False,
                "persona_used": persona_per_filename.get(filename),
            }
        else:
            result = enhance.enhance_one(input_path, strategy, enhanced_dir)
        return slot, filename, {"strategy": strategy, "result": result}

    print(f"🎨 Enhance parallèle : {ENHANCE_WORKERS} workers sur {len(ordered_filenames)} photos")
    with ThreadPoolExecutor(max_workers=ENHANCE_WORKERS) as ex:
        futures = {ex.submit(_enhance_one_job, i, fn): (i, fn) for i, fn in enumerate(ordered_filenames, 1)}
        for fut in as_completed(futures):
            # ━━ Robuste aux exceptions : si UNE photo crashe (rate-limit, Gemini KO,
            #    image corrompue…), on log et on continue avec les autres. Sinon
            #    l'exception remonte hors du pool et coupe TOUT le pipeline avant
            #    le multi-format → Martin voyait ses crops disparaître.
            i, fn = futures[fut]
            try:
                ret = fut.result()
            except Exception as e:
                import traceback
                print(f"❌ [enhance #{i}] {fn} : {type(e).__name__}: {e}")
                traceback.print_exc()
                # On insère un slot avec un résultat d'erreur pour ne pas perturber le tri
                with enhance_lock:
                    enhanced_by_slot[i] = {
                        "filename": fn, "final_order_pos": i,
                        "input_path": "", "output_path": None,
                        "action": "error", "reason": f"Erreur enhance: {type(e).__name__}",
                        "error": str(e)[:200],
                        "steps": [], "cost_usd": 0, "duration_ms": 0,
                    }
                    enhance_progress["done"] += 1
                continue
            if ret is None:
                with enhance_lock:
                    enhance_progress["done"] += 1
                continue
            # ━━ BUGFIX critique (Martin 12/05/2026) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # Avant : la variable `payload` était écrasée ICI par le résultat enhance
            # → tout en bas (ligne ~1215), `payload.get("output_formats")`, `payload.get(
            # "outpaint_enabled")`, `payload.get("slowmo_enabled")` lisaient sur le DICT
            # enhance (`{"strategy":..., "result":...}`) au lieu du body HTTP original.
            # → multi-format et slowmo SILENCIEUSEMENT désactivés peu importe ce que le
            # client cochait. On utilise désormais un nom local distinct.
            slot, filename, enhance_ret = ret
            result = enhance_ret["result"]
            strategy = enhance_ret["strategy"]
            with enhance_lock:
                enhanced_by_slot[slot] = {"filename": filename, "final_order_pos": slot, **result}
                enhancement_cost_usd += result.get("cost_usd", 0)
                enhancement_input_tokens += result.get("input_tokens", 0) or 0
                enhancement_output_tokens += result.get("output_tokens", 0) or 0
                enhance_progress["done"] += 1
                done = enhance_progress["done"]
            progress.update(
                f"{slug}_analyze",
                current=done,
                total=len(ordered_filenames),
                message=f"Retouche {done}/{len(ordered_filenames)} : {filename} ({strategy['action']})",
            )

    # Sécurité : si enhanced_dir n'existe pas encore (toutes les photos ont raté ou aucune
    # n'avait d'analyse valide), on le crée pour que le multi-format ne saute pas en silence
    # (il logue "enhanced_dir absent" et continue) → Martin a un signal clair.
    enhanced_dir.mkdir(parents=True, exist_ok=True)

    # Reconstruit l'ordre stable du pack final (slot 1, 2, 3, ...)
    enhanced_results = [enhanced_by_slot[s] for s in sorted(enhanced_by_slot.keys())]

    # === Étape 4.5 (optionnelle) : Slow-motion loop ===
    # Une seule photo finale → cinemagraph mp4 (Higgsfield Kling 2.1 Pro + ping-pong ffmpeg).
    # Source = la version `enhanced` finale (avec retouches IA + LUT brand appliqués).
    # Voir docs/SLOWMO_SPEC.md pour la rationale.
    slowmo_enabled = bool((payload or {}).get("slowmo_enabled", False))
    # Variantes de format pour le slowmo (Insta story / feed / YouTube).
    slowmo_variants = (payload or {}).get("slowmo_format_variants") or []
    slowmo_result = None
    slowmo_target = None

    # ━━ Mode "réutilise le slowmo précédent" (Martin 13/05/2026) ━━
    # Si slowmo DÉCOCHÉ MAIS qu'un slowmo existe sur disque depuis un run précédent
    # → on le ré-expose dans la réponse pour que l'UI affiche la card vidéo sans
    # le re-générer ($0). Évite de devoir re-payer juste pour "revoir" le slowmo.
    #
    # Stratégie en 2 étapes :
    #   (a) Lit run.json pour le slowmo_result complet (cas idéal, prompt + cost gardés)
    #   (b) Fallback : scan /slowmo/*.mp4 sur disque si run.json absent / corrompu /
    #       ancien (avant qu'on ajoute la sérialisation slowmo_result). Reconstruit
    #       un slowmo_result minimal pour permettre l'affichage UI.
    if not slowmo_enabled:
        slowmo_dir_cache = ROOT / "data" / "output" / slug / "slowmo"
        cached_run_path = ROOT / "data" / "output" / slug / "run.json"
        # (a) Tente run.json
        if cached_run_path.exists():
            try:
                with open(cached_run_path) as f:
                    prev_run = json.load(f)
                prev_slowmo = prev_run.get("slowmo_result")
                if prev_slowmo and prev_slowmo.get("success"):
                    prev_path = prev_slowmo.get("output_path")
                    if prev_path and Path(prev_path).exists():
                        slowmo_result = prev_slowmo
                        slowmo_result["reused_from_cache"] = True
                        print(f"[slowmo] réutilise cache (run.json) : {Path(prev_path).name}")
            except Exception:
                pass
        # (b) Fallback disk-scan si rien chargé via run.json
        if slowmo_result is None and slowmo_dir_cache.exists():
            # On exclut les variantes (_raw / _story_9x16 / _feed_1x1 / _youtube_16x9)
            # et on garde le mp4 final principal le plus récent.
            VARIANT_SUFFIXES = ("_raw", "_story_9x16", "_feed_1x1", "_youtube_16x9")
            candidates = [
                p for p in slowmo_dir_cache.glob("*.mp4")
                if not any(p.stem.endswith(s) for s in VARIANT_SUFFIXES)
            ]
            if candidates:
                latest = max(candidates, key=lambda p: p.stat().st_mtime)
                slowmo_result = {
                    "success": True,
                    "output_path": str(latest),
                    "target": {
                        "filename": latest.stem + ".jpg",  # best-guess (correspond au stem)
                        "motion_subject": "ambient",
                    },
                    "reused_from_cache": True,
                    "reused_from_disk_scan": True,
                }
                print(f"[slowmo] réutilise cache (disk scan) : {latest.name}")

    if slowmo_enabled and ordered_pack:
        # Passe enhanced_results pour détecter les photos qui ont eu un humain ajouté IA
        # → on utilisera la version intermédiaire (avant ajout perso) + LUT brand.
        slowmo_target = slowmo_higgsfield.pick_slowmo_target(
            ordered_pack, by_filename, enhanced_results=enhanced_results
        )
        if slowmo_target:
            slowmo_dir = ROOT / "data" / "output" / slug / "slowmo"
            slowmo_dir.mkdir(parents=True, exist_ok=True)
            target_filename = slowmo_target["filename"]

            # ━━ Résolution de la photo source pour le slowmo (Martin 15/05/2026 v3) ━━
            # Stratégie : utiliser TOUJOURS la photo finale (enhanced) — y compris si
            # elle a un humain ajouté IA. La logique "use_intermediate" est désactivée
            # car pick_slowmo_target() exige maintenant `ai_validation.ok == True`
            # ET `fallback_to_original == False` pour les photos avec humain IA,
            # garantissant que la photo finale est "propre" et exploitable par Kling.
            #
            # Avant : on prenait l'intermédiaire (sans humain) si humain IA présent.
            # Maintenant : Kling reçoit la version FINALE (avec humain validé) +
            # prompt renforcé qui interdit timelapse + autorise micro-movements humains.
            enhanced_path = enhanced_dir / target_filename
            if enhanced_path.exists():
                source_path = enhanced_path
                if slowmo_target.get("has_ai_human"):
                    source_reason = "enhanced final (avec humain IA validé)"
                else:
                    source_reason = "enhanced final"
            else:
                source_path = Path(by_filename[target_filename]["input"]["path_absolute"])
                source_reason = "source originale (enhanced absent)"

            output_mp4 = slowmo_dir / (Path(target_filename).stem + ".mp4")
            variants_msg = f" + {len(slowmo_variants)} variantes" if slowmo_variants else ""
            progress.update(
                f"{slug}_analyze",
                step="slowmo",
                current=0,
                total=1,
                message=f"Slow-motion loop : {target_filename} ({slowmo_target['motion_subject']}, source: {source_reason}){variants_msg}…",
            )
            slowmo_result = slowmo_higgsfield.generate_slowmo(
                source_path,
                slowmo_target["motion_subject"],
                output_mp4,
                format_variants=slowmo_variants or None,
            )
            slowmo_result["target"] = slowmo_target
            slowmo_result["source_reason"] = source_reason
            progress.update(
                f"{slug}_analyze",
                step="slowmo_done",
                current=1,
                total=1,
                message=("Slow-motion OK" if slowmo_result.get("success") else f"Slow-motion KO : {slowmo_result.get('error')}"),
            )

    # === Étape 5 (optionnelle) : Multi-format crop ===
    # Si l'utilisateur a coché des formats dans Step 4 → on génère les variantes
    # croppées par format après les retouches IA.
    output_formats = (payload or {}).get("output_formats") or []
    outpaint_enabled = bool((payload or {}).get("outpaint_enabled", False))
    outpaint_quality = (payload or {}).get("outpaint_quality") or "flash"
    multiformat_result = None
    # Log explicite des raisons de skip (Martin 12/05/2026 : "multi-format ne tourne plus")
    if not output_formats:
        print(f"[multi-format] SKIP : aucun format coché dans Step 3 (output_formats={output_formats})")
    elif not enhanced_dir.exists():
        print(f"[multi-format] SKIP : enhanced_dir absent → {enhanced_dir}")
    else:
        n_enhanced_jpg = len([p for p in enhanced_dir.glob("*.jpg") if p.is_file()])
        if n_enhanced_jpg == 0:
            print(f"[multi-format] SKIP : enhanced_dir vide ({enhanced_dir}) → 0 jpg trouvés. Causes possibles : enhance loop a crash sur toutes les photos, ou aucune photo n'avait d'analyse valide.")
    if output_formats and enhanced_dir.exists():
        try:
            import multi_format_cropper
            n_photos_enhanced = len([p for p in enhanced_dir.glob("*.jpg") if p.is_file()])
            total_variants = n_photos_enhanced * len(output_formats)
            progress.update(
                f"{slug}_analyze",
                step="multi_format",
                current=0,
                total=total_variants,
                message=f"Génération multi-format : {total_variants} variantes ({n_photos_enhanced} photos × {len(output_formats)} formats)" + (f", outpaint {outpaint_quality}" if outpaint_enabled else "") + "…",
            )

            def _multiformat_progress(done: int, total: int, msg: str):
                progress.update(
                    f"{slug}_analyze",
                    step="multi_format",
                    current=done,
                    total=total,
                    message=msg,
                )

            multiformat_dir = ROOT / "data" / "output" / slug / "multiformat"
            multiformat_result = multi_format_cropper.run_multi_format(
                enhanced_dir=enhanced_dir,
                output_dir=multiformat_dir,
                format_ids=output_formats,
                analyses_dir=analyses_dir if analyses_dir.exists() else None,
                outpaint_enabled=outpaint_enabled,
                outpaint_quality=outpaint_quality,
                progress_callback=_multiformat_progress,
            )
            n_ok = multiformat_result['summary']['crop'] + multiformat_result['summary']['resize'] + multiformat_result['summary'].get('outpaint', 0)
            progress.update(
                f"{slug}_analyze",
                step="multi_format_done",
                current=total_variants,
                total=total_variants,
                message=f"Multi-format OK : {n_ok}/{total_variants} variantes (crop {multiformat_result['summary']['crop']} + outpaint {multiformat_result['summary'].get('outpaint', 0)} + skip {multiformat_result['summary'].get('skip', 0)})",
            )
        except Exception as e:
            multiformat_result = {"error": f"{type(e).__name__}: {e}"}

    # === Réponse ===
    photos_summary = []
    for a in analyses:
        filename = a["input"]["filename"]
        analysis = a.get("analysis") or {}
        factual = analysis.get("factual") or {}
        emotional = analysis.get("emotional") or {}
        scores = emotional.get("pillar_scores") or {}
        sel = selection_meta.get(filename)
        # Récupère l'erreur Gemini OU l'erreur d'ingestion (résolution trop faible, fichier corrompu)
        gemini_trace = next((t for t in a.get("trace", []) if t.get("node") == "N2_analyze_gemini"), None)
        n1_trace = next((t for t in a.get("trace", []) if t.get("node") == "N1_ingestion"), None)
        gemini_error = None
        if gemini_trace and gemini_trace.get("result") != "pass":
            gemini_error = gemini_trace.get("error")
        elif n1_trace and n1_trace.get("result") == "reject":
            gemini_error = f"Ingestion : {n1_trace.get('error', 'rejected')}"

        # Score components calculé en live pour TOUTES les photos (sélectionnées ou non)
        try:
            sc = coverage_mod.compute_score_components(a) if a.get("analysis") else None
        except Exception:
            sc = None

        photos_summary.append({
            "filename": filename,
            "url": f"/uploads/{slug}/{filename}",
            "category": factual.get("category"),
            "human_count": factual.get("human_count"),
            "ambiance": (analysis.get("technical_hints") or {}).get("ambiance"),
            "sensations": emotional.get("sensations"),
            "score_brand": int(scores.get("freedom", 0) + scores.get("wellness", 0) + scores.get("experience", 0)),
            "score_brand_total": sc["total"] if sc else None,
            "score_components": sc,
            "amenity_dominance": analysis.get("amenity_dominance"),
            "shot_type": analysis.get("shot_type"),
            "hero_quality": analysis.get("hero_quality"),
            "ai_candidate": (analysis.get("ai_add_character_candidate") or {}).get("is_candidate"),
            "dedup_status": a["dedup_status"],
            "issues": analysis.get("issues") or [],
            "trace_ok": any(t.get("result") == "pass" and t.get("node") == "N2_analyze_gemini" for t in a.get("trace", [])),
            "gemini_error": gemini_error,
            "selected": sel is not None,
            "selection_category": sel["category"] if sel else None,
            "selection_rank": sel["rank"] if sel else None,
        })

    # Map cluster_id pour debug front
    cluster_map = {}
    for cid, cluster in enumerate(clusters):
        for p in cluster:
            cluster_map[p.name] = cid
    for ph in photos_summary:
        ph["cluster_id"] = cluster_map.get(ph["filename"])

    # ━━ NB : progress.finish() est déplacé en FIN de api_run() (juste avant le return) ━━
    # Avant : finish ici → côté front, le polling voyait done=true et affichait "Pipeline
    # terminé" pendant que le serveur passait 5-15s à construire enhanced_summary +
    # photos_summary pour la réponse JSON. Résultat : bouton bloqué "Analyse en cours…",
    # progress UI à 100% mais aucun résultat affiché. Maintenant finish() fire juste avant
    # return → done=true coïncide avec la dispo de la réponse → bouton réactivé en même
    # temps que les résultats s'affichent.
    # On affiche un step "finalizing" intermédiaire pour que le front voit le pipe
    # encore vivant pendant la construction de la payload.
    progress.update(
        f"{slug}_analyze",
        step="finalizing",
        message="Construction de la réponse (lecture des analyses + transformations)…",
    )

    # Cumul des dépenses sur tous les runs (data/spend.json)
    spend.add_run(slug, {
        "analysis_usd": total_cost_usd,
        "enhancement_usd": enhancement_cost_usd,
        "ai_lighting": sum(1 for r in enhanced_results if r.get("action") == "ai_lighting"),
        "ai_add_character": sum(1 for r in enhanced_results if r.get("action") == "ai_add_character"),
        "ai_remove_people": sum(1 for r in enhanced_results if r.get("action") == "ai_remove_people"),
        "ai_recompose": sum(1 for r in enhanced_results if r.get("action") == "ai_recompose"),
        "local_smart_crop": sum(1 for r in enhanced_results if r.get("action") == "local_smart_crop"),
        "local_warm_boost": sum(1 for r in enhanced_results if r.get("action") == "local_warm_boost"),
    })

    # ━━ Construit le mapping SEO {filename → seo_filename} en amont ━━
    # Seq basé sur le slot final (= final_order_pos) → la photo en slot 1 du
    # pack aura `dayuse_{hotel}_{amenity}_01.jpg`, slot 2 → _02, etc.
    # On persiste sur disque pour que les ZIPs (téléchargés a posteriori)
    # utilisent les mêmes noms.
    hotel_slug_seo = _hotel_slug_for(slug)
    seo_names_map: dict[str, str] = {}
    for r in enhanced_results:
        fn = r["filename"]
        slot = r.get("final_order_pos") or (len(seo_names_map) + 1)
        amenity = _seo_amenity_for_file(slug, fn)
        ext = Path(fn).suffix.lower() or ".jpg"
        seo_names_map[fn] = _seo_filename(hotel_slug_seo, amenity, slot, ext)
    _persist_seo_names_map(slug, seo_names_map)

    # Construit la liste enhanced pour le front : avant/après + justification
    enhanced_summary = []
    for r in enhanced_results:
        filename = r["filename"]
        # Récup analyse + sélection meta pour la justification
        a = by_filename.get(filename) or {}
        analysis = a.get("analysis") or {}
        factual = analysis.get("factual") or {}
        emotional = analysis.get("emotional") or {}
        scores = emotional.get("pillar_scores") or {}
        score_total = int(scores.get("freedom", 0) + scores.get("wellness", 0) + scores.get("experience", 0))
        sel = selection_meta.get(filename, {})

        justification = {
            "category": factual.get("category"),
            "categories_secondary": factual.get("categories_secondary") or [],
            "coverage_targets": photo_targets.get(filename, []),
            "selection_category": sel.get("category"),
            "selection_rank": sel.get("rank"),
            "score_brand_total": sel.get("score_brand_total", score_total),
            "score_components": sel.get("score_components"),  # détail breakdown pour debug
            "score_freedom": scores.get("freedom", 0),
            "score_wellness": scores.get("wellness", 0),
            "score_experience": scores.get("experience", 0),
            "amenity_dominance": analysis.get("amenity_dominance"),
            "shot_type": analysis.get("shot_type"),
            "hero_quality": analysis.get("hero_quality"),
            "ambiance": (analysis.get("technical_hints") or {}).get("ambiance"),
            "palette": (analysis.get("technical_hints") or {}).get("palette_alignment"),
            "human_count": factual.get("human_count"),
            "human_presence_type": factual.get("human_presence_type"),
            "time_of_day": factual.get("time_of_day"),
            "sensations": emotional.get("sensations") or [],
            "issues": analysis.get("issues") or [],
        }

        # ━━ Bloc complet "🧠 Analyse Gemini Vision" pour le front (Martin 14/05/2026) ━━
        # On expose l'analyse sémantique COMPLÈTE faite par Gemini Vision sur cette photo
        # + le résultat du scenario_writer (V5) si dispo. Permet de comprendre ce que
        # Vision a vu / décidé pour chaque photo. Sert au debug et à la transparence.
        safe_zones_block = analysis.get("safe_zones_for_humans") or {}
        placement_capacity = analysis.get("placement_capacity") or {}
        # scenario_writer metadata vient des steps (injecté dans _pick_main_action V5)
        scenario_writer_meta = None
        for s in (r.get("steps") or []):
            if s.get("scenario_writer"):
                scenario_writer_meta = s.get("scenario_writer")
                break
        gemini_analysis = {
            "category": factual.get("category"),
            "categories_secondary": factual.get("categories_secondary") or [],
            "shot_type": analysis.get("shot_type"),
            "hero_quality": analysis.get("hero_quality"),
            "amenity_dominance": analysis.get("amenity_dominance"),
            "human_count": factual.get("human_count"),
            "human_presence_type": factual.get("human_presence_type"),
            "time_of_day": factual.get("time_of_day"),
            "ambiance": (analysis.get("technical_hints") or {}).get("ambiance"),
            "palette_alignment": (analysis.get("technical_hints") or {}).get("palette_alignment"),
            "exposure": (analysis.get("technical_hints") or {}).get("exposure"),
            "white_balance": (analysis.get("technical_hints") or {}).get("white_balance"),
            "issues": analysis.get("issues") or [],
            "clutter_to_remove": analysis.get("clutter_to_remove") or [],
            "safe_zones_for_humans": {
                "safe_areas": safe_zones_block.get("safe_areas") or [],
                "unsafe_areas": safe_zones_block.get("unsafe_areas") or [],
                "max_recommended": safe_zones_block.get("max_recommended"),
            },
            "placement_capacity": {
                "total": placement_capacity.get("total"),
                "breakdown": placement_capacity.get("breakdown") or {},
            },
            "sensations": emotional.get("sensations") or [],
            "pillar_scores": scores,
            "scenario_writer_v5": scenario_writer_meta,  # None si V5 inactif ou pas applicable
        }

        if r.get("output_path"):
            is_fully_gen = bool((a or {}).get("is_fully_generated"))
            is_bonus = bool(sel.get("is_bonus")) if sel else False
            # ━ Construit un récap structuré des transformations appliquées (cases à cocher UI) ━
            steps_actions = {s.get("action") for s in (r.get("steps") or [])}
            ai_validation = r.get("ai_validation") or {}
            transformations = {
                "smart_crop":       "local_smart_crop" in steps_actions,
                "ai_recompose":     "ai_recompose" in steps_actions,
                "lut_brand":        bool(r.get("brand_lut_applied")),
                "ai_lighting":      "ai_lighting" in steps_actions,
                "clutter_removed":  "ai_remove_clutter" in steps_actions,
                "character_added":  "ai_add_character" in steps_actions,
                "people_removed":   "ai_remove_people" in steps_actions,
                "warm_boost":       "local_warm_boost" in steps_actions,
                "fully_generated":  is_fully_gen,
                "bonus_lifestyle":  is_bonus,
                "validation_ok":    bool(ai_validation.get("ok")) and not r.get("fallback_to_original"),
                "retry_attempted":  bool(r.get("retry_attempted")),
                "fallback_original":bool(r.get("fallback_to_original")),
            }
            enhanced_summary.append({
                "filename": filename,
                "seo_filename": seo_names_map.get(filename, filename),
                "final_order_pos": r.get("final_order_pos"),
                "before_url": f"/uploads/{slug}/{filename}",
                "after_url": f"/output/{slug}/enhanced/{filename}",
                "action": r["action"],
                "reason": r["reason"],
                "method": r.get("method"),
                "cost_usd": r.get("cost_usd", 0),
                "duration_ms": r.get("duration_ms", 0),
                "framing_changed": r.get("framing_changed", False),
                "framing_warning": r.get("framing_warning"),
                "steps": r.get("steps") or [],
                "ai_validation": r.get("ai_validation"),
                "transformations": transformations,
                "justification": justification,
                "gemini_analysis": gemini_analysis,
                "is_fully_generated": is_fully_gen,
                "is_bonus": is_bonus,
                "bonus_amenity": sel.get("bonus_amenity") if sel else None,
                "persona_used": r.get("persona_used"),
            })
        else:
            enhanced_summary.append({
                "filename": filename,
                "seo_filename": seo_names_map.get(filename, filename),
                "final_order_pos": r.get("final_order_pos"),
                "before_url": f"/uploads/{slug}/{filename}",
                "after_url": None,
                "action": r["action"],
                "reason": r["reason"],
                "error": r.get("error"),
                "justification": justification,
            })

    total_enhancement_usd = enhancement_cost_usd
    total_all_usd = total_cost_usd + total_enhancement_usd

    pipeline_completed_at = time.time()
    pipeline_duration_s = round(pipeline_completed_at - pipeline_started_at, 1)

    # ━━ Construction du payload de réponse AVANT progress.finish ━━━━━━━━━━━━━
    # ⚠️ RÉGRESSION OBSERVÉE Martin 13/05/2026 : si on appelle progress.finish ICI,
    # puis qu'on construit ensuite un payload qui inclut `build_photo_journey()`
    # (~1-5s pour ~80 photos × 16 nœuds) et la grosse `jsonify`, le front voit
    # "Pipeline terminé" mais le fetch /api/run reste en attente → bouton bloqué
    # "Analyse en cours…".
    # Fix : on PRÉ-CALCULE photo_journey ICI (avant progress.finish), puis on
    # appelle progress.finish juste avant le return. Comme ça la fenêtre entre
    # done=true côté progress et la réponse fetch retournée est minimale.
    photo_journey_payload = photo_journey_mod.build_photo_journey(
        slug=slug,
        photo_paths_all=photo_paths_all,
        deselected_set=deselected,
        analyses=analyses,
        cov=cov,
        generated_photos=generated_photos,
        ordered_pack=ordered_pack,
        enhanced_results=enhanced_results,
        vlm_dedup_results=vlm_dedup_results,
        verifier_results=verifier_results,
        slowmo_result=slowmo_result,
    )

    # ━━ Sauve un manifest run.json pour permettre :
    #    - À /api/regenerate-slowmo de connaître la cible précédente
    #    - À un futur run mode `selection`/`postprocess` SANS slowmo coché de
    #      réutiliser le slowmo en cache (cf. logique plus haut au step 4.5)
    #
    # ⚠️ NE PAS ÉCRASER : si slowmo_result est None (case décochée et pas de cache
    # trouvé), on PRÉSERVE l'ancien run.json slowmo_result pour ne pas perdre la
    # trace d'un slowmo généré dans un run antérieur (sinon Martin perd visibilité
    # quand il itère en "Reprendre depuis sélection" case décochée).
    cached_run_path = ROOT / "data" / "output" / slug / "run.json"
    cached_run_path.parent.mkdir(parents=True, exist_ok=True)
    # Si on n'a rien de nouveau côté slowmo, lit l'ancien et garde-le
    persisted_slowmo_result = slowmo_result
    persisted_slowmo_summary = _build_slowmo_summary(slowmo_result, slug) if slowmo_result else None
    if persisted_slowmo_result is None and cached_run_path.exists():
        try:
            with open(cached_run_path) as f:
                prev_run = json.load(f)
            persisted_slowmo_result = prev_run.get("slowmo_result")
            persisted_slowmo_summary = prev_run.get("slowmo")
        except Exception:
            pass
    try:
        with open(cached_run_path, "w") as f:
            json.dump({
                "slug": slug,
                "completed_at": pipeline_completed_at,
                "duration_s": pipeline_duration_s,
                "resume_from": resume_from,
                "slowmo_result": persisted_slowmo_result,  # garde tout l'objet pour reuse
                "slowmo": persisted_slowmo_summary,
            }, f, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        print(f"[run.json] save failed: {e}")

    # ━━ Maintenant que TOUT est prêt, on marque "done" — coïncide avec la dispo
    #    de la réponse côté front (fenêtre done→fetch retourné = ms, pas s).
    progress.finish(f"{slug}_analyze", message="Pipeline terminé")

    # ━━ Persist validation results dans audit DB (Martin 15/05/2026) ━━━━━━━━━
    # Permet le dashboard agrégé + corrélation avec tags humains.
    # Ne JAMAIS bloquer le pipeline sur un échec de persist (try/except interne).
    try:
        for entry in enhanced_summary:
            fn = entry.get("filename")
            if not fn:
                continue
            audit_db.save_photo_result(slug=slug, filename=fn, entry=entry)
    except Exception as e:
        print(f"[audit_db] persist batch failed for {slug}: {e}")

    # ━━ Construit le response_data avant le return (pour persistence + return) ━━
    # (Martin 19/05/2026, fix "Failed to fetch" — Railway HTTP proxy timeout ~5min
    #  coupe la connexion front sur un pipeline long, mais le pipeline backend continue.
    #  On persiste la réponse complète dans final_response.json pour permettre au
    #  front de la récupérer via /api/last-response/<slug> quand sa connexion timeout.)
    response_data = {
        "slug": slug,
        "pipeline_started_at": pipeline_started_at,
        "pipeline_completed_at": pipeline_completed_at,
        "pipeline_duration_s": pipeline_duration_s,
        "hotel": {
            "name": rp_data["name"],
            "city": rp_data["city"],
            "stars": rp_data["star_classification"],
            "vibe": rp_data["vibe_primary"],
            "personas_allowed": rp_data["personas_allowed"],
            # Liste des URLs des photos ResortPass dans l'ordre original RP (pour la
            # prévisualisation comparative côte-à-côte dans le pack final).
            "rp_image_urls": rp_data.get("image_urls") or [],
            "rp_url": rp_data.get("url"),
        },
        "photos": photos_summary,
        "coverage": cov,
        "enhanced": enhanced_summary,
        "slowmo": _build_slowmo_summary(slowmo_result, slug) if slowmo_result else None,
        "stats": {
            "uploaded": len(photo_paths),
            "analyzed_ok": sum(1 for p in photos_summary if p["trace_ok"]),
            "kept_after_dedup": sum(1 for a in analyses if a.get("dedup_status") == "kept"),
            "duplicates_dropped": sum(1 for a in analyses if a.get("dedup_status") == "duplicate_dropped"),
            "vlm_dedup_pairs_checked": len(vlm_dedup_results),
            "vlm_dedup_same_scene_count": sum(1 for p in vlm_dedup_results if p.get("same_scene")),
            "vlm_dedup_cost_usd": round(sum(p.get("cost_usd", 0) for p in vlm_dedup_results), 6),
            "amenity_verifier_checked": len(verifier_results),
            "amenity_verifier_rejected": sum(1 for r in verifier_results if not r.get("is_focused")),
            "amenity_verifier_cost_usd": verifier_cost_usd,
            "enhanced": len([e for e in enhanced_summary if e.get("after_url")]),
            "ai_lighting": len([e for e in enhanced_summary if e.get("action") == "ai_lighting"]),
            "ai_add_character": len([e for e in enhanced_summary if e.get("action") == "ai_add_character"]),
            "ai_remove_people": len([e for e in enhanced_summary if e.get("action") == "ai_remove_people"]),
            "ai_recompose": len([e for e in enhanced_summary if e.get("action") == "ai_recompose"]),
            "local_enhanced": len([e for e in enhanced_summary if e.get("action") == "local_warm_boost"]),
            "slowmo_generated": 1 if (slowmo_result and slowmo_result.get("success")) else 0,
        },
        "cost": {
            "analysis_input_tokens": total_input_tokens,
            "analysis_output_tokens": total_output_tokens,
            "analysis_usd": round(total_cost_usd, 6),
            "enhancement_usd": round(total_enhancement_usd, 6),
            "enhancement_input_tokens": enhancement_input_tokens,
            "enhancement_output_tokens": enhancement_output_tokens,
            "slowmo_usd": round((slowmo_result or {}).get("cost_usd", 0) or 0, 6),
            "multiformat_usd": round((multiformat_result or {}).get("total_cost_usd", 0) or 0, 6),
            "multiformat_input_tokens": (multiformat_result or {}).get("total_input_tokens", 0) or 0,
            "multiformat_output_tokens": (multiformat_result or {}).get("total_output_tokens", 0) or 0,
            "vlm_dedup_usd": round(sum(p.get("cost_usd", 0) for p in vlm_dedup_results), 6),
            "amenity_verifier_usd": verifier_cost_usd,
            "total_usd": round(
                total_all_usd
                + sum(p.get("cost_usd", 0) for p in vlm_dedup_results)
                + verifier_cost_usd
                + ((multiformat_result or {}).get("total_cost_usd", 0) or 0)
                + ((slowmo_result or {}).get("cost_usd", 0) or 0),
                6),
            "total_eur": round((total_all_usd + sum(p.get("cost_usd", 0) for p in vlm_dedup_results) + verifier_cost_usd + ((multiformat_result or {}).get("total_cost_usd", 0) or 0) + ((slowmo_result or {}).get("cost_usd", 0) or 0)) * 0.92, 6),
            "total_input_tokens": total_input_tokens + enhancement_input_tokens + ((multiformat_result or {}).get("total_input_tokens", 0) or 0),
            "total_output_tokens": total_output_tokens + enhancement_output_tokens + ((multiformat_result or {}).get("total_output_tokens", 0) or 0),
        },
        "vlm_dedup_results": vlm_dedup_results,
        "amenity_verifier_results": verifier_results,
        # ━━ Multi-format output (Step 5, optionnel) ━━
        "multiformat": multiformat_result,
        # ━━ Workflow visualization data ━━
        "photo_journey": photo_journey_payload,  # pré-calculé avant progress.finish (cf. note ci-dessus)
        "workflow_nodes": photo_journey_mod.NODE_DEFINITIONS,
    }

    # ━━ Persiste la réponse pour recovery (Martin 19/05/2026, fix Failed to fetch) ━━
    # Railway HTTP proxy timeout ~5min coupe la connexion sur un pipeline long, mais
    # le backend continue à tourner. On écrit la réponse sur disk pour que le front
    # puisse la récupérer via /api/last-response/<slug> après que sa connexion ait
    # été coupée.
    try:
        final_response_path = ROOT / "data" / "output" / slug / "final_response.json"
        final_response_path.parent.mkdir(parents=True, exist_ok=True)
        # Utilise app.json.dumps qui passe par NumpyJSONProvider → gère numpy types
        with open(final_response_path, "w") as f:
            f.write(app.json.dumps(response_data))
        print(f"[run] final_response.json persisté ({slug}) — récupérable via /api/last-response/{slug}")
    except Exception as e:
        print(f"[run] persist final_response failed: {e}")

    return jsonify(response_data)


def _build_slowmo_summary(slowmo_result: dict, slug: str) -> dict:
    """Construit le summary slowmo envoyé au front."""
    target = slowmo_result.get("target") or {}
    out_path = slowmo_result.get("output_path")
    video_url = None
    if slowmo_result.get("success") and out_path:
        video_url = f"/output/{slug}/slowmo/{Path(out_path).name}"
    return {
        "success": bool(slowmo_result.get("success")),
        "video_url": video_url,
        "target_filename": target.get("filename"),
        "target_slot": target.get("slot"),
        "motion_subject": target.get("motion_subject") or slowmo_result.get("motion_subject"),
        "motion_strength": target.get("motion_strength"),
        "fallback_used": target.get("fallback_used", False),
        "model": slowmo_result.get("model"),
        "duration_s": slowmo_result.get("duration_s"),
        "duration_ms": slowmo_result.get("duration_ms"),
        "cost_usd": slowmo_result.get("cost_usd", 0.0),
        "prompt": slowmo_result.get("prompt"),
        "error": slowmo_result.get("error"),
    }


@app.route("/output/<slug>/slowmo/<filename>")
def serve_slowmo(slug, filename):
    """Sert le mp4 slow-motion pour preview dans l'UI."""
    return send_from_directory(ROOT / "data" / "output" / slug / "slowmo", filename)


@app.route("/api/regenerate-slowmo/<slug>", methods=["POST"])
def api_regenerate_slowmo(slug):
    """Re-génère uniquement le slowmo loop pour un hôtel déjà processé.

    Cycle de vie typique :
    - Le pipeline complet a tourné, `data/output/<slug>/enhanced/` contient les
      photos finales et le manifest run.json a un `slowmo_target.filename`.
    - L'utilisateur veut juste un autre essai sur le slowmo (changement de mood,
      modèle, ou simplement re-run après un échec credits) sans re-toucher au
      reste du pipeline (~$1-3 de Gemini Image).

    Body (optionnel) :
        {
          "filename": "...",        # override : choisir une autre photo cible
          "motion_subject": "...",  # override : water | curtains | foliage | pool_float | fire | steam | fountain | ambient
          "format_variants": ["story_9x16", ...],
          "model": "..."            # override modèle Higgsfield
        }
    """
    payload = request.get_json(silent=True) or {}

    # Lit le dernier run pour récupérer le pack + la cible slowmo par défaut
    run_path = ROOT / "data" / "output" / slug / "run.json"
    if not run_path.exists():
        return jsonify({"error": "Pas de run précédent. Lance le pipeline complet d'abord."}), 404
    try:
        with open(run_path) as f:
            run_data = json.load(f)
    except Exception as e:
        return jsonify({"error": f"run.json corrompu : {e}"}), 500

    # Cible par défaut = celle stockée dans le run précédent
    default_target = (run_data.get("slowmo") or {}).get("target") or {}
    target_filename = (payload.get("filename") or "").strip() or default_target.get("filename")
    motion_subject = (payload.get("motion_subject") or "").strip() or default_target.get("motion_subject") or "ambient"
    format_variants = payload.get("format_variants") or []
    model_override = (payload.get("model") or "").strip() or None

    if not target_filename:
        return jsonify({"error": "Aucune photo cible (ni override, ni cible stockée du run précédent)"}), 400

    enhanced_dir = ROOT / "data" / "output" / slug / "enhanced"
    enhanced_path = enhanced_dir / target_filename
    if not enhanced_path.exists():
        return jsonify({"error": f"Photo cible introuvable : {target_filename}"}), 404

    slowmo_dir = ROOT / "data" / "output" / slug / "slowmo"
    slowmo_dir.mkdir(parents=True, exist_ok=True)
    output_mp4 = slowmo_dir / (Path(target_filename).stem + ".mp4")

    # Lance la génération
    progress.init(f"{slug}_regen_slowmo", total=1, step="slowmo")
    progress.update(f"{slug}_regen_slowmo",
                    message=f"Re-génération slowmo : {target_filename} ({motion_subject})…")

    try:
        result = slowmo_higgsfield.generate_slowmo(
            enhanced_path,
            motion_subject,
            output_mp4,
            model=model_override,
            format_variants=format_variants or None,
        )
    except Exception as e:
        progress.finish(f"{slug}_regen_slowmo", message=f"Erreur : {e}")
        return jsonify({"error": f"Génération slowmo échouée : {str(e)[:200]}"}), 500

    progress.finish(f"{slug}_regen_slowmo",
                    message="Slowmo OK" if result.get("success") else f"Slowmo KO : {result.get('error')}")

    # Update du run.json pour garder une trace de la dernière génération slowmo
    run_data["slowmo"] = {
        **(run_data.get("slowmo") or {}),
        "target": {
            "filename": target_filename,
            "motion_subject": motion_subject,
            "regenerated_at": time.time(),
        },
        "success": result.get("success"),
        "error": result.get("error"),
        "output_path": result.get("output_path"),
        "loop_mode": result.get("loop_mode"),
        "format_variants": result.get("format_variants"),
        "cost_usd": result.get("cost_usd", 0),
    }
    try:
        with open(run_path, "w") as f:
            json.dump(run_data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

    return jsonify({
        "success": result.get("success"),
        "filename": target_filename,
        "motion_subject": motion_subject,
        "output_url": f"/output/{slug}/slowmo/{output_mp4.name}" if result.get("success") else None,
        "loop_mode": result.get("loop_mode"),
        "format_variants": result.get("format_variants"),
        "cost_usd": result.get("cost_usd", 0),
        "duration_ms": result.get("duration_ms"),
        "error": result.get("error"),
    })


@app.route("/output/<slug>/enhanced/<filename>")
def serve_enhanced(slug, filename):
    """Sert les photos retouchées pour preview avant/après dans l'UI."""
    return send_from_directory(ROOT / "data" / "output" / slug / "enhanced", filename)


@app.route("/output/<slug>/multiformat/<path:filename>")
def serve_multiformat(slug, filename):
    """Sert les variantes multi-format pour preview dans le rendu front.
    Le path peut inclure le dossier format (ex: 'insta_feed/photo_01.jpg')."""
    return send_from_directory(ROOT / "data" / "output" / slug / "multiformat", filename)


def _seo_slug_from_name(name: str | None, fallback_slug: str) -> str:
    """Construit un slug SEO-friendly depuis le nom de l'hôtel.

    'Moxy Miami South Beach' → 'moxy-miami-south-beach'. Si le nom est absent,
    on retombe sur le slug interne en retirant les préfixes techniques
    (booking-, hyatt-, …).
    """
    if name:
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if slug:
            return slug
    s = fallback_slug.lower()
    for prefix in ("booking-", "hyatt-", "hilton-", "marriott-", "rp-"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s or fallback_slug


# Mapping FR → EN pour les amenities (utilisé dans les noms de fichiers SEO).
_AMENITY_EN = {
    "piscine":                "pool",
    "piscine_vue_aerienne":   "pool-aerial-view",
    "cabana":                 "cabana",
    "transat":                "sun-lounger",
    "rooftop":                "rooftop",
    "spa":                    "spa",
    "f_and_b":                "bar-restaurant",
    "beach":                  "beach",
    "gym":                    "gym",
    "chambre":                "room",
    "interieur_commun":       "lobby",
    "exterieur":              "outdoor",
    "facade":                 "facade",
    "detail":                 "detail",
    "staff":                  "staff",
    "autre":                  "other",
}


def _seo_amenity_for_file(slug: str, original_filename: str) -> str:
    """Retourne l'amenity EN slugifiée pour cette photo (lue depuis l'analyse Gemini)."""
    analyses_dir = ROOT / "data" / "analyses" / slug
    stem = Path(original_filename).stem
    analysis_path = analyses_dir / f"{stem}.json"
    if not analysis_path.exists():
        return "photo"
    try:
        with open(analysis_path) as fh:
            data = json.load(fh)
        cat = ((data.get("analysis") or {}).get("factual") or {}).get("category") or ""
        cat = cat.lower().strip()
        return _AMENITY_EN.get(cat, cat) if cat else "photo"
    except Exception:
        return "photo"


def _seo_filename(hotel_slug: str, amenity: str, seq: int, ext: str) -> str:
    """Format SEO unifié : `dayuse_{hotel-slug}_{amenity}_{seq:02d}.{ext}`."""
    return f"dayuse_{hotel_slug}_{amenity}_{seq:02d}{ext}"


def _hotel_slug_for(slug: str) -> str:
    """Lit le nom de l'hôtel depuis data/rp/{slug}.json et le slugifie. Caché en
    fonction pour éviter de répéter le lecture+slugify dans plusieurs call sites.
    """
    rp_path = ROOT / "data" / "rp" / f"{slug}.json"
    hotel_name = None
    if rp_path.exists():
        try:
            with open(rp_path) as f:
                hotel_name = json.load(f).get("name")
        except Exception:
            pass
    return _seo_slug_from_name(hotel_name, slug)


def _load_seo_names_map(slug: str) -> dict[str, str] | None:
    """Lit le mapping persisté `data/output/{slug}/seo_names.json` (généré au
    moment du run via _persist_seo_names_map). Retourne None si absent.
    """
    p = ROOT / "data" / "output" / slug / "seo_names.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _persist_seo_names_map(slug: str, name_map: dict[str, str]) -> None:
    """Sauvegarde le mapping {original_filename → seo_filename} sur disque pour
    que ZIPs et front lisent les mêmes noms.
    """
    out = ROOT / "data" / "output" / slug
    out.mkdir(parents=True, exist_ok=True)
    (out / "seo_names.json").write_text(json.dumps(name_map, ensure_ascii=False, indent=2))


def _build_seo_filename_map(slug: str, files: list[Path]) -> dict[str, str]:
    """Construit le mapping {original_filename → SEO_filename} à partir d'une
    liste de fichiers triés alphabétiquement.

    Format : `dayuse_{hotel-slug}_{amenity}_{seq:02d}.{ext}`. Le seq suit l'ordre
    alphabétique des filenames (= ordre du pack final en pratique).

    NOTE : si un mapping persisté existe (`seo_names.json`), on l'utilise en
    priorité — il est aligné sur l'ordre du pack final (slot 1, 2, …) calculé
    pendant le run, plus précis que l'ordre alpha.
    """
    persisted = _load_seo_names_map(slug)
    if persisted:
        # On rapatrie les noms persistés pour les fichiers demandés
        result = {}
        for f in files:
            if f.name in persisted:
                result[f.name] = persisted[f.name]
            else:
                # Fichier sans mapping persisté → fallback compute fresh
                # (cas où un nouveau fichier serait apparu sur disque hors pipeline)
                amenity = _seo_amenity_for_file(slug, f.name)
                hotel_slug = _hotel_slug_for(slug)
                seq = len(result) + 1
                result[f.name] = _seo_filename(hotel_slug, amenity, seq, f.suffix.lower())
        return result

    # Pas de mapping persisté → calcul sur ordre alpha (fallback)
    hotel_slug = _hotel_slug_for(slug)
    name_map: dict[str, str] = {}
    for seq, f in enumerate(files, 1):
        amenity = _seo_amenity_for_file(slug, f.name)
        name_map[f.name] = _seo_filename(hotel_slug, amenity, seq, f.suffix.lower())
    return name_map


@app.route("/api/download-zip/<slug>")
def api_download_zip(slug):
    """Pack les photos retouchées finales en ZIP pour téléchargement.

    Renomme chaque fichier en `{hotel-slug}_{amenity}_{seq:02d}.jpg` pour que
    les noms parlent aux bots IA / SEO image. L'ordre seq suit l'ordre du
    pack final (= ordre alphabétique des filenames origine, qui matche).
    """
    import zipfile
    import io as _io

    enhanced_dir = ROOT / "data" / "output" / slug / "enhanced"
    if not enhanced_dir.exists():
        return jsonify({"error": "Aucune photo retouchée. Lance le pipeline d'abord."}), 404

    files = sorted([p for p in enhanced_dir.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")])
    if not files:
        return jsonify({"error": "Dossier de retouches vide"}), 404

    name_map = _build_seo_filename_map(slug, files)
    # Préfixe du ZIP basé sur le hotel-slug aussi (cohérence)
    rp_path = ROOT / "data" / "rp" / f"{slug}.json"
    hotel_name = None
    if rp_path.exists():
        try:
            hotel_name = json.load(open(rp_path)).get("name")
        except Exception:
            pass
    hotel_slug = _seo_slug_from_name(hotel_name, slug)

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=name_map.get(f.name, f.name))
    buf.seek(0)

    from flask import send_file
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"dayuse_{hotel_slug}_pack.zip",
    )


@app.route("/api/export/<slug>.pdf", methods=["POST"])
def api_export_pdf(slug):
    """Génère un PDF branded Dayuse "avant/après" depuis le run_data POSTé.

    Le front envoie le `state.lastRun` (= dernière réponse de /api/run) en JSON
    dans le body. Le serveur génère un PDF via Playwright + Jinja2 (charte Dayuse)
    et le retourne en attachment.

    Pourquoi POST + body au lieu de GET + fichier sur disque : on évite de stocker
    le `data` du run en plus (déjà persisté en bouts dispersés : enhanced/, analyses/,
    rp/, etc.) et on garde le PDF reproductible exactement comme la page affichée.
    """
    run_data = request.get_json(silent=True) or {}
    if not run_data:
        return jsonify({"error": "Body JSON manquant (run_data attendu)"}), 400

    try:
        pdf_bytes = pdf_export.generate_pdf(slug, run_data)
    except pdf_export.PdfNoPhotosError as e:
        # Cas attendu : disque wipé (Railway eph fs) ou run_data vide.
        # On renvoie un 422 ("Unprocessable Entity") avec un message actionnable
        # plutôt qu'un 500 générique. Le front affiche tel quel via alert().
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        return jsonify({"error": f"Génération PDF échouée : {type(e).__name__}: {str(e)[:200]}"}), 500

    hotel_name = (run_data.get("hotel") or {}).get("name") or slug
    # Slugify minimal pour le filename (espaces → underscores, char non-ASCII → ascii-safe)
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in hotel_name)[:60]
    filename = f"Dayuse_{safe}_pack_photos.pdf"

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/uploads/<slug>/<filename>")
def serve_upload(slug, filename):
    """Sert les photos uploadées pour preview dans l'UI."""
    return send_from_directory(UPLOADS_DIR / slug, filename)


@app.route("/api/rp/<slug>")
def api_rp(slug):
    """Retourne le RP scrapé pour un slug donné (utilisé par le raccourci hôtel
    pour rebooter state.rp sans relancer le scrap)."""
    rp_path = ROOT / "data" / "rp" / f"{slug}.json"
    if not rp_path.exists():
        return jsonify({"error": "RP non scrapé"}), 404
    return jsonify({"slug": slug, "data": json.loads(rp_path.read_text())})


@app.route("/api/hotels-processed")
def api_hotels_processed():
    """Liste tous les hôtels qui ont déjà eu au moins un scrap (sources sur disque)
    OU une analyse OU un run complet. Utilisé par le sélecteur 'Reprendre un hôtel'
    en haut de Step 1 pour permettre de skip le scrap si on bosse sur un hôtel
    qu'on a déjà processé.

    Sources combinées (Martin 19/05/2026 — fix Railway eph fs) :
      1. Filesystem `data/rp/*.json` (le plus riche : name, city, vibe, stars)
      2. Filesystem `data/uploads/<slug>/` (uploads only)
      3. Postgres `photo_results` (NEW : persistant à travers les redeploys,
         sauve la liste même quand filesystem wipé sur Railway sans Volume)
    """
    hotels = []
    rp_dir = ROOT / "data" / "rp"
    seen_slugs = set()

    # Source 1 : RP scrapés (le plus complet : on a name, city, vibe)
    if rp_dir.exists():
        for rp_file in sorted(rp_dir.glob("*.json")):
            slug = rp_file.stem
            seen_slugs.add(slug)
            try:
                rp = json.loads(rp_file.read_text())
                hotels.append({
                    "slug": slug,
                    "name": rp.get("name") or slug,
                    "city": rp.get("city"),
                    "vibe": rp.get("vibe_primary"),
                    "stars": rp.get("star_classification"),
                    "rp_scraped": True,
                })
            except Exception:
                hotels.append({"slug": slug, "name": slug, "rp_scraped": True})

    # Source 2 : slugs avec uploads/ mais sans RP (cas où Booking-only)
    if UPLOADS_DIR.exists():
        for sub in sorted(UPLOADS_DIR.iterdir()):
            if sub.is_dir() and sub.name not in seen_slugs:
                seen_slugs.add(sub.name)
                hotels.append({
                    "slug": sub.name,
                    "name": sub.name.replace("-", " ").title(),
                    "rp_scraped": False,
                })

    # Source 3 : Postgres (slugs depuis audit_db.photo_results)
    # Permet de retrouver les hôtels processés même après un redeploy qui a
    # wipé le filesystem (Railway éphémère sans Volume attaché).
    try:
        with audit_db.SessionLocal() as session:
            from sqlalchemy import distinct, select, func as _sql_func
            stmt = select(
                audit_db.PhotoResult.slug,
                _sql_func.max(audit_db.PhotoResult.created_at).label("last_run_at"),
            ).group_by(audit_db.PhotoResult.slug)
            for row in session.execute(stmt).all():
                slug = row[0]
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                hotels.append({
                    "slug": slug,
                    "name": slug.replace("-", " ").title(),
                    "rp_scraped": False,
                    "from_db_only": True,  # signal au front : pas de cache disque
                    "last_run_at": row[1],
                })
    except Exception as e:
        print(f"[hotels-processed] DB source failed (non-bloquant) : {e}")

    # Enrichi avec compteurs (sources, analyses, enhanced) pour le badge
    for h in hotels:
        slug = h["slug"]
        sources_dir = UPLOADS_DIR / slug
        analyses_dir = ROOT / "data" / "analyses" / slug
        enhanced_dir = ROOT / "data" / "output" / slug / "enhanced"
        h["n_sources"] = len(list(sources_dir.glob("*.jpg")) + list(sources_dir.glob("*.jpeg")) + list(sources_dir.glob("*.png")) + list(sources_dir.glob("*.webp"))) if sources_dir.exists() else 0
        h["n_analyses"] = len(list(analyses_dir.glob("*.json"))) if analyses_dir.exists() else 0
        h["n_enhanced"] = len([p for p in enhanced_dir.glob("*.jpg")] + [p for p in enhanced_dir.glob("*.png")]) if enhanced_dir.exists() else 0
        # Last run timestamp
        progress_path = ROOT / "data" / "progress" / f"{slug}_analyze.json"
        if progress_path.exists():
            try:
                p = json.loads(progress_path.read_text())
                h["last_run_at"] = p.get("updated_at")
            except Exception:
                h["last_run_at"] = None
        else:
            h["last_run_at"] = None

    # Tri : derniers runs en premier
    hotels.sort(key=lambda h: -(h.get("last_run_at") or 0))
    return jsonify({"hotels": hotels})


@app.route("/api/run-status/<slug>")
def api_run_status(slug):
    """Détecte ce qui existe déjà sur disque pour un slug → permet de proposer
    une reprise partielle dans l'UI Step 3 (économie de temps massif sur les
    tests itératifs : skip analyse Gemini ~10min, skip enhance ~5min, etc.).
    """
    rp_path = ROOT / "data" / "rp" / f"{slug}.json"
    sources_dir = UPLOADS_DIR / slug
    analyses_dir = ROOT / "data" / "analyses" / slug
    enhanced_dir = ROOT / "data" / "output" / slug / "enhanced"
    multiformat_dir = ROOT / "data" / "output" / slug / "multiformat"
    slowmo_dir = ROOT / "data" / "output" / slug / "slowmo"

    n_sources = len([p for p in sources_dir.glob("*.jpg")] + [p for p in sources_dir.glob("*.jpeg")] + [p for p in sources_dir.glob("*.png")] + [p for p in sources_dir.glob("*.webp")]) if sources_dir.exists() else 0
    n_analyses = len(list(analyses_dir.glob("*.json"))) if analyses_dir.exists() else 0
    n_enhanced = len([p for p in enhanced_dir.glob("*.jpg")] + [p for p in enhanced_dir.glob("*.png")]) if enhanced_dir.exists() else 0
    n_multiformat = 0
    multiformat_formats = []
    if multiformat_dir.exists():
        for sub in multiformat_dir.iterdir():
            if sub.is_dir():
                count = len(list(sub.glob("*.jpg")))
                if count > 0:
                    multiformat_formats.append({"format_id": sub.name, "n_variants": count})
                    n_multiformat += count
    n_slowmo = 0
    if slowmo_dir.exists():
        # On compte UNIQUEMENT les loops finaux, pas les fichiers `_raw.mp4`
        # (clip Kling brut avant ping-pong ffmpeg — 2 fichiers par génération).
        # Aussi exclure les variantes format (suffixes _story_9x16, _feed_1x1, etc.)
        # qui sont des dérivés du même slowmo.
        VARIANT_SUFFIXES = ("_raw", "_story_9x16", "_feed_1x1", "_youtube_16x9")
        files = list(slowmo_dir.glob("*.mp4")) + list(slowmo_dir.glob("*.webm"))
        n_slowmo = sum(1 for p in files if not any(p.stem.endswith(s) for s in VARIANT_SUFFIXES))

    # Latest run timestamp (depuis progress.json)
    progress_path = ROOT / "data" / "progress" / f"{slug}_analyze.json"
    last_run_at = None
    if progress_path.exists():
        try:
            p = json.loads(progress_path.read_text())
            last_run_at = p.get("updated_at")
        except Exception:
            pass

    return jsonify({
        "slug": slug,
        "rp_scraped": rp_path.exists(),
        "n_sources": n_sources,
        "n_analyses": n_analyses,
        "n_enhanced": n_enhanced,
        "n_multiformat": n_multiformat,
        "multiformat_formats": multiformat_formats,
        "n_slowmo": n_slowmo,
        "last_run_at": last_run_at,
        # Niveaux de reprise possibles
        "can_resume_from_analyze": n_sources > 0,
        "can_resume_from_selection": n_analyses > 0 and n_sources > 0,
        "can_resume_from_postprocess": n_enhanced > 0,  # juste re-générer multi-format/slowmo
    })


@app.route("/api/output-formats")
def api_output_formats():
    """Retourne le catalogue des formats de sortie pour l'UI Step 3."""
    return send_from_directory(ROOT / "config", "output_formats.json")


@app.route("/api/mcscla-fields")
def api_mcscla_fields():
    """Retourne la taxonomie MCSCLA des champs photo (10 buckets) pour la doc."""
    return send_from_directory(ROOT / "config", "mcscla_fields.json")


@app.route("/api/download-multiformat-zip/<slug>")
def api_download_multiformat_zip(slug):
    """Pack le dossier multiformat (1 sous-dossier par format) en ZIP.

    Chaque variante est renommée en `{hotel-slug}_{amenity}_{seq:02d}.jpg`
    (seq calculé sur l'ordre alphabétique du nom d'origine = ordre du pack).
    La structure du ZIP conserve les sous-dossiers par format_id.
    """
    import zipfile
    import io as _io

    multiformat_dir = ROOT / "data" / "output" / slug / "multiformat"
    if not multiformat_dir.exists():
        return jsonify({"error": "Aucun multi-format généré. Coche au moins un format dans Step 4 et relance la pipeline."}), 404

    # Construit le mapping SEO une fois (basé sur l'ordre alphabétique du 1er
    # sous-dossier non-vide) pour que tous les formats partagent la même
    # numérotation séquentielle.
    reference_files = []
    for sub in sorted([s for s in multiformat_dir.iterdir() if s.is_dir()]):
        candidates = sorted([f for f in sub.iterdir() if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")])
        if candidates:
            reference_files = candidates
            break
    name_map = _build_seo_filename_map(slug, reference_files) if reference_files else {}

    # Liste tous les fichiers (jpg dans sous-dossiers + manifest.json à la racine)
    files = []
    for sub in multiformat_dir.iterdir():
        if sub.is_dir():
            for f in sub.iterdir():
                if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    # Renomme via name_map (tous formats partagent le mapping)
                    seo_name = name_map.get(f.name, f.name)
                    files.append((f, f"{sub.name}/{seo_name}"))
        elif sub.is_file() and sub.name == "manifest.json":
            files.append((sub, sub.name))

    if not any(arcname.endswith((".jpg", ".jpeg", ".png", ".webp")) for _, arcname in files):
        return jsonify({"error": "Aucune variante générée (peut-être que tous les formats ont été skippés)."}), 404

    # Préfixe ZIP cohérent avec le hotel slug
    rp_path = ROOT / "data" / "rp" / f"{slug}.json"
    hotel_name = None
    if rp_path.exists():
        try:
            hotel_name = json.load(open(rp_path)).get("name")
        except Exception:
            pass
    hotel_slug = _seo_slug_from_name(hotel_name, slug)

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath, arcname in files:
            zf.write(fpath, arcname=arcname)
    buf.seek(0)

    from flask import send_file
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"dayuse_{hotel_slug}_multiformat.zip",
    )


@app.route("/comparison/<slug>/")
@app.route("/comparison/<slug>/<path:filename>")
def serve_comparison(slug, filename="comparison.html"):
    """Sert le rapport comparatif AB test (HTML + images des sous-dossiers).

    Utilisé pour intégrer la documentation modèles d'image (Nano Banana vs GPT)
    dans l'onglet Documentation > Comparatif modèles via iframe.
    """
    return send_from_directory(ROOT / "data" / "output" / slug / "comparison", filename)


@app.route("/laws-audit/")
@app.route("/laws-audit/<path:filename>")
def serve_laws_audit(filename="laws_audit.html"):
    """Sert le rapport d'audit matriciel des lois (heatmaps redondance/conflit).

    Documentation défendable : pour chaque paire des 20 lois (11 lois métier + 9 filets),
    quantifie le % de redondance et de conflit sur ~73k PhotoStates simulés.
    Cf. `laws.py` (formalisation) et `laws_matrix.py` (calcul).
    """
    return send_from_directory(ROOT / "data" / "output" / "laws_audit", filename)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Audit endpoints (Martin 15/05/2026, pack C)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route("/api/photo-tag", methods=["POST"])
def api_set_photo_tag():
    """Crée/MAJ le tag humain ✅⚠️❌ pour une photo.

    Body JSON : { slug: str, filename: str, tag: 'good'|'borderline'|'bad', note?: str }
    """
    data = request.get_json(force=True) or {}
    slug = (data.get("slug") or "").strip()
    filename = (data.get("filename") or "").strip()
    tag = (data.get("tag") or "").strip()
    note = data.get("note")
    if not slug or not filename or not tag:
        return jsonify({"error": "slug, filename, tag requis"}), 400
    res = audit_db.set_photo_tag(slug, filename, tag, note)
    if not res.get("ok"):
        return jsonify({"error": res.get("error", "save failed")}), 400
    return jsonify(res)


@app.route("/api/photo-tag", methods=["GET"])
def api_get_photo_tag():
    """Retourne le tag humain courant d'une photo (ou null)."""
    slug = (request.args.get("slug") or "").strip()
    filename = (request.args.get("filename") or "").strip()
    if not slug or not filename:
        return jsonify({"error": "slug et filename requis"}), 400
    tag = audit_db.get_photo_tag(slug, filename)
    return jsonify(tag or {})


@app.route("/api/photo-tags-bulk", methods=["GET"])
def api_get_tags_bulk():
    """Tous les tags d'un slug, formatés en {filename: {tag, note, ...}}."""
    slug = (request.args.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "slug requis"}), 400
    return jsonify(audit_db.get_all_tags_for_slug(slug))


@app.route("/api/last-response/<slug>")
def api_last_response(slug):
    """Retourne la dernière réponse pipeline persistée (recovery après Failed to fetch).

    Quand le Railway proxy timeout coupe la connexion HTTP front pendant le pipeline,
    le backend continue à tourner et écrit `final_response.json` à la fin. Cet endpoint
    permet au front de récupérer la réponse complète sans relancer le pipeline.

    Usage côté front :
    - POST /api/run lance le pipeline
    - Si fetch fail (Failed to fetch / timeout), poll /api/progress?slug=X
    - Quand progress.done=true, fetch /api/last-response/<slug> → réponse complète
    """
    final_response_path = ROOT / "data" / "output" / slug / "final_response.json"
    if not final_response_path.exists():
        return jsonify({"error": f"Aucune réponse persistée pour le slug '{slug}'. "
                                  "Le pipeline n'a peut-être pas terminé, ou les fichiers ont été "
                                  "wipés (filesystem éphémère Railway sans Volume attaché)."}), 404
    try:
        with open(final_response_path) as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Lecture final_response.json échouée : {str(e)[:200]}"}), 500


@app.route("/api/audit-dashboard")
def api_audit_dashboard():
    """Stats agrégées pour le dashboard. Filtres optionnels : slug, days."""
    slug = request.args.get("slug") or None
    try:
        days = int(request.args.get("days") or 30)
    except (ValueError, TypeError):
        days = 30
    return jsonify(audit_db.get_dashboard_stats(slug=slug, days=days))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
