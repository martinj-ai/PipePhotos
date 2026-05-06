"""Module slowmotion : génère un cinemagraph loop seamless via Higgsfield + ffmpeg.

Voir docs/SLOWMO_SPEC.md pour la rationale (FLF natif indispo sur l'API officielle,
fallback ping-pong via ffmpeg, alternatives écartées).

Usage programmatique :
    from slowmo_higgsfield import pick_slowmo_target, generate_slowmo
    target = pick_slowmo_target(ordered_pack, by_filename)
    if target:
        result = generate_slowmo(input_path, target["motion_subject"], output_path)

Pipeline interne (1 photo finale par hôtel) :
    1. Upload du JPEG vers Higgsfield (upload_file → URL CDN)
    2. POST kling-video/v2.1/pro/image-to-video (5s, prompt motion ambiant)
    3. Download du mp4 généré
    4. Post-process ffmpeg : ping-pong (clip + reverse) → loop mathématiquement parfait
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# --- Config ---

# Modèle Higgsfield. kling-video/v2.1/pro = meilleur compromis qualité/cohérence.
# Alternatives officielles testables : higgsfield-ai/dop/standard, bytedance/seedance/v1/pro/image-to-video
HIGGSFIELD_MODEL = os.getenv("HIGGSFIELD_SLOWMO_MODEL", "kling-video/v2.1/pro/image-to-video")
HIGGSFIELD_DURATION_S = int(os.getenv("HIGGSFIELD_SLOWMO_DURATION", "5"))

# Pricing approximatif (USD) — à ajuster selon facturation réelle Higgsfield.
# Kling 2.1 Pro 5s ≈ $0.35, DoP standard ≈ $0.10
HIGGSFIELD_PRICE_USD = float(os.getenv("HIGGSFIELD_SLOWMO_PRICE_USD", "0.35"))

# Mode dry-run : skip l'appel API, copie un placeholder mp4 si dispo (tests UI).
DRY_RUN = os.getenv("SLOWMO_DRY_RUN", "0") == "1"


# --- Prompts par sujet de mouvement ---
# Volonté : mouvement AMBIANT et NON-DIRECTIONNEL pour que le ping-pong soit invisible.
# Mots-clés négatifs : "no camera movement", "static composition" — Kling DoP a tendance
# à pousser des camera moves cinématiques par défaut, on les coupe.

PROMPTS_BY_SUBJECT = {
    "water": (
        "Subtle gentle water ripples on the surface, soft slow ambient movement, "
        "natural reflection shimmer. Static composition, locked-off shot, "
        "no camera movement, no zoom, no pan. People and objects remain perfectly still. "
        "Only the water surface moves softly."
    ),
    "curtains": (
        "Soft gentle breeze making the curtains and light fabrics sway slowly, "
        "ambient drift. Static composition, locked-off shot, no camera movement. "
        "Everything else remains still."
    ),
    "foliage": (
        "Gentle wind softly moving the leaves and plants, ambient natural sway. "
        "Static composition, locked-off shot, no camera movement, no zoom, no pan. "
        "Only foliage moves subtly."
    ),
    "fire": (
        "Gentle dancing flames, soft warm flicker, slow ember glow. "
        "Static composition, locked-off shot, no camera movement. "
        "Everything else remains perfectly still."
    ),
    "steam": (
        "Soft slow rising steam and mist, gentle ambient drift. "
        "Static composition, locked-off shot, no camera movement. "
        "Background and objects remain still."
    ),
    "fountain": (
        "Gentle water flow from the fountain, soft continuous splashing, "
        "ambient water motion. Static composition, locked-off shot, no camera movement. "
        "Everything else remains still."
    ),
}

PROMPT_FALLBACK = (
    "Subtle ambient atmosphere with very gentle natural motion. "
    "Static composition, locked-off shot, no camera movement, no zoom, no pan. "
    "Photorealistic, high quality."
)


def _ffmpeg_bin() -> str:
    """Retourne le chemin du binaire ffmpeg (système ou via imageio-ffmpeg)."""
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return sys_ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError(
            "ffmpeg introuvable. Installe `imageio-ffmpeg` (pip) ou `brew install ffmpeg`."
        )


def has_credentials() -> bool:
    """Vérifie qu'on a les credentials Higgsfield."""
    return bool(
        os.getenv("HF_KEY")
        or (os.getenv("HF_API_KEY") and os.getenv("HF_API_SECRET"))
    )


def pick_slowmo_target(
    ordered_pack: list[dict],
    by_filename: dict[str, dict],
) -> Optional[dict]:
    """Choisit la photo du pack final qui sera convertie en slowmo.

    Stratégie :
      1. Parmi les photos du pack final ordonné, ne garder que celles avec
         `slowmo_potential.has_motion_subject == True`.
      2. Trier par `motion_strength` décroissant.
      3. Tie-break : préférer slot 1 (hero), puis ordre du pack.
      4. Fallback si aucune candidate qualifiée : slot 1 avec subject="ambient".

    Returns:
        dict {filename, motion_subject, motion_strength, slot, fallback_used} ou None.
    """
    candidates = []
    for slot, entry in enumerate(ordered_pack, 1):
        filename = entry["input"]["filename"]
        a = by_filename.get(filename) or {}
        analysis = a.get("analysis") or {}
        sp = analysis.get("slowmo_potential") or {}

        if sp.get("has_motion_subject"):
            candidates.append({
                "filename": filename,
                "motion_subject": sp.get("motion_subject", "water"),
                "motion_strength": int(sp.get("motion_strength", 0) or 0),
                "slot": slot,
                "fallback_used": False,
            })

    if candidates:
        # Tri : motion_strength desc, puis slot asc (slot 1 prioritaire en cas d'égalité)
        candidates.sort(key=lambda c: (-c["motion_strength"], c["slot"]))
        return candidates[0]

    # Fallback : slot 1
    if ordered_pack:
        return {
            "filename": ordered_pack[0]["input"]["filename"],
            "motion_subject": "ambient",
            "motion_strength": 0,
            "slot": 1,
            "fallback_used": True,
        }
    return None


