"""Multi-format crop pipeline (Phase 1+2: Pillow + outpainting Nano Banana).

Étape 5 du pipeline : pour chaque photo finale + chaque format demandé,
produit une variante au ratio cible :
- Resize simple si Δratio ≤ 5%
- Crop intelligent (Pillow + safe_zones Gemini) si crop possible (zone ≥ 50%)
- Outpainting via Nano Banana 2 si extension nécessaire (Phase 2, opt-in)
- Fallback Nano Banana Pro si Flash échoue (Phase 4)

Output : dossier par format + manifest.json + ZIP final.

Usage standalone :
    .venv/bin/python multi_format_cropper.py <slug> --formats home_card_mobile,insta_feed
    .venv/bin/python multi_format_cropper.py <slug> --preset pack_dayuse --outpaint
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config" / "output_formats.json"

# === Configuration outpainting (Phase 2) ===
NANO_BANANA_FLASH = "gemini-3.1-flash-image-preview"  # ~$0.039/image
NANO_BANANA_PRO = "gemini-3-pro-image-preview"        # ~$0.134/image (fallback Phase 4)
COST_FLASH_USD = 0.039
COST_PRO_USD = 0.134

OUTPAINT_PROMPT_TEMPLATE = """You are doing PHOTOGRAPHIC OUTPAINTING (uncrop). The input is a transparent canvas of {canvas_w}×{canvas_h} pixels with a single photograph placed {placement_desc}, occupying {source_pct}% of the canvas. The remaining transparent (alpha=0) areas — specifically {extend_directions} — must be filled with a CONTINUOUS EXTENSION of the SAME scene.

🚨 ABSOLUTE NON-NEGOTIABLE RULES :
1. ONE SINGLE COHERENT PHOTOGRAPH. The output must read as ONE photo taken from the SAME camera at the SAME moment with a slightly WIDER field of view. NEVER tile, mirror, repeat, or stack multiple views. NEVER show the same element (the same chair, same pool, same column, same skyline) twice in different positions.
2. The original photograph pixels MUST stay 100% identical at their original position (no shift, no recoloring, no zoom).
3. Continuity rules for the extension:
   - The HORIZON, sky line, ceiling line, floor line, walls, columns, balustrades, pool edges, and architectural elements must continue from the original at the SAME height, SAME angle, SAME perspective.
   - Lighting direction, sun position, shadow angles must match the original EXACTLY.
   - Color palette, white balance, contrast, grain, exposure must match.
   - Depth perspective : objects further from the camera in the extension must be smaller, in correct vanishing point alignment.
