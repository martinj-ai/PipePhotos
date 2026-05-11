"""LUT brand Dayuse — applique un ton colorimétrique COHÉRENT à toutes les photos finales.

Objectif : que toutes les photos d'un pack ET tous les packs (tous les hôtels, dans le temps)
aient le même ton brand "soleil de jour ensoleillé Dayuse" — ciel bleu vif, tons chauds golden,
saturation premium-accessible, jamais HDR ni sur-saturé.

Source de vérité : `config/brand_lut.json` — modifiable sans toucher au code Python.
Si le fichier est absent ou corrompu, on tombe sur DEFAULT_PARAMS (versionnés ici).
"""

from __future__ import annotations

import json
from pathlib import Path
from PIL import Image, ImageEnhance

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config" / "brand_lut.json"

# Paramètres par défaut — v2 (11/05/2026) calibrés pour "soleil de jour Dayuse" :
# - saturation +18% (pop couleurs, encore en-dessous du seuil HDR ~1.25)
# - contraste +10% (donne du punch aux ombres/lumières sans écraser)
# - luminosité +5% (effet de scène ensoleillée, pas surex)
# - warmth +8% R, -8% B (ambiance dorée golden hour, retire les tons bleus parasites)
# Martin v1 (sat+12%, lum+2%, warmth R+4% B-3%) trouvait que l'effet n'était pas assez
# ensoleillé sur les photos finales — bump cohérent pour passer "ambiance neutre" → "soleil chaud".
DEFAULT_PARAMS = {
    "saturation": 1.18,
    "contrast": 1.10,
    "brightness": 1.05,
    "warmth_r": 1.08,
    "warmth_b": 0.92,
}


def load_params() -> dict:
    """Lit les paramètres LUT depuis config/brand_lut.json (avec fallback défauts)."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                user = json.load(f)
            return {**DEFAULT_PARAMS, **user}
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_PARAMS.copy()


def apply_brand_lut(input_path: Path, output_path: Path, params: dict | None = None) -> dict:
    """Applique la LUT brand sur l'image. Déterministe, reproductible.

    Si input_path == output_path, on travaille en place (lecture + écriture sur le même fichier).
    """
    if params is None:
        params = load_params()

    img = Image.open(input_path).convert("RGB")

    # 1. Saturation
    img = ImageEnhance.Color(img).enhance(params.get("saturation", 1.12))
    # 2. Contraste
    img = ImageEnhance.Contrast(img).enhance(params.get("contrast", 1.06))
    # 3. Luminosité
    img = ImageEnhance.Brightness(img).enhance(params.get("brightness", 1.02))
    # 4. Warmth (push R, pull B)
    warmth_r = params.get("warmth_r", 1.04)
    warmth_b = params.get("warmth_b", 0.97)
    if warmth_r != 1.0 or warmth_b != 1.0:
        r, g, b = img.split()
        if warmth_r != 1.0:
            r = r.point(lambda v: max(0, min(255, int(v * warmth_r))))
        if warmth_b != 1.0:
            b = b.point(lambda v: max(0, min(255, int(v * warmth_b))))
        img = Image.merge("RGB", (r, g, b))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, quality=92)
    return {"params_applied": params}
