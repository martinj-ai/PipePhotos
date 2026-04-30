"""Re-vérification de l'amenity_dominance via 2e passe Gemini Vision (Q4 Martin).

Pourquoi : la 1ère passe d'analyse retourne `amenity_dominance.primary_amenity_visible_pct`
mais Gemini est parfois trop généreux (une piscine en arrière-plan flou tagged 60% alors
qu'elle est ~15% du cadre réel). Conséquence : photos lifestyle close-up remontent en top.

Solution : pour les TOP CANDIDATES SLOT 1 uniquement, on appelle un 2e prompt court et focalisé
qui demande "vraiment, est-ce que [amenity] est le sujet principal ici ?". On corrige la dominance
en mémoire, ce qui plombe le score des photos lifestyle déguisées.

Coût : ~$0.0003 par appel × ~3-5 photos / hôtel = $0.0015. Négligeable.

Usage :
    from amenity_verifier import verify_amenity_focus
    result = verify_amenity_focus(image_path, claimed_category="pool")
    # → {is_focused: bool, real_dominance_pct: int, reason: str}
"""

from __future__ import annotations

import json
import os
import re as _re
import time
from pathlib import Path
from PIL import Image
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

VERIFICATION_MODEL = "gemini-2.5-flash"

# Mapping amenity → description naturelle pour le prompt
AMENITY_DESCRIPTION = {
    "pool":     "swimming pool (the body of water itself, visible as the main subject)",
    "cabana":   "cabana / daybed pavilion (covered structure with lounging furniture inside)",
    "rooftop":  "rooftop terrace / sky-deck (elevated outdoor area with skyline or terrace view)",
    "spa":      "spa amenity (massage tables, sauna, hammam, jacuzzi, treatment rooms — wellness setting)",
    "beach":    "beach (sand, ocean, beachfront setting — at ground level)",
    "food":     "food / restaurant (served plates, dressed dining tables, kitchen)",
    "bar":      "bar amenity (cocktails, drinks served, bar counter)",
    "hero_ext": "exterior facade / outdoor venue view of the hotel",
    "detail":   "interior detail / common interior space",
}

VERIFICATION_PROMPT_TEMPLATE = """Look at this hotel photo. Is {amenity_desc} CLEARLY THE MAIN SUBJECT of this photo?

Return ONLY a JSON object:

{{
  "is_focused": true,
  "real_dominance_pct": 0,
  "reason": "1 short sentence justifying"
}}

Definitions:
- is_focused = true ONLY if the amenity occupies a meaningful portion of the frame AND is what the photo is "about"
- real_dominance_pct = 0-100, % of the visual frame the amenity actually occupies as the dominant subject

Examples:
- A wide shot of the pool with loungers in foreground → is_focused=true, ~70%
- A close-up of someone's torso in a bikini with the pool in soft background → is_focused=false, ~10%
- An underwater portrait of a person swimming → is_focused=false, ~5% (the subject is the person, not the pool itself)
- A cocktail glass close-up on a poolside table with pool in background → is_focused=false (it's about the cocktail, not the pool)
- A vue aerienne of the pool from above → is_focused=true, ~80%

Return only the JSON, no markdown, no explanation outside the JSON.
"""


_GENAI_CONFIGURED = False


def _ensure_configured():
    global _GENAI_CONFIGURED
    if not _GENAI_CONFIGURED:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquante")
        genai.configure(api_key=api_key)
        _GENAI_CONFIGURED = True


def verify_amenity_focus(image_path: Path, claimed_category: str,
                          model_name: str = VERIFICATION_MODEL,
                          max_retries: int = 1) -> dict:
    """Demande à Gemini si la photo montre vraiment [claimed_category] comme sujet principal.

    Args:
        image_path : path local de la photo
        claimed_category : catégorie cible coverage (pool/cabana/rooftop/spa/beach/food/bar)

    Returns:
        {
          is_focused: bool,
          real_dominance_pct: int (0-100),
          reason: str,
          duration_ms: int,
          cost_usd: float,
          error: str | None
        }
    """
    _ensure_configured()
    amenity_desc = AMENITY_DESCRIPTION.get(claimed_category, claimed_category)
    prompt = VERIFICATION_PROMPT_TEMPLATE.format(amenity_desc=amenity_desc)

    last_error = None
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            img = Image.open(image_path).convert("RGB")
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(
                [prompt, img],
                generation_config={"response_mime_type": "application/json", "temperature": 0.0},
            )
            data = json.loads(response.text)
            usage = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
            output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
            cost_usd = (input_tokens * 0.30 + output_tokens * 2.50) / 1_000_000

            real_dom = data.get("real_dominance_pct", 0)
            try:
                real_dom = int(real_dom)
            except (ValueError, TypeError):
                real_dom = 0

            return {
                "is_focused": bool(data.get("is_focused")),
                "real_dominance_pct": max(0, min(100, real_dom)),
                "reason": (data.get("reason") or "")[:200],
                "claimed_category": claimed_category,
                "duration_ms": int((time.time() - t0) * 1000),
                "cost_usd": round(cost_usd, 6),
                "error": None,
            }
        except Exception as e:
            last_error = e
            err = str(e)
            is_retryable = "429" in err or "500" in err or "503" in err
            if is_retryable and attempt < max_retries:
                m = _re.search(r"retry in (\d+(?:\.\d+)?)\s*s", err)
                wait = (float(m.group(1)) + 2) if m else 5
                time.sleep(wait)
                continue
            break

    # Fallback : on suppose que c'est OK (don't block le pipeline)
    return {
        "is_focused": True,
        "real_dominance_pct": 50,
        "reason": f"verification failed: {str(last_error)[:150]}",
        "claimed_category": claimed_category,
        "duration_ms": 0,
        "cost_usd": 0,
        "error": str(last_error)[:200] if last_error else None,
    }


