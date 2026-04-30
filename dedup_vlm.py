"""Dédup sémantique via Gemini Vision (VLM check par paires).

Stratégie en cascade :
  1. pHash strict (THRESHOLD=12) → clusters certains (pixel-near-identique)
  2. Paires en zone grise (13-28 de distance Hamming) → check Gemini Vision
  3. Pré-filtre : on ne check que les paires de MÊME catégorie + subjects similaires
     (évite d'appeler Gemini sur des paires évidemment différentes)

Coût : ~$0.0006 par paire en zone grise. Sur ~30 photos, généralement 5-20 paires à checker
       → ~$0.005 par hôtel. Négligeable.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional
from PIL import Image
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Zone grise pHash : on n'envoie à Gemini que les paires dans cet intervalle
GREY_ZONE_MIN = 13
GREY_ZONE_MAX = 28

# Cap dur pour éviter d'exploser les coûts/temps si beaucoup de paires en zone grise.
# Les paires les plus suspectes (distance la plus faible) sont prioritaires.
MAX_PAIRS_TO_CHECK = 30

# Parallélisme des appels Gemini Vision pour la dédup VLM
VLM_PARALLEL_WORKERS = 3

VALIDATION_MODEL = "gemini-2.5-flash"

VALIDATION_PROMPT = """Tu reçois 2 photos d'hôtel. Détermine si elles montrent la MÊME scène (même sujet principal, même partie de l'hôtel) sous des angles légèrement différents (zoom ou cadrage variant), OU si elles montrent des sujets/zones différents.

Critères "MÊME scène" :
- Même sujet principal identifiable (la même piscine spécifique, le même cabana, la même salle...)
- Mobilier/architecture commune visible dans les 2 photos
- L'une peut être un crop ou un zoom de l'autre

Critères "scènes DIFFÉRENTES" :
- Sujets distincts (piscine A vs piscine B, ou piscine vs spa)
- Zones différentes de l'hôtel
- Angles fondamentalement opposés sur des sujets différents

Retourne UNIQUEMENT un JSON :
{
  "same_scene": true,
  "confidence": "high | medium | low",
  "reason": "1 phrase justifiant"
}

Pas de markdown, pas de texte hors JSON.
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


def _prefilter_compatible(a1: dict, a2: dict) -> bool:
    """Pré-filtre : retourne True si les 2 analyses sont assez similaires
    sémantiquement pour mériter un check VLM. Sinon on skip (économie)."""
    if not a1 or not a2:
        return True  # pas d'info, on check par sécurité

    f1 = a1.get("factual") or {}
    f2 = a2.get("factual") or {}

    # Catégories différentes ET sans recouvrement secondaire → skip
    cat1 = (f1.get("category") or "").lower()
    cat2 = (f2.get("category") or "").lower()
    sec1 = set((f1.get("categories_secondary") or []))
    sec2 = set((f2.get("categories_secondary") or []))
    all_cats_1 = {cat1} | sec1
    all_cats_2 = {cat2} | sec2
    if not (all_cats_1 & all_cats_2):
        return False

    # Si les 2 photos n'ont aucun subject commun, probablement scènes différentes
    s1 = set(s.lower() for s in (f1.get("subjects") or []))
    s2 = set(s.lower() for s in (f2.get("subjects") or []))
    if s1 and s2 and not (s1 & s2):
        # Pas de subject commun → moins probable que ce soit la même scène, mais on check quand même
        # (Gemini peut nommer les subjects différemment)
        pass

    return True


