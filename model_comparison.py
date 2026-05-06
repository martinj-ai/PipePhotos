"""Test comparatif GPT Image vs Nano Banana sur 3 niveaux de qualité.

Pipeline :
- Source : 19 photos sélectionnées par le workflow Yotel Miami (data/uploads/booking-yotel-miami/)
- Pour chaque photo : 6 versions générées (2 modèles × 3 niveaux)
- Output : data/output/booking-yotel-miami/comparison/{photo_id}/{model}_{level}.{ext}
- Rapport : comparison.html (page interactive standalone)

Usage :
    .venv/bin/python model_comparison.py [--max N]   # N = limiter à N photos pour test
    .venv/bin/python model_comparison.py --html-only # ne re-générer que le HTML
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import time
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

load_dotenv()

# ============================================================
# Configuration
# ============================================================

# Mode du test : on cible UNIQUEMENT les photos qui auraient eu une transformation
# IA majeure dans le pipeline (ajout personnages OU nuit→jour). Le prompt utilisé
# par photo est exactement celui que le pipeline aurait utilisé.
TARGET_ACTIONS = {"ai_add_character", "ai_lighting"}

# Persona par défaut pour add_character (le pipeline alterne — on prend "couples"
# comme baseline universelle). Override possible par photo via family_friendly_indicators.
DEFAULT_PERSONA = "couples"

# Catégories autorisées par persona (extrait simplifié de la logique pipeline)
PERSONA_BY_CATEGORY = {
    "piscine": "couples",
    "beach": "couples",
    "rooftop": "couples",
    "cabana": "couples",
    "transat": "couples",
    "f_and_b": "small_groups",
    "interieur_commun": "couples",
    "gym": "solos",
    "spa": "couples",
    "chambre": "couples",
}

# === Scénarios PRÉCIS par photo (pour AB test contrôlé) ===
# Chaque scénario fixe POSITION + POSTURE + ACTION pour que les 6 versions
# d'une même photo soient comparables (sinon chaque modèle choisit une zone
# différente et la comparaison n'isole pas le modèle).
# Différents scénarios entre photos = panel représentatif (eau / canapé / vue / transat / gym).
SCENARIOS = {
    "booking_045_627493912": {
        "label": "Couple sur canapé lounge",
        "directive": (
            "Place EXACTLY 2 people on the central modular sofa: "
            "(1) a woman in casual elegant attire (white linen pants, soft top), seated facing slightly toward her partner, legs crossed, holding a coffee cup or magazine. "
            "(2) a man in casual smart attire (chinos, light shirt), seated next to her, leaning back relaxed against the cushions, one arm resting on the sofa back. "
            "Both are mid-conversation. Realistic relaxed lounge posture. "
            "Natural skin tones, photoreal editorial style, faces with believable expressions."
        ),
    },
    "official_010_unknown_10": {
        "label": "Couple DANS l'eau (immersion poitrine)",
        "directive": (
            "Place EXACTLY 2 people IN THE WATER, standing in the middle of the pool: "
            "(1) a woman in a navy one-piece swimsuit, water up to chest level, facing her partner. "
            "(2) a man in dark swim trunks, water up to chest level, facing the woman, smiling. "
            "🚨 CRITICAL water physics: the water surface MUST cut both bodies cleanly at chest level. "
            "Below water = visible refraction (legs bent, distorted). Above water = wet skin reflections, slight droplets. "
            "Both shoulders/arms above water. They are mid-conversation, relaxed."
        ),
    },
    "official_029_unknown_29": {
        "label": "Solo homme à l'entraînement",
        "directive": (
            "Place EXACTLY 1 person: a man in athletic wear (black shorts, fitted grey t-shirt), "
            "standing at the center of the gym in front of the mirror wall, "
            "holding a dumbbell in his right hand at shoulder level (mid bicep-curl). "
            "Face profile or 3/4 view. Focused expression, slight muscle definition. "
            "Realistic gym lighting, photoreal editorial style."
        ),
    },
    "official_034_unknown_34": {
        "label": "Couple debout vers la baie vitrée",
        "directive": (
            "Place EXACTLY 2 people standing near the floor-to-ceiling window: "
            "(1) a woman in elegant casual wear, in front, leaning casually with one hand on the window frame, looking at the city view outside. "
            "(2) a man slightly behind her, hands in pockets, also looking at the view. "
            "Both seen from behind / 3-quarter view. The view through the window remains fully visible. "
            "Calm contemplative posture. Photoreal editorial style."
        ),
    },
    "official_062_unknown_62": {
        "label": "Couple sur transats face à la vue",
        "directive": (
            "Place EXACTLY 2 people on the rooftop loungers, side by side: "
            "(1) a woman lying back on the chaise lounger, sunglasses on, holding a magazine, head turned slightly toward her partner. "
            "(2) a man sitting upright on the adjacent lounger, holding a glass of water, looking out at the city skyline. "
            "Both in elegant beachwear. Natural relaxed midday posture. Photoreal editorial style."
        ),
    },
    # official_044 = lighting only (pas de scénario perso)
}


def build_scenario_prompt(scenario_directive: str, category: str) -> str:
    """Construit un prompt sur-mesure qui force le scénario PRÉCIS pour AB test contrôlé."""
    return f"""🚨 CRITICAL: This is a STRICT IMAGE EDIT task with a precisely specified scenario.

