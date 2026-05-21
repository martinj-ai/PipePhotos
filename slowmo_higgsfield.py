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

# ━ Mode de loop ━
# - "ping_pong" (default) : clip + reverse(clip) → 10s loop mathématiquement seamless.
#   ✅ Idéal pour sujets NON-DIRECTIONNELS (water, pool_float, curtains, foliage, steam).
#   ❌ Crée un effet "stop-motion" sur sujets DIRECTIONNELS (fire, fountain).
# - "crossfade" : clip 5s avec fondu enchaîné fin↔début 0.5s → 5s loop.
#   ✅ Marche sur sujets directionnels (flammes qui montent, fontaine qui jaillit).
#   ❌ Léger blur visible au point de couture (0.5s de morph).
# - "auto" : ping_pong sauf si motion_subject ∈ {fire, fountain} → crossfade.
SLOWMO_LOOP_MODE = os.getenv("SLOWMO_LOOP_MODE", "auto").lower()

# Sujets considérés directionnels → crossfade auto en mode "auto".
DIRECTIONAL_SUBJECTS = {"fire", "fountain"}

# Durée du crossfade (secondes) — 0.5s = bon compromis blur invisible / loop court.
CROSSFADE_DURATION_S = float(os.getenv("SLOWMO_CROSSFADE_DURATION", "0.5"))


