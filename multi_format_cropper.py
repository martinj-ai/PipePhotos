"""Multi-format crop pipeline (Phase 1 - Pillow only).

Étape 5 du pipeline : pour chaque photo finale + chaque format demandé,
produit une variante au ratio cible :
- Resize simple si Δratio ≤ 5%
- Crop intelligent (Pillow + safe_zones Gemini) si crop possible
- Skip + warning si extension nécessaire (réservé Phase 2 avec outpainting)

Output : dossier par format + manifest.json + ZIP final.

Usage standalone :
    .venv/bin/python multi_format_cropper.py <slug> --formats home_card_mobile,insta_feed
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config" / "output_formats.json"

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

def generate_variant(img: Image.Image, format_spec: dict, safe_zones: dict | None) -> tuple[Image.Image | None, str, dict]:
    """Génère une variante d'une photo pour un format donné.

    Returns:
        (image_variant, strategy, meta)
        image_variant = None si la stratégie est "skip" (Phase 2 outpainting)
    """
    target_size = (format_spec["width"], format_spec["height"])
    strategy = compute_crop_strategy(img.size, target_size)
    meta = {"strategy": strategy, "source_size": img.size, "target_size": target_size}

    if strategy == "resize":
        return resize_simple(img, target_size), strategy, meta
    if strategy == "crop":
        return crop_intelligently(img, target_size, safe_zones), strategy, meta
    if strategy == "outpaint":
        # Phase 2 : pour l'instant on skip avec warning
        meta["warning"] = "outpainting nécessaire — reporté en Phase 2"
        return None, "skip", meta
    return None, "skip", meta


def run_multi_format(
    enhanced_dir: Path,
    output_dir: Path,
    format_ids: list[str],
    analyses_dir: Path | None = None,
) -> dict:
    """Pour chaque photo finale + chaque format demandé, produit la variante.

    Args:
        enhanced_dir : dossier des photos finales retouchées (data/output/{slug}/enhanced/)
        output_dir : dossier où stocker les variantes (data/output/{slug}/multiformat/)
        format_ids : liste des format ids à produire
        analyses_dir : dossier des analyses Gemini (pour récupérer crop_safe_zones).
                       Si None ou si fichier absent → fallback crop centré.

    Returns:
        manifest avec liste des variantes générées + stratégies + warnings.
    """
    config = load_formats_config()
    formats_to_run = [get_format(config, fid) for fid in format_ids]
    photos = sorted([p for p in enhanced_dir.glob("*.jpg") if p.is_file()])
    if not photos:
        raise RuntimeError(f"Aucune photo trouvée dans {enhanced_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "started_at": time.time(),
        "format_ids": format_ids,
        "n_photos": len(photos),
        "n_formats": len(formats_to_run),
        "variants": [],
        "summary": {
            "resize": 0, "crop": 0, "skip": 0, "errors": 0,
        },
    }

    print(f"📐 Multi-format : {len(photos)} photos × {len(formats_to_run)} formats = {len(photos) * len(formats_to_run)} variantes")

    for photo_path in photos:
        # Charger l'analyse pour récupérer crop_safe_zones
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

        for fmt in formats_to_run:
            fmt_dir = output_dir / fmt["id"]
            fmt_dir.mkdir(exist_ok=True)
            variant_path = fmt_dir / photo_path.name

            try:
                t0 = time.time()
                variant, strategy, meta = generate_variant(img, fmt, safe_zones)
                duration_ms = int((time.time() - t0) * 1000)

                if variant is None:
                    manifest["summary"]["skip"] += 1
                    manifest["variants"].append({
                        "source": photo_path.name, "format_id": fmt["id"],
                        "strategy": strategy, "duration_ms": duration_ms,
                        "warning": meta.get("warning"),
                    })
                    print(f"  ⚠️  {photo_path.name} → {fmt['id']:<24} | skip ({meta.get('warning', '')})")
                else:
                    variant.save(variant_path, "JPEG", quality=92, optimize=True)
                    manifest["summary"][strategy] += 1
                    manifest["variants"].append({
                        "source": photo_path.name, "format_id": fmt["id"],
                        "output": str(variant_path.relative_to(output_dir)),
                        "strategy": strategy, "duration_ms": duration_ms,
                        "source_size": list(meta["source_size"]),
                        "target_size": list(meta["target_size"]),
                    })
                    print(f"  ✅ {photo_path.name} → {fmt['id']:<24} | {strategy} ({duration_ms}ms)")
            except Exception as e:
                manifest["summary"]["errors"] += 1
                print(f"  ❌ {photo_path.name} → {fmt['id']:<24} | {type(e).__name__}: {e}")
                manifest["variants"].append({
                    "source": photo_path.name, "format_id": fmt["id"],
                    "error": f"{type(e).__name__}: {e}",
                })

    manifest["completed_at"] = time.time()
    manifest["duration_s"] = round(manifest["completed_at"] - manifest["started_at"], 2)

    # Save manifest
    manifest_path = output_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    s = manifest["summary"]
    print(f"\n📊 Résumé : resize={s['resize']} · crop={s['crop']} · skip={s['skip']} · erreurs={s['errors']}")
    print(f"⏱  Durée : {manifest['duration_s']}s")
    print(f"📦 Manifest : {manifest_path}")
    return manifest


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("slug", help="Slug de l'hôtel (ex: booking-yotel-miami)")
    parser.add_argument("--formats", required=True, help="Liste de formats séparés par virgule (ex: home_card_mobile,insta_feed)")
    parser.add_argument("--preset", help="Au lieu de --formats, choisir un preset : pack_dayuse | pack_social | pack_all")
    args = parser.parse_args()

    config = load_formats_config()
    if args.preset:
        format_ids = config["presets"].get(args.preset, [])
        if not format_ids:
            print(f"❌ Preset inconnu : {args.preset}")
            sys.exit(1)
    else:
        format_ids = [f.strip() for f in args.formats.split(",") if f.strip()]

    enhanced_dir = ROOT / "data" / "output" / args.slug / "enhanced"
    output_dir = ROOT / "data" / "output" / args.slug / "multiformat"
    analyses_dir = ROOT / "data" / "analyses" / args.slug

    if not enhanced_dir.exists():
        print(f"❌ {enhanced_dir} n'existe pas — lance d'abord la pipeline")
        sys.exit(1)

    run_multi_format(enhanced_dir, output_dir, format_ids,
                     analyses_dir=analyses_dir if analyses_dir.exists() else None)