4. CONTENT of the extension : it should look like what would naturally be visible if the original photo had been taken with a wider lens — more of the same room/terrace/pool/sky. Plausible continuation of the existing scene. NOT a different scene.
5. FORBIDDEN in the extension :
   - NEW people, NEW characters, NEW faces (zero humans in the extension if original has zero)
   - NEW furniture not implied by the existing scene
   - NEW signs, logos, text, watermarks
   - REPETITION of any element already visible in the original (no second pool, no copy of the same chair stack)
   - SEAM lines, color breaks, lighting discontinuities, ghost outlines
   - Multiple stacked views of "the same scene from different angles" (this is the #1 failure mode — DO NOT DO IT)
   - CGI / 3D-render aesthetic / oversharpened plasticky look

Target aspect ratio : {target_aspect_ratio}. Final image dimensions : {canvas_w}×{canvas_h}.

Negative prompt : new people, duplicated elements, repeated chairs, repeated pool, mirrored scene, tiled output, multiple views, seam, color break, ghost outlines, CGI, watermark, logo, text."""

# ============================================================
# Helpers
# ============================================================

def load_formats_config() -> dict:
    """Charge le catalogue des formats."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_format(formats_config: dict, format_id: str) -> dict:
    """Récupère un format par son id."""
    for f in formats_config["formats"]:
        if f["id"] == format_id:
            return f
    raise ValueError(f"Format inconnu : {format_id}")


# ============================================================
# Stratégie de crop
# ============================================================

CropStrategy = Literal["resize", "crop", "outpaint", "skip"]

RESIZE_TOLERANCE = 0.05  # Δratio ≤ 5% → resize simple


def compute_crop_strategy(source_size: tuple[int, int], target_size: tuple[int, int]) -> CropStrategy:
    """Détermine la stratégie pour passer de source à target.

    - resize : ratios compatibles (Δ ≤ 5%)
    - crop : target plus étroit ou moins haut → on rogne
    - outpaint : target plus large ou plus haut → on doit étendre (Phase 2)
    """
    sw, sh = source_size
    tw, th = target_size
    source_ratio = sw / sh
    target_ratio = tw / th
    ratio_diff = abs(source_ratio - target_ratio) / max(source_ratio, target_ratio)

    if ratio_diff <= RESIZE_TOLERANCE:
        return "resize"

    # Le ratio cible est différent : doit-on rogner ou étendre ?
    # - Si on peut crop la source pour matcher le target → "crop"
    # - Sinon (target a un côté plus grand que source proportionnellement) → "outpaint"
    # En pratique : on peut toujours crop, sauf si la perte est trop importante (>50%)
    # Pour Phase 1, on autorise crop tant que la zone restante fait ≥ 50% de l'image
    if target_ratio > source_ratio:
        # Target plus large que source → on rogne en hauteur
        retain_pct = (sw / target_ratio) / sh
    else:
        # Target plus étroit que source → on rogne en largeur
        retain_pct = (sh * target_ratio) / sw

    if retain_pct >= 0.50:
        return "crop"
    return "outpaint"  # Phase 2 : on skip pour l'instant


# ============================================================
# Resize simple
# ============================================================

def resize_simple(img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    """Resize bicubic vers le target size exact (suppose ratios compatibles)."""
    return img.resize(target_size, Image.Resampling.LANCZOS)


# ============================================================
# Crop intelligent
# ============================================================

def _bbox_to_pixel(bbox: dict, w: int, h: int) -> tuple[int, int, int, int]:
    """Convertit une bbox normalisée en coordonnées pixel (left, top, right, bottom)."""
    left = int(bbox.get("x", 0) * w)
    top = int(bbox.get("y", 0) * h)
    right = int((bbox.get("x", 0) + bbox.get("w", 0)) * w)
    bottom = int((bbox.get("y", 0) + bbox.get("h", 0)) * h)
    return (left, top, right, bottom)


def _placement_window(source_size: tuple[int, int], target_size: tuple[int, int]) -> tuple[int, int]:
    """Calcule la taille de la fenêtre à découper dans la source pour matcher le target ratio.

    Returns (window_width, window_height) — la fenêtre fait la max possible dans la source
    avec le ratio target.
    """
    sw, sh = source_size
    tw, th = target_size
    target_ratio = tw / th

    if sw / sh > target_ratio:
        # Source plus large que target → fenêtre = hauteur source × ratio target
        wh = sh
        ww = int(sh * target_ratio)
    else:
        # Source plus haute que target → fenêtre = largeur source / ratio target
        ww = sw
        wh = int(sw / target_ratio)
    return (ww, wh)


def _score_window(window: tuple[int, int, int, int], safe_zones: dict, source_size: tuple[int, int]) -> float:
    """Score une fenêtre de crop. Plus haut = mieux.

    Heuristiques :
    - Pénalité (lourde) si elle coupe un humain
    - Bonus si elle contient l'amenity principale
    - Bonus si elle contient les critical_zones
    - Bonus pour centrage
    """
    sw, sh = source_size
    wl, wt, wr, wb = window
    score = 100.0

    # Pénalité si coupe un humain
    for hbox in safe_zones.get("humans_bboxes", []) or []:
        hl, ht, hr, hb = _bbox_to_pixel(hbox, sw, sh)
        # Coupe si la fenêtre n'englobe pas entièrement l'humain
        if not (wl <= hl and wt <= ht and wr >= hr and wb >= hb):
            # Combien est coupé ?
            visible_w = max(0, min(wr, hr) - max(wl, hl))
            visible_h = max(0, min(wb, hb) - max(wt, ht))
            human_w = hr - hl
            human_h = hb - ht
            if human_w > 0 and human_h > 0:
                visible_ratio = (visible_w * visible_h) / (human_w * human_h)
                if visible_ratio < 1.0:
                    # Pénalité énorme : couper un humain est inacceptable
                    score -= 1000 * (1 - visible_ratio)

    # Bonus si contient l'amenity principale
    amenity = safe_zones.get("main_amenity_bbox")
    if amenity and amenity.get("w", 0) > 0:
        al, at, ar, ab = _bbox_to_pixel(amenity, sw, sh)
        # Combien de l'amenity est dans la fenêtre ?
        visible_w = max(0, min(wr, ar) - max(wl, al))
        visible_h = max(0, min(wb, ab) - max(wt, at))
        amenity_w = ar - al
        amenity_h = ab - at
        if amenity_w > 0 and amenity_h > 0:
            visible_ratio = (visible_w * visible_h) / (amenity_w * amenity_h)
            score += 30 * visible_ratio

    # Bonus pour centrage : préfère une fenêtre dont le centre est proche du centre de la source
    cw, ch = (wl + wr) / 2, (wt + wb) / 2
    sc_x, sc_y = sw / 2, sh / 2
    dist = ((cw - sc_x) ** 2 + (ch - sc_y) ** 2) ** 0.5
    max_dist = (sw ** 2 + sh ** 2) ** 0.5
    score += 10 * (1 - dist / max_dist)

    return score


# ============================================================
# Outpainting Nano Banana (Phase 2)
# ============================================================

_GENAI_CLIENT = None


def _get_genai_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        from google import genai
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante dans l'environnement")
        _GENAI_CLIENT = genai.Client(api_key=api_key)
    return _GENAI_CLIENT


def _compute_outpaint_canvas(source_size: tuple[int, int], target_size: tuple[int, int]) -> tuple[Image.Image, int, int, int, int]:
    """Construit un canvas RGBA de la taille target avec l'image source placée
    pour préserver son ratio en maximisant sa surface visible.

    Returns: (canvas_blank, paste_x, paste_y, paste_w, paste_h)
    """
    sw, sh = source_size
    tw, th = target_size
    source_ratio = sw / sh
    target_ratio = tw / th

    if source_ratio > target_ratio:
        # Source plus large → on conserve sa largeur, on étend en hauteur
        paste_w = tw
        paste_h = int(tw / source_ratio)
    else:
        # Source plus haute → on conserve sa hauteur, on étend en largeur
        paste_h = th
        paste_w = int(th * source_ratio)

    paste_x = (tw - paste_w) // 2
    paste_y = (th - paste_h) // 2

    canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    return canvas, paste_x, paste_y, paste_w, paste_h


def _aspect_ratio_label(target_size: tuple[int, int]) -> str:
    """Retourne un label lisible style '16:9' ou '9:16' pour le prompt."""
    from math import gcd
    w, h = target_size
    g = gcd(w, h) or 1
    return f"{w // g}:{h // g}"


def _gemini_image_call(client, model: str, prompt: str, image_bytes: bytes, mime: str = "image/png"):
    """Appel Gemini Image. Retourne (img_bytes, text, input_tokens, output_tokens)."""
    from google.genai import types
    response = client.models.generate_content(
        model=model,
        contents=[prompt, types.Part.from_bytes(data=image_bytes, mime_type=mime)],
    )
    img_data = None
    txt = None
    for part in response.candidates[0].content.parts:
        if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
            img_data = part.inline_data.data
            break
        if hasattr(part, "text") and part.text:
            txt = part.text
    # Extract tokens from usage_metadata if available
    u = getattr(response, "usage_metadata", None)
    input_tokens = (getattr(u, "prompt_token_count", 0) or 0) if u else 0
    output_tokens = (getattr(u, "candidates_token_count", 0) or 0) if u else 0
    return img_data, txt, input_tokens, output_tokens


def outpaint_via_nano_banana(
    img: Image.Image,
    target_size: tuple[int, int],
    model: str = NANO_BANANA_FLASH,
    max_retry: int = 1,
) -> tuple[Image.Image, dict]:
    """Étend l'image source pour matcher le ratio target via Nano Banana.

    Args:
        img: image source (Pillow)
        target_size: (width, height) du format cible
        model: modèle Gemini Image (Flash ou Pro)
        max_retry: nb de retry sur erreur API

    Returns:
        (image_pillow_finale, meta_dict)
    """
    t0 = time.time()
    client = _get_genai_client()

    # 1. Construit le canvas RGBA (source placée + zones alpha=0 à remplir)
    canvas, px, py, pw, ph = _compute_outpaint_canvas(img.size, target_size)
    img_resized = img.convert("RGBA").resize((pw, ph), Image.Resampling.LANCZOS)
    canvas.paste(img_resized, (px, py))

    # 2. Sauvegarde canvas en PNG (nécessaire pour préserver l'alpha)
    buf = BytesIO()
    canvas.save(buf, format="PNG")
    image_bytes = buf.getvalue()

    # 3. Construit le prompt en injectant la direction d'extension (anti-tile)
    tw, th = target_size
    extend_dirs = []
    if py > 0:
        extend_dirs.append("the top")
    if (th - py - ph) > 0:
        extend_dirs.append("the bottom")
    if px > 0:
        extend_dirs.append("the left")
    if (tw - px - pw) > 0:
        extend_dirs.append("the right")
    extend_directions = " and ".join(extend_dirs) if extend_dirs else "the surrounding area"

    # Placement description : où la source est dans le canvas
    if py > 0 and (th - py - ph) > 0:
        v_pos = "vertically centered"
    elif py == 0:
        v_pos = "at the top"
    else:
        v_pos = "at the bottom"
    if px > 0 and (tw - px - pw) > 0:
        h_pos = "horizontally centered"
    elif px == 0:
        h_pos = "on the left"
    else:
        h_pos = "on the right"
    placement_desc = f"{v_pos}, {h_pos}"
    source_pct = int(100 * (pw * ph) / max(1, tw * th))

    aspect_label = _aspect_ratio_label(target_size)
    prompt = OUTPAINT_PROMPT_TEMPLATE.format(
        target_aspect_ratio=aspect_label,
        canvas_w=tw,
        canvas_h=th,
        placement_desc=placement_desc,
        source_pct=source_pct,
        extend_directions=extend_directions,
    )

    last_error = None
    cumulated_input_tokens = 0
    cumulated_output_tokens = 0
    for attempt in range(max_retry + 1):
        try:
            img_data, txt, it, ot = _gemini_image_call(client, model, prompt, image_bytes, "image/png")
            cumulated_input_tokens += it
            cumulated_output_tokens += ot
            if img_data:
                # Décode en Pillow
                out_img = Image.open(BytesIO(img_data))
                # Force la taille exacte target (Gemini peut renvoyer une taille légèrement différente)
                if out_img.size != target_size:
                    out_img = out_img.resize(target_size, Image.Resampling.LANCZOS)
                duration_s = round(time.time() - t0, 2)
                cost_usd = COST_FLASH_USD if model == NANO_BANANA_FLASH else COST_PRO_USD
                return out_img.convert("RGB"), {
                    "model": model,
                    "duration_s": duration_s,
                    "cost_usd": cost_usd,
                    "attempts": attempt + 1,
                    "input_tokens": cumulated_input_tokens,
                    "output_tokens": cumulated_output_tokens,
                }
            last_error = f"no image returned (text: {(txt or '')[:120]})"
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:200]}"

    raise RuntimeError(f"Outpainting échec après {max_retry + 1} tentatives ({model}): {last_error}")