def verify_top_candidates(analyses: list[dict], coverage_result: dict,
                          n_per_bucket: int | None = None,
                          parallel: int = 3) -> list[dict]:
    """Re-vérifie les candidats de chaque bucket amenity (slot 1 candidates).

    Met à jour `amenity_dominance.primary_amenity_visible_pct` IN-PLACE dans les analyses
    si le verifier détecte que la photo n'est pas focus sur l'amenity.

    Args:
        analyses : sortie de analyze.analyze_batch (avec input.path_absolute)
        coverage_result : sortie de coverage.compute_coverage (avec by_category)
        n_per_bucket : combien de top-photos par bucket on re-vérifie. None = toutes
            (plus sûr car Gemini hallucine sur les positions arbitraires).
        parallel : nb workers parallèles pour les appels Gemini

    Returns:
        Liste des résultats de vérification, format dict (logging/debug + UI).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Identifier les candidats à re-vérifier (toutes les photos d'un bucket amenity)
    by_filename = {a["input"]["filename"]: a for a in analyses}
    top_candidates: list[tuple[str, str]] = []  # (filename, claimed_amenity)
    AMENITY_BUCKETS = {"pool", "cabana", "rooftop", "spa", "beach", "food", "bar"}

    for cat, info in (coverage_result.get("by_category") or {}).items():
        if cat not in AMENITY_BUCKETS:
            continue
        photos = info.get("photos") or []
        if n_per_bucket is not None:
            photos = photos[:n_per_bucket]
        for ph in photos:
            top_candidates.append((ph["filename"], cat))

    # Dedup (une photo peut être top-3 dans plusieurs buckets)
    seen = set()
    unique_candidates = []
    for fname, cat in top_candidates:
        key = (fname, cat)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append((fname, cat))

    results: list[dict] = []

    def _verify_one(item):
        fname, cat = item
        a = by_filename.get(fname)
        if not a or "input" not in a:
            return None
        path = Path(a["input"]["path_absolute"])
        if not path.exists():
            return None
        res = verify_amenity_focus(path, cat)
        res["filename"] = fname
        return res

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        future_to_item = {ex.submit(_verify_one, item): item for item in unique_candidates}
        for fut in as_completed(future_to_item):
            try:
                r = fut.result()
                if r is not None:
                    results.append(r)
            except Exception:
                pass

    # Application : si is_focused=false → on baisse la dominance dans l'analyse
    # Si is_focused=true → on confirme avec real_dominance_pct (peut être plus élevé que la 1ère passe)
    # Note : on prend le PLUS BAS entre claim original et verify, sécurité.
    for r in results:
        a = by_filename.get(r["filename"])
        if not a or not a.get("analysis"):
            continue
        ad = a["analysis"].setdefault("amenity_dominance", {})
        original_dom = ad.get("primary_amenity_visible_pct") or 0
        try:
            original_dom = int(original_dom)
        except (ValueError, TypeError):
            original_dom = 0

        if not r["is_focused"]:
            # Photo lifestyle déguisée : on plafonne à la valeur du verifier (généralement basse)
            ad["primary_amenity_visible_pct"] = min(original_dom, r["real_dominance_pct"])
            ad["is_amenity_focused"] = False
            ad["verifier_reason"] = r["reason"]
        else:
            # Photo OK : on confirme. Si verifier dit moins que la 1ère passe, on ajuste prudemment.
            ad["primary_amenity_visible_pct"] = min(original_dom, r["real_dominance_pct"]) \
                if r["real_dominance_pct"] > 0 else original_dom
            ad["is_amenity_focused"] = True
            ad["verifier_reason"] = r["reason"]

    return results


# CLI debug
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python amenity_verifier.py <image_path> <amenity_category>")
        sys.exit(1)
    res = verify_amenity_focus(Path(sys.argv[1]), sys.argv[2])
    print(json.dumps(res, indent=2, ensure_ascii=False))