def check_pair_with_vlm(path1: Path, path2: Path,
                        model_name: str = VALIDATION_MODEL,
                        max_retries: int = 2) -> dict:
    """Demande à Gemini Vision si 2 photos sont la même scène. Retourne {same_scene, confidence, reason, cost_usd}."""
    _ensure_configured()
    model = genai.GenerativeModel(model_name)

    last_error = None
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            img1 = Image.open(path1).convert("RGB")
            img2 = Image.open(path2).convert("RGB")
            response = model.generate_content(
                [VALIDATION_PROMPT, img1, img2],
                generation_config={"response_mime_type": "application/json", "temperature": 0.0},
            )
            data = json.loads(response.text)
            usage = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
            output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
            cost_usd = (input_tokens * 0.30 + output_tokens * 2.50) / 1_000_000
            return {
                "same_scene": bool(data.get("same_scene")),
                "confidence": data.get("confidence", "medium"),
                "reason": data.get("reason", ""),
                "duration_ms": int((time.time() - t0) * 1000),
                "cost_usd": round(cost_usd, 6),
            }
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(min(2 ** attempt * 3, 20))
                continue
            break

    return {
        "same_scene": False,
        "confidence": "low",
        "reason": f"VLM check failed: {str(last_error)[:200]}",
        "duration_ms": 0,
        "cost_usd": 0,
        "error": str(last_error)[:200] if last_error else None,
    }


def find_semantic_duplicates(
    paths: list[Path],
    analyses_by_filename: dict[str, dict],
    phash_distances: dict[tuple[str, str], int],
    max_pairs: int = MAX_PAIRS_TO_CHECK,
    parallel_workers: int = VLM_PARALLEL_WORKERS,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> list[dict]:
    """Trouve les paires de photos qui sont sémantiquement le même sujet (via VLM)
    en zone grise pHash.

    Args:
        paths : liste des photos déjà filtrées par pHash strict
        analyses_by_filename : {filename → analysis Gemini}
        phash_distances : {(filename1, filename2) → distance Hamming}
        max_pairs : cap dur sur le nombre de paires checkées (les plus suspectes d'abord)
        parallel_workers : nombre de workers parallèles pour les appels Gemini Vision
        progress_callback : appelée à chaque paire terminée → (done, total, last_pair_label)

    Returns:
        Liste de paires {a, b, same_scene, confidence, reason, cost_usd}.
    """
    # 1) Pré-filtre + collecte des paires candidates
    pairs_to_check = []
    for (f1, f2), dist in phash_distances.items():
        if not (GREY_ZONE_MIN <= dist <= GREY_ZONE_MAX):
            continue
        a1 = analyses_by_filename.get(f1)
        a2 = analyses_by_filename.get(f2)
        if _prefilter_compatible(a1, a2):
            pairs_to_check.append((f1, f2, dist))

    # 2) Tri par distance ASC (les plus suspectes en premier) puis cap
    pairs_to_check.sort(key=lambda x: x[2])
    if len(pairs_to_check) > max_pairs:
        pairs_to_check = pairs_to_check[:max_pairs]

    if not pairs_to_check:
        return []

    name_to_path = {p.name: p for p in paths}

    def _check_one(triplet):
        f1, f2, dist = triplet
        p1, p2 = name_to_path.get(f1), name_to_path.get(f2)
        if not (p1 and p2 and p1.exists() and p2.exists()):
            return None
        check = check_pair_with_vlm(p1, p2)
        return {
            "a": f1, "b": f2,
            "phash_distance": dist,
            "same_scene": check["same_scene"],
            "confidence": check["confidence"],
            "reason": check["reason"],
            "cost_usd": check.get("cost_usd", 0),
            "duration_ms": check.get("duration_ms", 0),
        }

    results = []
    total = len(pairs_to_check)
    done = 0
    # 3) Parallélisation (3 workers par défaut, comme analyze_batch)
    with ThreadPoolExecutor(max_workers=max(1, parallel_workers)) as executor:
        future_to_pair = {executor.submit(_check_one, t): t for t in pairs_to_check}
        for future in as_completed(future_to_pair):
            triplet = future_to_pair[future]
            done += 1
            try:
                r = future.result()
                if r is not None:
                    results.append(r)
            except Exception:
                pass
            if progress_callback:
                try:
                    progress_callback(done, total, f"{triplet[0]} ↔ {triplet[1]}")
                except Exception:
                    pass
    return results