def crop_intelligently(img: Image.Image, target_size: tuple[int, int], safe_zones: dict | None) -> Image.Image:
    """Crop la source pour matcher le target ratio en préservant les safe_zones.

    Si safe_zones est None ou vide → crop centré (fallback).
    """
    sw, sh = img.size
    ww, wh = _placement_window((sw, sh), target_size)

    # Cas trivial : la fenêtre fait déjà la taille de la source
    if ww == sw and wh == sh:
        cropped = img
    else:
        # Énumère les positions possibles (par pas de 5%)
        candidates = []
        x_max = sw - ww
        y_max = sh - wh
        step_x = max(1, x_max // 20) if x_max > 0 else 1
        step_y = max(1, y_max // 20) if y_max > 0 else 1
        for x in range(0, max(1, x_max + 1), step_x):
            for y in range(0, max(1, y_max + 1), step_y):
                window = (x, y, x + ww, y + wh)
                score = _score_window(window, safe_zones or {}, (sw, sh))
                candidates.append((score, window))
        if not candidates:
            # Fallback : crop centré
            cx = (sw - ww) // 2
            cy = (sh - wh) // 2
            cropped = img.crop((cx, cy, cx + ww, cy + wh))
        else:
            candidates.sort(key=lambda c: -c[0])
            best_window = candidates[0][1]
            cropped = img.crop(best_window)

    # Resize vers la taille target finale
    return cropped.resize(target_size, Image.Resampling.LANCZOS)


# ============================================================
# Pipeline générale
# ============================================================

def generate_variant(
    img: Image.Image,
    format_spec: dict,
    safe_zones: dict | None,
    outpaint_enabled: bool = False,
    outpaint_quality: str = "flash",  # "flash" ou "pro" (Phase 4 fallback)
) -> tuple[Image.Image | None, str, dict]:
    """Génère une variante d'une photo pour un format donné.

    Args:
        img: image source
        format_spec: dict du format cible
        safe_zones: bbox Gemini (humans, amenity, critical) ou None
        outpaint_enabled: si True, lance outpainting Nano Banana sur les "outpaint" plutôt que skip
        outpaint_quality: "flash" (default, ~$0.039) ou "pro" (~$0.134, fallback Phase 4)

    Returns:
        (image_variant, strategy, meta)
    """
    target_size = (format_spec["width"], format_spec["height"])
    strategy = compute_crop_strategy(img.size, target_size)
    meta = {"strategy": strategy, "source_size": img.size, "target_size": target_size}

    if strategy == "resize":
        return resize_simple(img, target_size), strategy, meta
    if strategy == "crop":
        return crop_intelligently(img, target_size, safe_zones), strategy, meta
    if strategy == "outpaint":
        if not outpaint_enabled:
            meta["warning"] = "outpainting nécessaire — désactivé (active 'outpainting' dans Step 3 pour générer)"
            return None, "skip", meta
        # Phase 2 : outpainting Nano Banana
        model = NANO_BANANA_FLASH if outpaint_quality == "flash" else NANO_BANANA_PRO
        try:
            out_img, op_meta = outpaint_via_nano_banana(img, target_size, model=model)
            meta.update(op_meta)
            return out_img, "outpaint", meta
        except Exception as e:
            # Phase 4 : retry en Pro si Flash a échoué (et qu'on n'était pas déjà sur Pro)
            if outpaint_quality == "flash":
                try:
                    out_img, op_meta = outpaint_via_nano_banana(img, target_size, model=NANO_BANANA_PRO)
                    meta.update(op_meta)
                    meta["fallback_pro"] = True
                    meta["flash_error"] = str(e)[:200]
                    return out_img, "outpaint", meta
                except Exception as e2:
                    meta["error"] = f"Flash + Pro échec: {str(e2)[:200]}"
                    return None, "skip", meta
            meta["error"] = str(e)[:200]
            return None, "skip", meta
    return None, "skip", meta


def run_multi_format(
    enhanced_dir: Path,
    output_dir: Path,
    format_ids: list[str],
    analyses_dir: Path | None = None,
    outpaint_enabled: bool = False,
    outpaint_quality: str = "flash",
    progress_callback=None,
) -> dict:
    """Pour chaque photo finale + chaque format demandé, produit la variante.

    Args:
        enhanced_dir : dossier des photos finales retouchées (data/output/{slug}/enhanced/)
        output_dir : dossier où stocker les variantes (data/output/{slug}/multiformat/)
        format_ids : liste des format ids à produire
        analyses_dir : dossier des analyses Gemini (pour récupérer crop_safe_zones).
                       Si None ou si fichier absent → fallback crop centré.
        outpaint_enabled : si True, active outpainting Nano Banana sur les formats
                           qui nécessiteraient extension (~$0.039 par variante outpaintée).
        outpaint_quality : "flash" (default) ou "pro" (qualité supérieure, ~$0.134).

    Returns:
        manifest avec liste des variantes générées + stratégies + warnings.
    """
    config = load_formats_config()
    formats_to_run = [get_format(config, fid) for fid in format_ids]
    photos = sorted([p for p in enhanced_dir.glob("*.jpg") if p.is_file()])
    if not photos:
        raise RuntimeError(f"Aucune photo trouvée dans {enhanced_dir}")

    # ━━ Cleanup pre-run : on repart d'un dossier clean ━━━━━━━━━━━━━━━━━━━━━━━━━
    # Sans ce wipe, des sous-dossiers/fichiers d'anciens runs (avec d'autres
    # formats cochés) restent sur disque et sont servis par la route Flask
    # /output/<slug>/multiformat → l'utilisateur voit "les anciens formats"
    # alors qu'il n'a pas regénéré (bug rapporté 6/5/26).
    # Politique : un run = un état clean. Tout est régénéré, rien ne fuit.
    import shutil
    if output_dir.exists():
        for sub in output_dir.iterdir():
            if sub.is_dir():
                shutil.rmtree(sub)
            elif sub.is_file() and sub.name in {"manifest.json"}:
                # On supprime l'ancien manifest aussi (sera réécrit en fin de run)
                sub.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "started_at": time.time(),
        "format_ids": format_ids,
        "n_photos": len(photos),
        "n_formats": len(formats_to_run),
        "outpaint_enabled": outpaint_enabled,
        "outpaint_quality": outpaint_quality,
        "variants": [],
        "summary": {
            "resize": 0, "crop": 0, "outpaint": 0, "skip": 0, "errors": 0,
        },
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }

    total_variants = len(photos) * len(formats_to_run)
    print(f"📐 Multi-format : {len(photos)} photos × {len(formats_to_run)} formats = {total_variants} variantes" +
          (f" (outpaint {outpaint_quality})" if outpaint_enabled else ""))
    if progress_callback:
        progress_callback(0, total_variants, "Initialisation multi-format…")
    # ━━━ Pré-charge les images + safe_zones une seule fois par photo ━━━
    # On scope la charge AVANT le pool de workers : ouvrir une image avec Pillow est
    # rapide mais sequencer ça permet de profiter du parallélisme uniquement où il
    # compte (les appels API Gemini Image).
    photo_payloads = []  # liste de (photo_path, img_pillow, safe_zones)
    for photo_path in photos:
        safe_zones = None
        if analyses_dir:
            afile = analyses_dir / f"{photo_path.stem}.json"
            if afile.exists():
                try:
                    a = json.load(afile.open())
                    safe_zones = a.get("analysis", {}).get("crop_safe_zones")
                except Exception:
                    pass
        try:
            img = Image.open(photo_path).convert("RGB")
        except Exception as e:
            print(f"  ❌ {photo_path.name}: load failed ({e})")
            manifest["summary"]["errors"] += 1
            continue
        photo_payloads.append((photo_path, img, safe_zones))

    # ━━━ Génération parallèle des variantes ━━━
    # ThreadPoolExecutor → idéal pour I/O-bound (les outpaints sont des appels HTTP
    # Gemini qui libèrent le GIL pendant l'attente réseau). 3 workers = sweet spot
    # côté rate-limit Gemini Image (~60 RPM tier paid). resize/crop locales tournent
    # quasi instantanément donc pas de gain sur elles, mais ça mange pas de thread non plus.
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    MULTIFORMAT_WORKERS = int(os.environ.get("MULTIFORMAT_WORKERS", "3"))
    progress_lock = threading.Lock()
    manifest_lock = threading.Lock()
    n_done_counter = {"n": 0}

    def _process_one(photo_path: Path, img: Image.Image, safe_zones, fmt: dict) -> dict | None:
        """Worker : génère une variante. Retourne l'entry à ajouter au manifest."""
        fmt_dir = output_dir / fmt["id"]
        fmt_dir.mkdir(exist_ok=True)
        variant_path = fmt_dir / photo_path.name
        try:
            t0 = time.time()
            variant, strategy, meta = generate_variant(
                img, fmt, safe_zones,
                outpaint_enabled=outpaint_enabled,
                outpaint_quality=outpaint_quality,
            )
            duration_ms = int((time.time() - t0) * 1000)

            if variant is None:
                label = meta.get("warning") or meta.get("error") or "skip"
                print(f"  ⚠️  {photo_path.name} → {fmt['id']:<24} | skip ({label[:80]})")
                return {
                    "_summary_bucket": "skip",
                    "entry": {
                        "source": photo_path.name, "format_id": fmt["id"],
                        "strategy": strategy, "duration_ms": duration_ms,
                        "warning": meta.get("warning"),
                        "error": meta.get("error"),
                    },
                }
            variant.save(variant_path, "JPEG", quality=92, optimize=True)
            cost = meta.get("cost_usd", 0)
            entry = {
                "source": photo_path.name, "format_id": fmt["id"],
                "output": str(variant_path.relative_to(output_dir)),
                "strategy": strategy, "duration_ms": duration_ms,
                "source_size": list(meta["source_size"]),
                "target_size": list(meta["target_size"]),
            }
            if cost:
                entry["cost_usd"] = cost
                entry["model"] = meta.get("model")
                entry["input_tokens"] = meta.get("input_tokens", 0)
                entry["output_tokens"] = meta.get("output_tokens", 0)
            if meta.get("fallback_pro"):
                entry["fallback_pro"] = True
            suffix = f" 💰${cost:.3f}" if cost else ""
            suffix += " 🔄fallback Pro" if meta.get("fallback_pro") else ""
            print(f"  ✅ {photo_path.name} → {fmt['id']:<24} | {strategy} ({duration_ms}ms){suffix}")
            return {
                "_summary_bucket": strategy,
                "entry": entry,
                "cost": cost,
                "input_tokens": meta.get("input_tokens", 0) or 0,
                "output_tokens": meta.get("output_tokens", 0) or 0,
            }
        except Exception as e:
            print(f"  ❌ {photo_path.name} → {fmt['id']:<24} | {type(e).__name__}: {e}")
            return {
                "_summary_bucket": "errors",
                "entry": {
                    "source": photo_path.name, "format_id": fmt["id"],
                    "error": f"{type(e).__name__}: {e}",
                },
            }

    # Soumet TOUTES les variantes (photo × format) au pool
    tasks = [(pp, im, sz, f) for (pp, im, sz) in photo_payloads for f in formats_to_run]
    print(f"📐 Parallélisation : {MULTIFORMAT_WORKERS} workers sur {len(tasks)} variantes")

    with ThreadPoolExecutor(max_workers=MULTIFORMAT_WORKERS) as ex:
        futures = [ex.submit(_process_one, pp, im, sz, f) for (pp, im, sz, f) in tasks]
        for fut in as_completed(futures):
            # ━━ Robuste aux exceptions (cf. fix enhance loop) : une variante qui
            #    crashe ne doit pas arrêter le pool entier.
            try:
                result = fut.result()
            except Exception as e:
                print(f"  ❌ worker outpaint crash : {type(e).__name__}: {e}")
                with manifest_lock:
                    manifest["summary"]["errors"] += 1
                continue
            if result is None:
                continue
            with manifest_lock:
                bucket = result["_summary_bucket"]
                if bucket in manifest["summary"]:
                    manifest["summary"][bucket] += 1
                else:
                    manifest["summary"][bucket] = 1
                manifest["variants"].append(result["entry"])
                if "cost" in result:
                    manifest["total_cost_usd"] += result["cost"]
                    manifest["total_input_tokens"] += result["input_tokens"]
                    manifest["total_output_tokens"] += result["output_tokens"]
            # Progress callback (sous lock pour ne pas griller l'UI)
            with progress_lock:
                n_done_counter["n"] += 1
                n_done = n_done_counter["n"]
            if progress_callback:
                progress_callback(
                    n_done, total_variants,
                    f"Variante {n_done}/{total_variants} : {result['entry']['source']} → {result['entry']['format_id']}",
                )

    manifest["completed_at"] = time.time()
    manifest["duration_s"] = round(manifest["completed_at"] - manifest["started_at"], 2)

    # Save manifest
    manifest_path = output_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    s = manifest["summary"]
    print(f"\n📊 Résumé : resize={s['resize']} · crop={s['crop']} · outpaint={s.get('outpaint', 0)} · skip={s['skip']} · erreurs={s['errors']}")
    if manifest["total_cost_usd"] > 0:
        print(f"💰 Coût outpainting : ${manifest['total_cost_usd']:.2f}")
    print(f"⏱  Durée : {manifest['duration_s']}s")
    print(f"📦 Manifest : {manifest_path}")
    manifest["total_cost_usd"] = round(manifest["total_cost_usd"], 4)
    # Re-save avec total final
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    return manifest


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("slug", help="Slug de l'hôtel (ex: booking-yotel-miami)")
    parser.add_argument("--formats", default="", help="Liste de formats séparés par virgule (ex: home_card_mobile,insta_feed)")
    parser.add_argument("--preset", help="Au lieu de --formats : pack_dayuse | pack_social | pack_all")
    parser.add_argument("--outpaint", action="store_true", help="Active outpainting Nano Banana sur les formats à étendre (~$0.04/variante)")
    parser.add_argument("--outpaint-quality", choices=["flash", "pro"], default="flash", help="Qualité outpainting (Flash $0.04 ou Pro $0.13)")
    args = parser.parse_args()

    config = load_formats_config()
    if args.preset:
        format_ids = config["presets"].get(args.preset, [])
        if not format_ids:
            print(f"❌ Preset inconnu : {args.preset}")
            sys.exit(1)
    else:
        format_ids = [f.strip() for f in args.formats.split(",") if f.strip()]
    if not format_ids:
        print("❌ Aucun format spécifié. Utilise --formats ou --preset")
        sys.exit(1)

    enhanced_dir = ROOT / "data" / "output" / args.slug / "enhanced"
    output_dir = ROOT / "data" / "output" / args.slug / "multiformat"
    analyses_dir = ROOT / "data" / "analyses" / args.slug

    if not enhanced_dir.exists():
        print(f"❌ {enhanced_dir} n'existe pas — lance d'abord la pipeline")
        sys.exit(1)

    # Charge .env pour la clé Gemini
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    run_multi_format(
        enhanced_dir, output_dir, format_ids,
        analyses_dir=analyses_dir if analyses_dir.exists() else None,
        outpaint_enabled=args.outpaint,
        outpaint_quality=args.outpaint_quality,
    )