CONTEXT: This is a hotel {category} photo. You must EDIT this exact image — same composition, same architecture, same furniture, same lighting, same colors, same camera angle. Pixel-level structural preservation of all background elements.

SCENARIO TO PLACE (mandatory exact specification):
{scenario_directive}

🎯 STRICT RULES — read carefully:
- The scene EXCEPT for the placed people must remain pixel-identical to the input
- Place ONLY the people described above, NO MORE, NO LESS
- Respect the EXACT positions, postures and outfits described
- DO NOT add additional people anywhere else in the scene
- DO NOT add new objects, props, drinks, towels, or accessories beyond what's specified
- DO NOT change lighting, time of day, weather, or camera angle
- DO NOT crop, zoom, or reframe
- The placed humans must be photorealistic with believable proportions, faces, and skin tones
- Cast realistic shadows on the existing surfaces (floor, water, lounger)
- Match the existing scene's lighting direction and color temperature exactly

Output: the SAME image with the people placed EXACTLY as described, blending seamlessly with the existing scene."""


# Mapping niveau → config par modèle
LEVELS = {
    "low": {
        "gpt": {"quality": "low"},
        "gemini": {"model": "gemini-2.5-flash-image", "label": "Gemini 2.5 Flash"},
    },
    "medium": {
        "gpt": {"quality": "medium"},
        "gemini": {"model": "gemini-3.1-flash-image-preview", "label": "Nano Banana 2 (Flash)"},
    },
    "high": {
        "gpt": {"quality": "high"},
        "gemini": {"model": "gemini-3-pro-image-preview", "label": "Gemini 3 Pro Image"},
    },
}

# Coûts approx. par image (USD) — utilisés pour le manifest et le total
COSTS = {
    ("gpt", "low"): 0.011,
    ("gpt", "medium"): 0.042,
    ("gpt", "high"): 0.167,
    ("gemini", "low"): 0.039,     # 2.5 Flash
    ("gemini", "medium"): 0.039,  # 3.1 Flash (Nano Banana 2)
    ("gemini", "high"): 0.134,    # 3 Pro
}

OPENAI_MODEL = "gpt-image-2"

# Paths
ROOT = Path(__file__).parent
SOURCE_DIR = ROOT / "data" / "uploads" / "booking-yotel-miami"
SELECTED_DIR = ROOT / "data" / "output" / "booking-yotel-miami" / "enhanced"
OUTPUT_DIR = ROOT / "data" / "output" / "booking-yotel-miami" / "comparison"


# ============================================================
# Clients lazy
# ============================================================

_OPENAI_CLIENT = None
_GENAI_CLIENT = None


def get_openai_client():
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        from openai import OpenAI
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY manquante dans .env")
        _OPENAI_CLIENT = OpenAI(api_key=key)
    return _OPENAI_CLIENT


def get_genai_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        from google import genai
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY manquante dans .env")
        _GENAI_CLIENT = genai.Client(api_key=key)
    return _GENAI_CLIENT


# ============================================================
# Génération GPT Image
# ============================================================

def enhance_with_gpt_image(input_path: Path, output_path: Path, prompt: str, quality: str) -> dict:
    """Edit via gpt-image-1. quality ∈ {low, medium, high}."""
    t0 = time.time()
    client = get_openai_client()

    # gpt-image-1 edit attend du PNG. On convertit, et on cap à 1024 pour rester safe.
    img = Image.open(input_path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # Garder le ratio de l'original. On choisit la "size" la plus proche.
    w, h = img.size
    if w > h:
        target_size = "1536x1024"
        target_w, target_h = 1536, 1024
    elif h > w:
        target_size = "1024x1536"
        target_w, target_h = 1024, 1536
    else:
        target_size = "1024x1024"
        target_w, target_h = 1024, 1024

    # Resize pour matcher exactement la dimension demandée (sinon API erreur 400)
    img.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
    # Padding éventuel pour matcher pile-poil
    if img.size != (target_w, target_h):
        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        offset = ((target_w - img.size[0]) // 2, (target_h - img.size[1]) // 2)
        canvas.paste(img, offset)
        img = canvas

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "input.png"

    response = client.images.edit(
        model=OPENAI_MODEL,
        image=buf,
        prompt=prompt,
        size=target_size,
        quality=quality,
        n=1,
    )

    img_b64 = response.data[0].b64_json
    img_bytes = base64.b64decode(img_b64)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(img_bytes)

    return {
        "duration_s": round(time.time() - t0, 2),
        "cost_usd": COSTS[("gpt", quality)],
        "model": OPENAI_MODEL,
        "level": quality,
        "size": target_size,
    }


# ============================================================
# Génération Gemini Image
# ============================================================

def enhance_with_gemini(input_path: Path, output_path: Path, prompt: str, model: str) -> dict:
    """Edit via Gemini Image (2.5 Flash / 3.1 Flash / 3 Pro)."""
    from google.genai import types
    t0 = time.time()
    client = get_genai_client()

    with open(input_path, "rb") as f:
        image_bytes = f.read()

    suffix = input_path.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(suffix, "image/jpeg")

    def _call(p):
        return client.models.generate_content(
            model=model,
            contents=[p, types.Part.from_bytes(data=image_bytes, mime_type=mime)],
        )

    def _extract(resp):
        img_d, txt = None, None
        for part in resp.candidates[0].content.parts:
            if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
                img_d = part.inline_data.data
                break
            if hasattr(part, "text") and part.text:
                txt = part.text
        return img_d, txt

    response = _call(prompt)
    image_data, text_resp = _extract(response)

    if not image_data:
        # Retry avec préfixe forçant l'image
        forced = ("🚨 STRICT IMAGE GENERATION REQUIRED — DO NOT respond with text or descriptions. "
                  "Apply the editing instruction and RETURN THE EDITED IMAGE as your only output.\n\n" + prompt)
        try:
            response2 = _call(forced)
            image_data, text2 = _extract(response2)
        except Exception as e:
            raise RuntimeError(f"Gemini ({model}) retry failed: {e}")
        if not image_data:
            raise RuntimeError(f"Gemini ({model}) n'a pas retourné d'image. Texte1: {(text_resp or '∅')[:120]}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(image_data)

    # Map model → level for cost
    level = {
        "gemini-2.5-flash-image": "low",
        "gemini-3.1-flash-image-preview": "medium",
        "gemini-3-pro-image-preview": "high",
    }.get(model, "medium")

    return {
        "duration_s": round(time.time() - t0, 2),
        "cost_usd": COSTS[("gemini", level)],
        "model": model,
        "level": level,
    }


# ============================================================
# Pipeline
# ============================================================

ANALYSES_DIR = ROOT / "data" / "analyses" / "booking-yotel-miami"


def determine_action_and_prompt(analysis: dict, photo_id: str = "") -> dict | None:
    """Reproduit la décision du pipeline (cf. enhance._pick_main_action) pour une photo.

    Pour le test AB, on **fixe un scénario précis par photo** (cf. SCENARIOS) afin que les
    6 versions soient comparables (même placement, même posture). Si pas de scénario défini
    pour la photo, on skip (pas inclus dans le test).

    Retourne None si pas une cible. Sinon {action, prompt, reason, scenario_label?}.
    """
    factual = analysis.get("factual", {})
    cat = factual.get("category", "") or ""
    time_of_day = (factual.get("time_of_day") or "").lower()
    human_count = factual.get("human_count", 0) or 0

    # === Règle 1 : nuit/aube/crépuscule → ai_lighting (PROMPT_ENSOLEILLEMENT, identique à pipeline) ===
    if time_of_day in ("nuit", "aube_crepuscule"):
        from enhance import PROMPT_ENSOLEILLEMENT
        return {
            "action": "ai_lighting",
            "prompt": PROMPT_ENSOLEILLEMENT,
            "reason": f"time_of_day={time_of_day} → ensoleillement IA",
            "scenario_label": "Transformation crépuscule → jour ensoleillé",
        }

    # === Règle 2 : ai_add_character avec SCÉNARIO FIXÉ par photo ===
    aac = analysis.get("ai_add_character_candidate") or {}
    is_food_only = (
        cat == "f_and_b"
        and "cocktail" not in " ".join(factual.get("subjects") or []).lower()
    )
    if not aac.get("is_candidate", False):
        return None
    if human_count > 0:
        return None
    if is_food_only or cat == "piscine_vue_aerienne":
        return None

    # Cherche un scénario FIXÉ pour cette photo
    scenario = SCENARIOS.get(photo_id)
    if scenario is None:
        # Pas de scénario fixé → on skip (la photo n'est pas dans le panel de test AB)
        return None

    prompt = build_scenario_prompt(scenario["directive"], cat)
    return {
        "action": "ai_add_character",
        "prompt": prompt,
        "reason": f"AB test — scénario fixé : {scenario['label']}",
        "scenario_label": scenario["label"],
    }


def get_target_photos(max_photos: int | None = None) -> list[dict]:
    """Filtre les 19 photos finales pour ne garder que celles avec ai_add_character ou ai_lighting.

    Retourne une liste de dicts : {path, id, prompt, action, reason, persona?}
    """
    selected_names = sorted([p.name for p in SELECTED_DIR.glob("*.jpg")])
    targets = []
    skipped = []
    for fname in selected_names:
        src_path = SOURCE_DIR / fname
        if not src_path.exists():
            skipped.append((fname, "source manquante"))
            continue
        afile = ANALYSES_DIR / f"{src_path.stem}.json"
        if not afile.exists():
            skipped.append((fname, "analyse manquante"))
            continue
        analysis = json.load(afile.open()).get("analysis", {})
        decision = determine_action_and_prompt(analysis, photo_id=src_path.stem)
        if decision is None:
            cat = analysis.get("factual", {}).get("category", "?")
            skipped.append((fname, f"pas une cible (cat={cat})"))
            continue
        targets.append({
            "path": src_path,
            "id": src_path.stem,
            **decision,
        })

    print(f"📋 {len(targets)} photos cibles / {len(selected_names)} photos finales")
    for t in targets:
        action_emoji = "🌙" if t["action"] == "ai_lighting" else "👤"
        print(f"  {action_emoji} {t['id']} → {t['action']} | {t['reason']}")
    if skipped and len(skipped) < 15:
        print(f"  [skipped: {len(skipped)} photos non-cibles]")

    if not targets:
        raise RuntimeError("Aucune photo cible (add_character / ai_lighting) trouvée")
    if max_photos:
        targets = targets[:max_photos]
    return targets


def run_comparison(max_photos: int | None = None) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    targets = get_target_photos(max_photos=max_photos)
    total = len(targets) * 6
    print(f"\n🚀 Comparaison sur {len(targets)} photos cibles × 6 versions = {total} générations\n")

    manifest_path = OUTPUT_DIR / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
    else:
        manifest = {"results": [], "started_at": time.time(), "photos_meta": {}}

    if "photos_meta" not in manifest:
        manifest["photos_meta"] = {}

    existing = {(r["photo"], r["model"], r["level"]) for r in manifest["results"] if "error" not in r}

    n = 0
    for target in targets:
        photo = target["path"]
        photo_id = target["id"]
        prompt = target["prompt"]
        photo_dir = OUTPUT_DIR / photo_id
        photo_dir.mkdir(exist_ok=True)

        # Sauver le prompt + métadonnées de la photo dans le manifest
        manifest["photos_meta"][photo_id] = {
            "action": target["action"],
            "reason": target["reason"],
            "scenario_label": target.get("scenario_label"),
            "prompt": prompt,
        }

        # Copier l'original pour référence dans l'HTML
        original_copy = photo_dir / "00_original.jpg"
        if not original_copy.exists():
            shutil.copy(photo, original_copy)

        for level in ["low", "medium", "high"]:
            # GPT Image
            n += 1
            out_gpt = photo_dir / f"gpt_{level}.png"
            key_gpt = (photo_id, OPENAI_MODEL, level)
            if out_gpt.exists() and key_gpt in existing:
                print(f"  [{n}/{total}] {photo_id} | {OPENAI_MODEL} {level} (cached)")
            else:
                try:
                    print(f"  [{n}/{total}] {photo_id} | {OPENAI_MODEL} {level}…", end="", flush=True)
                    cfg = LEVELS[level]["gpt"]
                    meta = enhance_with_gpt_image(photo, out_gpt, prompt, **cfg)
                    print(f" ✅ {meta['duration_s']:.1f}s ${meta['cost_usd']:.3f}")
                    manifest["results"].append({"photo": photo_id, **meta, "output": str(out_gpt.relative_to(OUTPUT_DIR))})
                    _save_manifest(manifest_path, manifest)
                except Exception as e:
                    print(f" ❌ {type(e).__name__}: {e}")
                    manifest["results"].append({"photo": photo_id, "model": OPENAI_MODEL, "level": level, "error": f"{type(e).__name__}: {e}"})
                    _save_manifest(manifest_path, manifest)

            # Gemini
            n += 1
            cfg = LEVELS[level]["gemini"]
            out_gem = photo_dir / f"gemini_{level}.jpg"
            key_gem = (photo_id, cfg["model"], level)
            if out_gem.exists() and key_gem in existing:
                print(f"  [{n}/{total}] {photo_id} | {cfg['label']} (cached)")
            else:
                try:
                    print(f"  [{n}/{total}] {photo_id} | {cfg['label']}…", end="", flush=True)
                    meta = enhance_with_gemini(photo, out_gem, prompt, model=cfg["model"])
                    print(f" ✅ {meta['duration_s']:.1f}s ${meta['cost_usd']:.3f}")
                    manifest["results"].append({"photo": photo_id, **meta, "output": str(out_gem.relative_to(OUTPUT_DIR))})
                    _save_manifest(manifest_path, manifest)
                except Exception as e:
                    print(f" ❌ {type(e).__name__}: {e}")
                    manifest["results"].append({"photo": photo_id, "model": cfg["model"], "level": level, "error": f"{type(e).__name__}: {e}"})
                    _save_manifest(manifest_path, manifest)

    manifest["completed_at"] = time.time()
    manifest["total_cost_usd"] = round(sum(r.get("cost_usd", 0) for r in manifest["results"]), 3)
    _save_manifest(manifest_path, manifest)

    print(f"\n📊 Total cost: ${manifest['total_cost_usd']:.2f}")
    print(f"📦 {len([r for r in manifest['results'] if 'error' not in r])}/{total} versions générées")
    errors = [r for r in manifest["results"] if "error" in r]
    if errors:
        print(f"⚠️  {len(errors)} erreurs — voir manifest.json")
    return manifest


def _save_manifest(path: Path, manifest: dict):
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


# ============================================================
# HTML interactif
# ============================================================

def build_html() -> Path:
    manifest_path = OUTPUT_DIR / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"manifest.json absent — lance d'abord run_comparison()")
    with open(manifest_path) as f:
        manifest = json.load(f)

    # Charge les verdicts d'analyse (optionnel — si fichier présent)
    verdicts_path = OUTPUT_DIR / "verdicts.json"
    verdicts = json.load(verdicts_path.open()) if verdicts_path.exists() else {}

    by_photo: dict[str, dict] = {}
    for r in manifest["results"]:
        photo_id = r["photo"]
        if photo_id not in by_photo:
            by_photo[photo_id] = {}
        # Key: (model_family, level)
        family = "gpt" if r.get("model", "").startswith("gpt") else "gemini"
        level = r.get("level", "?")
        by_photo[photo_id][f"{family}_{level}"] = r

    photos = sorted(by_photo.keys())

    photos_meta = manifest.get("photos_meta", {})

    # Préparer payload JSON pour le JS
    photos_data = []
    for p in photos:
        original_path = f"{p}/00_original.jpg"
        cells = {}
        for family in ["gpt", "gemini"]:
            for level in ["low", "medium", "high"]:
                key = f"{family}_{level}"
                r = by_photo[p].get(key)
                if r:
                    cells[key] = {
                        "model": r.get("model"),
                        "level": r.get("level"),
                        "duration_s": r.get("duration_s"),
                        "cost_usd": r.get("cost_usd"),
                        "output": r.get("output"),
                        "error": r.get("error"),
                    }
                else:
                    cells[key] = {"error": "not generated"}
        meta = photos_meta.get(p, {})

        # Injecter les verdicts par cellule s'ils existent
        photo_verdicts = verdicts.get(p, {})
        verdict_cells = photo_verdicts.get("cells", {})
        for cell_key in cells:
            v = verdict_cells.get(cell_key)
            if v:
                cells[cell_key]["verdict"] = v.get("verdict")
                cells[cell_key]["score"] = v.get("score")
                cells[cell_key]["respected"] = v.get("respected", [])
                cells[cell_key]["failed"] = v.get("failed", [])

        photos_data.append({
            "id": p,
            "original": original_path,
            "cells": cells,
            "action": meta.get("action"),
            "reason": meta.get("reason"),
            "scenario_label": meta.get("scenario_label"),
            "prompt": meta.get("prompt"),
            "winner": photo_verdicts.get("winner"),
        })

    total_cost = manifest.get("total_cost_usd", 0)
    n_ok = len([r for r in manifest["results"] if "error" not in r])
    n_total = len(photos) * 6
    global_summary = verdicts.get("_global_summary") if verdicts else None

    html = _render_html(photos_data, total_cost, n_ok, n_total, global_summary)
    out = OUTPUT_DIR / "comparison.html"
    with open(out, "w") as f:
        f.write(html)
    print(f"✅ HTML généré : {out}")
    return out


def _render_html(photos_data, total_cost, n_ok, n_total, global_summary=None) -> str:
    """Charge le template HTML et injecte les variables runtime."""
    template_path = Path(__file__).parent / "templates" / "comparison_template.html"
    template = template_path.read_text()
    return (template
            .replace("__PAYLOAD__", json.dumps(photos_data))
            .replace("__N_PHOTOS__", str(len(photos_data)))
            .replace("__N_OK__", str(n_ok))
            .replace("__N_TOTAL__", str(n_total))
            .replace("__TOTAL_COST__", f"{total_cost:.2f}")
            .replace("__GLOBAL_SUMMARY__", json.dumps(global_summary or {})))



# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=None, help="Limiter à N photos (test rapide)")
    parser.add_argument("--html-only", action="store_true", help="Ne rebuild que le HTML depuis le manifest")
    args = parser.parse_args()

    if args.html_only:
        build_html()
        sys.exit(0)

    if not SOURCE_DIR.exists():
        print(f"❌ {SOURCE_DIR} n'existe pas — assure-toi que le pipeline a tourné")
        sys.exit(1)
    if not SELECTED_DIR.exists():
        print(f"❌ {SELECTED_DIR} n'existe pas — pas de photos sélectionnées")
        sys.exit(1)

    run_comparison(max_photos=args.max)
    build_html()
    print(f"\n👉 Ouvre : {(OUTPUT_DIR / 'comparison.html').resolve()}")