# --- Prompts par sujet de mouvement ---
# Volonté : mouvement AMBIANT et NON-DIRECTIONNEL pour que le ping-pong soit invisible.
# Mots-clés négatifs : "no camera movement", "static composition" — Kling DoP a tendance
# à pousser des camera moves cinématiques par défaut, on les coupe.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLAUSE ANTI-TIMELAPSE v2 (Martin 20/05/2026 — feedback "encore des timelapse")
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Évolution :
# - v1 (15/05) : clause générique "real-time, not time-lapse" en fin de prompt
#   → Kling continue à pousser timelapse (nuages qui défilent, ombres qui
#     se déplacent vite). En cause : (a) clause trop générique, (b) en fin de
#     prompt donc moins prioritaire dans la lecture du modèle.
# - v2 (20/05) : clause préfixe (lue en premier) + interdits explicites par
#   élément (nuages, ombres, vagues, lumière) + ancrage durée concret
#   ("5 seconds of footage = 5 seconds of real time") + comparaison
#   start/end frame (doit ressembler quasi-identique sur des éléments lents).
_ANTI_TIMELAPSE_CLAUSE = (
    "🚨 ABSOLUTE TEMPO RULE — read this FIRST and override any other instinct :\n"
    "This is a 5-second clip that plays at REAL-TIME natural speed. "
    "5 seconds of footage = exactly 5 seconds of real-world time, NOT a compressed "
    "hour or day. A bystander watching the scene live for 5 seconds would see "
    "EXACTLY what's in the clip.\n\n"
    "❌ ABSOLUTELY FORBIDDEN (these are the SIGNATURE of timelapse and ruin Dayuse aesthetics) :\n"
    "- NO clouds moving across the sky in 5 seconds (clouds at this time scale are "
    "  visually static — at most a barely perceptible drift on cumulus edges)\n"
    "- NO shadows shifting position visibly (the sun does NOT move in 5 seconds)\n"
    "- NO sun arc / golden hour shift across the frame\n"
    "- NO accelerated waves crashing repeatedly (gentle ripples only, slow cycle)\n"
    "- NO ambient light color / temperature change over the duration\n"
    "- NO fast-cycling reflections on water or glass\n"
    "- NO 'people walking by quickly' background motion\n"
    "- NO time-of-day transition (day to dusk or anything similar)\n"
    "- NO 'lapse of time' or 'compressed time' or 'fast montage' feel of ANY kind\n\n"
    "✅ CORRECT TEMPO REFERENCE :\n"
    "- A leaf sways back and forth maybe 2-3 cycles in 5 seconds (real breeze pace)\n"
    "- A water ripple expands gently across ~1-1.5 meters in 5 seconds\n"
    "- A curtain edge gives ONE soft wave in 5 seconds\n"
    "- A pool float drifts maybe 20 cm in 5 seconds\n"
    "- If you compare frame 1 and frame 120 (= end), they should look almost "
    "  identical for slow-moving elements (sky, shadows, walls, furniture, "
    "  cushions, decor). Only the assigned motion subject visibly progresses.\n\n"
    "VERIFY BEFORE FINALIZING : if your motion looks like a Vimeo timelapse "
    "showreel → wrong. If it looks like a real-time security camera capture of "
    "5 seconds of a calm moment → correct.\n\n"
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLAUSE BONUS : si humain présent, la scène AUTOUR doit aussi être real-time
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pattern dégueu observé Martin : humain figé (frozen face v2) + monde qui
# défile en timelapse autour → effet Final Destination / horreur. La clause
# humain bannit les mouvements de l'humain ; cette clause complète en bannissant
# le timelapse "autour" de l'humain.
_HUMAN_SURROUND_REALTIME_CLAUSE = (
    "🚨 IF a human is visible : the ENVIRONMENT around them must ALSO play at "
    "real-time speed. NEVER pair a frozen human face with a fast-moving sky / "
    "rushing clouds / shifting shadows / time-compressed scene — that combination "
    "is uncanny and forbidden. The human is photographic stillness AND the world "
    "around them is real-time stillness (only the assigned subtle motion).\n\n"
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLAUSE HUMAINS (Martin 19/05/2026 v2 — visages STRICTEMENT immobiles)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Évolution :
# - v1 (15/05) : "humans show micro-movements (breathing, blinks, hair sway)"
#   → Kling animait quand même les visages / têtes → effet "uncanny valley",
#     yeux qui bougent, lèvres qui frémissent, micro-rotations de tête → rendu
#     dégueu sur photos lifestyle Dayuse. Bug observé Martin 18/05/2026.
# - v2 (19/05) : INTERDICTION ABSOLUE de tout mouvement facial/tête/yeux.
#   Seules les EXTRÉMITÉS (pieds dans l'eau, mains qui caressent l'eau, doigts)
#   peuvent bouger, en plus de la respiration imperceptible. Les visages
#   restent figés comme sur une PHOTO — c'est volontaire, ça donne un effet
#   "moment suspendu" Dayuse aspirational, pas un GIF artificiel.
_HUMANS_MICRO_MOTION_CLAUSE = (
    "🚨 CRITICAL HUMAN STILLNESS RULE — read this TWICE :\n"
    "Humans visible in the scene (if any) MUST keep their FACE, HEAD and EYES "
    "ABSOLUTELY FROZEN — exactly as in the input photo. Treat them as a HIGH-RES "
    "PHOTOGRAPH grafted onto the moving scene, not as animated characters.\n\n"
    "❌ STRICTLY FORBIDDEN movements :\n"
    "- NO blinking (eyes stay exactly as captured in the photo, no closing/opening)\n"
    "- NO lip / mouth motion (no breathing through mouth visible, no micro-expressions)\n"
    "- NO head turning, tilting, or any rotation\n"
    "- NO facial micro-expressions (eyebrows, cheeks, jaw all stay frozen)\n"
    "- NO eye darting / pupil movement\n"
    "- NO chest breathing visible (the rib cage stays perfectly still)\n"
    "- NO body shifting, NO arm gestures, NO leg crossing/uncrossing, NO walking\n"
    "- NO posture changes, NO weight shifting\n\n"
    "✅ ONLY these EXTREMITY motions are allowed (small, slow, natural) :\n"
    "- Feet/calves in water → tiny ripples around them, water displacement\n"
    "- Hands resting in/near water → fingers may move imperceptibly, water reacts\n"
    "- Hair tips → barely moving with the same breeze that moves leaves/water\n"
    "- Loose clothing edges (sarong, dress hem) → soft fabric sway only at the edges\n\n"
    "RESULT TARGET : the human looks like a PHOTOGRAPH overlaid on a living scene. "
    "Stillness is intentional and aspirational (Dayuse 'moment suspendu' aesthetic), "
    "NOT a failed animation. If you can't honor this, freeze the human entirely. "
)

# ━━ STRUCTURE DES PROMPTS v2 (Martin 20/05/2026) ━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Avant : description sujet → humain → anti-timelapse (en fin). Kling lit en
# priorité le début → anti-timelapse mal respecté.
# Maintenant : anti-timelapse EN TÊTE (priorité de lecture) → description sujet
# → humain stillness → human surround real-time → rappel statique.
PROMPTS_BY_SUBJECT = {
    "pool_float": (
        _ANTI_TIMELAPSE_CLAUSE +
        "MOTION SUBJECT : the inflatable pool float (flamingo, unicorn, swan, donut, etc.) "
        "drifts very slowly and gently on the water surface — soft horizontal bob and slow "
        "rotation around its vertical axis. Small concentric ripples spread around the float "
        "as it moves. The float never leaves the frame.\n\n"
        "Static composition, locked-off camera, no camera movement, no zoom, no pan.\n\n"
        + _HUMANS_MICRO_MOTION_CLAUSE
        + _HUMAN_SURROUND_REALTIME_CLAUSE +
        "Only the float and the water ripples around it move softly. Everything else (sky, "
        "clouds, shadows, deck, furniture, plants) stays photographically still."
    ),
    "water": (
        _ANTI_TIMELAPSE_CLAUSE +
        "MOTION SUBJECT : subtle gentle water ripples on the surface, soft slow ambient "
        "movement, natural reflection shimmer. Real water at calm pace — not crashing, not "
        "rushing.\n\n"
        "Static composition, locked-off camera, no camera movement, no zoom, no pan.\n\n"
        + _HUMANS_MICRO_MOTION_CLAUSE
        + _HUMAN_SURROUND_REALTIME_CLAUSE +
        "Only the water surface moves softly. Sky, clouds, shadows, surroundings stay still."
    ),
    "curtains": (
        _ANTI_TIMELAPSE_CLAUSE +
        "MOTION SUBJECT : soft gentle breeze making the curtains and light fabrics sway "
        "slowly with one or two cycles over the clip duration. Ambient natural drift.\n\n"
        "Static composition, locked-off camera, no camera movement, no zoom, no pan.\n\n"
        + _HUMANS_MICRO_MOTION_CLAUSE
        + _HUMAN_SURROUND_REALTIME_CLAUSE +
        "Only the fabric edges sway. Everything else (sky, light, decor, furniture) stays still."
    ),
    "foliage": (
        _ANTI_TIMELAPSE_CLAUSE +
        "MOTION SUBJECT : a gentle real breeze softly animating the FOLIAGE — leaves, "
        "palm fronds, tropical plants, shrubs, grass. Each leaf sways back and forth maybe "
        "2-3 cycles over the full 5 seconds (= real breeze pace, NOT wind tunnel, NOT storm). "
        "Palm fronds catch the light as they move ; smaller leaves flutter gently ; tall "
        "grasses bend slightly. The motion is hypnotic, organic, alive — the kind of "
        "subtle natural movement you notice in a Dayuse hotel garden when sitting still.\n\n"
        "Static composition, locked-off camera, no camera movement, no zoom, no pan.\n\n"
        + _HUMANS_MICRO_MOTION_CLAUSE
        + _HUMAN_SURROUND_REALTIME_CLAUSE +
        "Only foliage moves subtly. Sky, clouds, shadows, water surface, buildings stay still."
    ),
    "fire": (
        _ANTI_TIMELAPSE_CLAUSE +
        "MOTION SUBJECT : gentle dancing flames, soft warm flicker, slow ember glow. Real "
        "fire physics at natural pace.\n\n"
        "Static composition, locked-off camera, no camera movement, no zoom, no pan.\n\n"
        + _HUMANS_MICRO_MOTION_CLAUSE
        + _HUMAN_SURROUND_REALTIME_CLAUSE +
        "Only the flames flicker. Everything else (smoke trails, decor, surroundings) stays still."
    ),
    "steam": (
        _ANTI_TIMELAPSE_CLAUSE +
        "MOTION SUBJECT : soft slow rising steam and mist, gentle ambient drift upward. "
        "Real steam at natural rate — not blasting, not exploding, just curling slowly.\n\n"
        "Static composition, locked-off camera, no camera movement, no zoom, no pan.\n\n"
        + _HUMANS_MICRO_MOTION_CLAUSE
        + _HUMAN_SURROUND_REALTIME_CLAUSE +
        "Only the steam wisps drift. Background and objects remain still."
    ),
    "fountain": (
        _ANTI_TIMELAPSE_CLAUSE +
        "MOTION SUBJECT : gentle water flow from the fountain, soft continuous splashing, "
        "ambient water motion at calm pace.\n\n"
        "Static composition, locked-off camera, no camera movement, no zoom, no pan.\n\n"
        + _HUMANS_MICRO_MOTION_CLAUSE
        + _HUMAN_SURROUND_REALTIME_CLAUSE +
        "Only the fountain water flows. Everything else stays still."
    ),
}

PROMPT_FALLBACK = (
    _ANTI_TIMELAPSE_CLAUSE +
    "MOTION SUBJECT : subtle ambient atmosphere with very gentle natural motion (foliage "
    "sway, water shimmer, soft fabric drift — whichever is present in the scene).\n\n"
    "Static composition, locked-off camera, no camera movement, no zoom, no pan.\n\n"
    + _HUMANS_MICRO_MOTION_CLAUSE
    + _HUMAN_SURROUND_REALTIME_CLAUSE +
    "Photorealistic, high quality. The clip should feel like 5 seconds of real-time presence "
    "in a calm Dayuse hotel space, NOT a stylized time-lapse."
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


# Sujets de mouvement autorisés (Martin 14/05/2026) — restriction "pool uniquement".
# Avant : 7 sujets (pool_float, water, curtains, foliage, fire, steam, fountain)
# Après : 3 sujets cohérents avec une scène piscine (eau, bouée, plantes autour)
ALLOWED_MOTION_SUBJECTS_POOL = {"pool_float", "water", "foliage"}


def _is_pool_category(analysis: dict) -> bool:
    """Détermine si une photo appartient au scope 'piscine' pour le slowmo.

    Critère Q1=B (Martin 14/05/2026) : étendu = primary OR secondary contient 'piscine'.
    Inclut les rooftops avec piscine identifiable comme catégorie secondaire.
    """
    factual = (analysis or {}).get("factual") or {}
    cat = (factual.get("category") or "").lower()
    if cat == "piscine":
        return True
    sec = factual.get("categories_secondary") or []
    return any((s or "").lower() == "piscine" for s in sec)


def pick_slowmo_target(
    ordered_pack: list[dict],
    by_filename: dict[str, dict],
    enhanced_results: list[dict] | None = None,
) -> Optional[dict]:
    """Choisit la photo du pack final qui sera convertie en slowmo.

    Stratégie (Martin 15/05/2026 v3 — "humains IA autorisés si validation 100% clean") :
      1. **Filtre catégorie PISCINE** (primary OR secondary). Garde scope pool-only.
      2. EXCLURE les photos avec humain NATIF au premier plan
         (Higgsfield/Kling déforme les humains natifs de la photo source).
      3. Si la photo a un humain AJOUTÉ IA → on l'AUTORISE comme source slowmo
         (vs avant où on basculait sur intermédiaire) MAIS UNIQUEMENT si :
         (a) `ai_validation.ok == True` (= 0 violation détectée)
         (b) `fallback_to_original == False` (= on a bien une vraie photo IA, pas l'originale en fallback)
         Sinon → SKIP cette candidate (Kling amplifierait les défauts détectés).
      4. Garder les photos avec `slowmo_potential.has_motion_subject == True`
         ET `motion_subject` ∈ {pool_float, water, foliage}.
      5. **Tri par SLOT ASCENDANT** (= top photo en priorité, peu importe motion_strength).
         Tie-break : motion_strength desc.
      6. Fallback : si aucune candidate qualifiée → 1ère photo PISCINE sans humain
         natif AVEC validation OK, motion_subject="ambient".

    Args:
        ordered_pack : pack final ordonné (slot 1, 2, …)
        by_filename : analyses Gemini Vision indexées par filename
        enhanced_results : résultats enhance_one indexés. Sert à détecter les photos
            avec humain ajouté IA + vérifier la validation post-IA.

    Returns:
        dict {filename, motion_subject, motion_strength, slot, fallback_used,
              has_ai_human, use_intermediate=False} ou None.
        `use_intermediate` est conservé en rétrocompat mais toujours False désormais.
    """
    # ━ Index des enhanced_results par filename pour lookup rapide ━
    enhanced_by_fn: dict[str, dict] = {}
    if enhanced_results:
        for r in enhanced_results:
            fname = r.get("filename") or (
                r.get("input_path", "").rsplit("/", 1)[-1]
                if r.get("input_path") else None
            )
            if fname:
                enhanced_by_fn[fname] = r

    def _has_ai_human(r: dict) -> bool:
        """Détecte si une photo a un humain ajouté par IA."""
        if r.get("persona_used"):
            return True
        for s in (r.get("steps") or []):
            if s.get("action") == "ai_add_character":
                return True
        return False

    def _validation_clean(r: dict) -> bool:
        """Photo passe la validation IA full clean (0 violation, pas fallback).

        Martin 15/05/2026 : strict — slowmo amplifie les défauts (face/anatomy,
        pool surface, etc.) donc on n'autorise qu'une photo qui a TOUT validé.
        """
        if r.get("fallback_to_original"):
            return False
        ai_val = r.get("ai_validation") or {}
        if not ai_val:
            return True  # pas de validation = pas de step IA = OK par défaut
        if not ai_val.get("ok"):
            return False
        if ai_val.get("violations"):
            return False
        return True

    candidates = []
    for slot, entry in enumerate(ordered_pack, 1):
        filename = entry["input"]["filename"]
        a = by_filename.get(filename) or {}
        analysis = a.get("analysis") or {}

        # 1. Filtre catégorie PISCINE
        if not _is_pool_category(analysis):
            continue

        # 2. Exclusion humains NATIFS au premier plan (Kling déforme)
        factual = analysis.get("factual") or {}
        if (factual.get("human_count") or 0) > 0:
            presence = (factual.get("human_presence_type") or "").lower()
            if presence in ("full_visible", "fully visible", "complete", "prominent"):
                continue

        # 3. Si humain AJOUTÉ IA → exiger validation 100% clean
        r = enhanced_by_fn.get(filename) or {}
        has_ai_human = _has_ai_human(r)
        if has_ai_human and not _validation_clean(r):
            # Photo avec humain IA mais validation pas clean → SKIP
            # (Kling amplifierait les défauts détectés sur l'humain ou la scène)
            continue

        # 4. Motion_subject autorisé pool-only
        sp = analysis.get("slowmo_potential") or {}
        if sp.get("has_motion_subject"):
            motion_subj = sp.get("motion_subject", "water")
            if motion_subj in ALLOWED_MOTION_SUBJECTS_POOL:
                candidates.append({
                    "filename": filename,
                    "motion_subject": motion_subj,
                    "motion_strength": int(sp.get("motion_strength", 0) or 0),
                    "slot": slot,
                    "has_ai_human": has_ai_human,
                    # use_intermediate gardé en rétrocompat mais désormais TOUJOURS False
                    # (Martin 15/05/2026 : on utilise la version finale avec humain IA)
                    "use_intermediate": False,
                    "fallback_used": False,
                })

    if candidates:
        # Tri : slot asc (= top photo prioritaire), puis motion_strength desc en tie-break
        candidates.sort(key=lambda c: (c["slot"], -c["motion_strength"]))
        return candidates[0]

    # Fallback : 1ère photo PISCINE sans humain natif AVEC validation OK
    for slot, entry in enumerate(ordered_pack, 1):
        filename = entry["input"]["filename"]
        a = by_filename.get(filename) or {}
        analysis = a.get("analysis") or {}
        if not _is_pool_category(analysis):
            continue
        factual = analysis.get("factual") or {}
        if (factual.get("human_count") or 0) > 0:
            presence = (factual.get("human_presence_type") or "").lower()
            if presence in ("full_visible", "fully visible", "complete", "prominent"):
                continue
        # Exiger validation clean pour le fallback aussi
        r = enhanced_by_fn.get(filename) or {}
        has_ai_human = _has_ai_human(r)
        if has_ai_human and not _validation_clean(r):
            continue
        return {
            "filename": filename,
            "motion_subject": "ambient",
            "motion_strength": 0,
            "slot": slot,
            "has_ai_human": has_ai_human,
            "use_intermediate": False,
            "fallback_used": True,
        }

    # Aucune photo PISCINE valide disponible → pas de slowmo
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


def _probe_duration_seconds(mp4_path: Path) -> float:
    """Récupère la durée d'un mp4 en secondes via ffprobe (fallback ffmpeg si absent)."""
    ff = _ffmpeg_bin()
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(mp4_path)],
            capture_output=True, text=True, timeout=30,
        )
        try:
            return float(proc.stdout.strip())
        except ValueError:
            pass
    # Fallback : extrait depuis stderr de `ffmpeg -i`
    proc = subprocess.run(
        [ff, "-i", str(mp4_path)],
        capture_output=True, text=True, timeout=30,
    )
    import re
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr)
    if m:
        h, mn, s = m.groups()
        return int(h) * 3600 + int(mn) * 60 + float(s)
    # Dernier recours : suppose 5s (default Kling)
    return 5.0