def _download(url: str, dest: Path) -> None:
    """Télécharge une URL vers un chemin local."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _ping_pong_loop(input_mp4: Path, output_mp4: Path) -> None:
    """Génère un loop ping-pong (forward + reverse) à partir d'un clip.

    Résultat : durée doublée, mathématiquement seamless (la dernière frame du
    forward = la première du reverse, et vice-versa).
    """
    ff = _ffmpeg_bin()
    cmd = [
        ff, "-y",
        "-i", str(input_mp4),
        "-filter_complex", "[0:v]reverse[r];[0:v][r]concat=n=2:v=1:a=0[v]",
        "-map", "[v]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(output_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg ping-pong failed: {proc.stderr[:500]}")


def generate_slowmo(
    input_path: Path,
    motion_subject: str,
    output_path: Path,
    *,
    model: Optional[str] = None,
    duration_s: Optional[int] = None,
) -> dict:
    """Génère un slowmo loop à partir d'une photo finale.

    Args:
        input_path : photo source (JPEG/PNG/WEBP) — typiquement la version enhanced finale.
        motion_subject : clé dans PROMPTS_BY_SUBJECT (water, curtains, foliage, fire, steam, fountain, ambient).
        output_path : destination .mp4 (loop ping-pong final).
        model : surcharge de HIGGSFIELD_MODEL.
        duration_s : surcharge de HIGGSFIELD_DURATION_S (durée brute Kling avant ping-pong).

    Returns:
        dict avec keys : success, output_path, raw_output_path, prompt, model,
                         motion_subject, duration_ms, cost_usd, error.
    """
    t0 = time.time()
    model = model or HIGGSFIELD_MODEL
    duration_s = duration_s or HIGGSFIELD_DURATION_S
    prompt = PROMPTS_BY_SUBJECT.get(motion_subject, PROMPT_FALLBACK)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_output = output_path.with_name(output_path.stem + "_raw.mp4")

    base = {
        "success": False,
        "output_path": None,
        "raw_output_path": None,
        "prompt": prompt,
        "model": model,
        "motion_subject": motion_subject,
        "duration_s": duration_s,
        "cost_usd": 0.0,
        "duration_ms": 0,
        "error": None,
    }

    if DRY_RUN:
        base["error"] = "DRY_RUN active (SLOWMO_DRY_RUN=1)"
        base["duration_ms"] = int((time.time() - t0) * 1000)
        return base

    if not has_credentials():
        base["error"] = "Credentials Higgsfield manquants (HF_API_KEY + HF_API_SECRET ou HF_KEY)"
        base["duration_ms"] = int((time.time() - t0) * 1000)
        return base

    try:
        import higgsfield_client as hf
    except ImportError:
        base["error"] = "higgsfield-client non installé (pip install higgsfield-client)"
        base["duration_ms"] = int((time.time() - t0) * 1000)
        return base

    try:
        # 1. Upload de l'image vers le CDN Higgsfield
        image_url = hf.upload_file(input_path)

        # 2. Submit + wait
        result = hf.subscribe(
            model,
            arguments={
                "prompt": prompt,
                "image_url": image_url,
                "duration": duration_s,
            },
        )

        # Récup l'URL vidéo (réponse Higgsfield : { video: { url: ... } } ou { videos: [...] })
        video_url = None
        if isinstance(result, dict):
            video_url = (result.get("video") or {}).get("url") if isinstance(result.get("video"), dict) else None
            if not video_url and result.get("videos"):
                v0 = result["videos"][0]
                video_url = v0.get("url") if isinstance(v0, dict) else None
            if not video_url:
                # Certains modèles renvoient directement url ou raw.url
                video_url = result.get("url") or (result.get("raw") or {}).get("url")

        if not video_url:
            base["error"] = f"Réponse Higgsfield sans URL vidéo : {str(result)[:300]}"
            base["duration_ms"] = int((time.time() - t0) * 1000)
            return base

        # 3. Download du raw
        _download(video_url, raw_output)
        base["raw_output_path"] = str(raw_output)

        # 4. Post-process ping-pong → loop seamless
        _ping_pong_loop(raw_output, output_path)

        base["success"] = True
        base["output_path"] = str(output_path)
        base["cost_usd"] = HIGGSFIELD_PRICE_USD
        base["duration_ms"] = int((time.time() - t0) * 1000)
        return base

    except Exception as e:
        base["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        base["duration_ms"] = int((time.time() - t0) * 1000)
        return base