def _crossfade_loop(input_mp4: Path, output_mp4: Path, fade_duration_s: float = CROSSFADE_DURATION_S) -> None:
    """Génère un loop crossfade : fondu enchaîné entre la fin et le début du clip.

    Technique : on duplique le clip, on offset le 2nd de (duration - fade) et on `xfade`
    transition=fade entre les deux. La fin du clip 1 se fond dans le début du clip 2 sur
    `fade_duration_s` secondes. Résultat : durée = durée originale, loop seamless avec
    un léger blur (~0.5s) sur la zone de couture.

    Idéal pour sujets DIRECTIONNELS (fire/fountain) où le reverse du ping-pong se voit.
    """
    ff = _ffmpeg_bin()
    duration_s = _probe_duration_seconds(input_mp4)
    offset = max(0.1, duration_s - fade_duration_s)
    cmd = [
        ff, "-y",
        "-i", str(input_mp4),
        "-i", str(input_mp4),
        "-filter_complex",
        f"[0:v][1:v]xfade=transition=fade:duration={fade_duration_s}:offset={offset}[v]",
        "-map", "[v]",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(output_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg crossfade failed: {proc.stderr[:500]}")


# ━ Variantes format (V1.3) ━
# Le slowmo principal sort dans le ratio source. Pour utilisation Insta story / feed
# / YouTube, on génère des variantes via ffmpeg crop intelligent (center-crop le plus
# souvent OK pour un cinemagraph car le sujet est généralement au centre).
SLOWMO_FORMAT_VARIANTS = {
    # ratio_id : (width, height, label)
    "story_9x16":   (1080, 1920, "Insta story / Reels (9:16)"),
    "feed_1x1":     (1080, 1080, "Insta feed (1:1)"),
    "youtube_16x9": (1920, 1080, "YouTube / desktop (16:9)"),
}


def _crop_resize_mp4(input_mp4: Path, output_mp4: Path, target_w: int, target_h: int) -> None:
    """Crop centré + resize un mp4 vers (target_w, target_h) en préservant le ratio cible.

    Stratégie : filtre ffmpeg `crop=ow:oh + scale=W:H`. On crop d'abord le bon ratio
    (le maximum possible centré), puis on resize au target exact. Pas de letterbox.
    """
    ff = _ffmpeg_bin()
    # ffmpeg expression : crop=w:h:(iw-w)/2:(ih-h)/2 où w/h calculés dynamiquement
    # pour matcher le ratio target. On utilise les variables ffmpeg iw/ih.
    target_ratio = target_w / target_h
    # crop_w = min(iw, ih * target_ratio) ; crop_h = min(ih, iw / target_ratio)
    crop_expr = (
        f"crop='if(gt(iw/ih,{target_ratio}),ih*{target_ratio},iw)':"
        f"'if(gt(iw/ih,{target_ratio}),ih,iw/{target_ratio})':"
        f"(iw-iw)/2:(ih-ih)/2,"
        f"scale={target_w}:{target_h}"
    )
    cmd = [
        ff, "-y",
        "-i", str(input_mp4),
        "-vf", crop_expr,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(output_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg crop {target_w}x{target_h} failed: {proc.stderr[:500]}")


def generate_format_variants(main_mp4: Path, output_dir: Path, variants: list[str] | None = None) -> list[dict]:
    """Génère les variantes de format (Insta story / feed / YouTube) à partir du mp4 principal.

    Args:
        main_mp4 : le mp4 loop principal (déjà ping-pong ou crossfade)
        output_dir : dossier où écrire les variantes (typiquement data/output/{slug}/slowmo/)
        variants : liste de ratio_ids depuis SLOWMO_FORMAT_VARIANTS. None = toutes.

    Returns:
        Liste de dicts {ratio_id, label, width, height, output_path, error?}.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    if variants is None:
        variants = list(SLOWMO_FORMAT_VARIANTS.keys())

    stem = main_mp4.stem
    results = []
    for ratio_id in variants:
        spec = SLOWMO_FORMAT_VARIANTS.get(ratio_id)
        if not spec:
            results.append({"ratio_id": ratio_id, "error": f"ratio inconnu: {ratio_id}"})
            continue
        w, h, label = spec
        out_path = output_dir / f"{stem}_{ratio_id}.mp4"
        item = {"ratio_id": ratio_id, "label": label, "width": w, "height": h,
                "output_path": str(out_path)}
        try:
            _crop_resize_mp4(main_mp4, out_path, w, h)
        except Exception as e:
            item["error"] = str(e)[:200]
            item["output_path"] = None
        results.append(item)
    return results


def _pick_loop_mode(motion_subject: str, configured: str = SLOWMO_LOOP_MODE) -> str:
    """Détermine le mode loop à utiliser pour un motion_subject donné.

    - configured="ping_pong" → toujours ping_pong
    - configured="crossfade" → toujours crossfade
    - configured="auto" (default) → crossfade pour les sujets directionnels (fire, fountain),
                                     ping_pong sinon.
    """
    if configured in ("ping_pong", "crossfade"):
        return configured
    return "crossfade" if motion_subject in DIRECTIONAL_SUBJECTS else "ping_pong"


def generate_slowmo(
    input_path: Path,
    motion_subject: str,
    output_path: Path,
    *,
    model: Optional[str] = None,
    duration_s: Optional[int] = None,
    format_variants: Optional[list[str]] = None,
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
        result_status = (result or {}).get("status") if isinstance(result, dict) else None
        if isinstance(result, dict):
            video_url = (result.get("video") or {}).get("url") if isinstance(result.get("video"), dict) else None
            if not video_url and result.get("videos"):
                v0 = result["videos"][0]
                video_url = v0.get("url") if isinstance(v0, dict) else None
            if not video_url:
                # Certains modèles renvoient directement url ou raw.url
                video_url = result.get("url") or (result.get("raw") or {}).get("url")

        if not video_url:
            # ━ Détection des statuts d'échec connus pour des messages clairs ━
            # nsfw : le filter classifier Higgsfield refuse la photo (faux positifs
            #   fréquents sur photos hôtel avec maillots de bain). Solution : régénérer
            #   avec une autre photo cible via le bouton ↻ Régénérer.
            # failed / error : le modèle a échoué côté Higgsfield (network, GPU OOM…).
            if result_status == "nsfw":
                base["error"] = (
                    "🚫 Higgsfield a classifié la photo cible comme NSFW (filtre trop strict, "
                    "fréquent sur photos hôtel avec maillots). Coût $0 (refusé avant facturation). "
                    "Régénère avec une autre photo via le bouton ↻ Régénérer, ou contourne en passant "
                    "par un autre modèle (DoP standard est moins strict)."
                )
                base["nsfw_blocked"] = True
                base["request_id"] = (result or {}).get("request_id")
            elif result_status in ("failed", "error"):
                base["error"] = (
                    f"Higgsfield a renvoyé status={result_status} (échec côté API). "
                    f"Re-tente via ↻ Régénérer. Détails : {str(result)[:200]}"
                )
            else:
                base["error"] = f"Réponse Higgsfield sans URL vidéo : {str(result)[:300]}"
            base["duration_ms"] = int((time.time() - t0) * 1000)
            return base

        # 3. Download du raw
        _download(video_url, raw_output)
        base["raw_output_path"] = str(raw_output)

        # 4. Post-process : ping-pong OU crossfade selon le sujet
        loop_mode = _pick_loop_mode(motion_subject)
        base["loop_mode"] = loop_mode
        if loop_mode == "crossfade":
            _crossfade_loop(raw_output, output_path)
        else:
            _ping_pong_loop(raw_output, output_path)

        # 5. Variantes format (Insta story / feed / YouTube) si demandées
        # Coût : zéro (ffmpeg local). Temps : ~1-2s par variante.
        if format_variants:
            try:
                variants = generate_format_variants(
                    output_path, output_path.parent, variants=format_variants
                )
                base["format_variants"] = variants
            except Exception as e:
                base["format_variants_error"] = str(e)[:200]

        base["success"] = True
        base["output_path"] = str(output_path)
        base["cost_usd"] = HIGGSFIELD_PRICE_USD
        base["duration_ms"] = int((time.time() - t0) * 1000)
        return base

    except Exception as e:
        base["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        base["duration_ms"] = int((time.time() - t0) * 1000)
        return base
